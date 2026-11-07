"""MCP server: one tool per active card action, served over Streamable HTTP with Bearer-key auth.

Run:  uv run python -m runtime.gateway --load tests/fixture_site.json
"""

from __future__ import annotations

import argparse
import hmac
import importlib
import json
import logging
import os
import threading
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
from mcp.server.connection import Connection
from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ToolsListChanged
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from runtime.browser.pool import BrowserConfig, BrowserPool
from runtime.catalog import find_actions
from runtime.browser.tier import BrowserTier
from runtime.confirm import Confirmer
from runtime.connect import ConnectionManager
from runtime.connect_page import routes as connect_routes
from runtime.neko.manager import NekoConfig, NekoManager
from runtime.executor import Executor
from runtime.healing import HealConfig, HealManager, Remapper
from runtime.registry import Registry, StoredCard
from runtime.session_store import ChainedSessionStore, EncryptedSessionStore
from runtime.sessions import DevEnvSessionStore
from runtime.tiers import default_tiers
from runtime.state import StateStore
from schema import load_card
from schema.params import input_schema, tool_description

log = logging.getLogger("runtime.gateway")

DEFAULT_USER = "default"  # one API key = one user for now (TODO: per-user keys)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


@dataclass(frozen=True)
class ToolEntry:
    domain: str
    action: str
    tool: types.Tool


INSTRUCTIONS = (
    "Tools that act on websites for the user. Use find_actions to discover what a site can do, then run_action "
    "to do it. Every tool returns JSON; check its `status` field (SUCCESS, EMPTY, NEEDS_CONFIRMATION, "
    "AUTH_REQUIRED, ...) to decide what to do next.\n"
    "When you talk to the user, describe outcomes in plain terms (\"You're signed out of linkedin.com\", "
    "\"Sign in here: <link>\", \"The site changed; I'll retry in a moment\"). Don't mention tool names, status "
    "codes, sessions, cookies, browser profiles, cards or other internals unless the user asks how it works. "
    "Results about signing in include a `user_message`; use it.\n"
    "The user cannot see tool results. When start_connection gives you a sign-in link, write its user_message "
    "(a markdown link) exactly as given in your reply before you call poll_connection, or they will have nothing "
    "to click. (Apps that support "
    "it show the link to the user themselves; then start_connection simply returns once they've signed in.)"
)


TOOLS_MODES = ("generic", "per_action")

FIND_ACTIONS_TOOL = types.Tool(
    name="find_actions",
    description="Find what you can do on websites for the user. Give a short description of the task (query), a "
    "site (its domain, e.g. linkedin.com), or both. Returns matching actions with their parameters; then call "
    "run_action. With no arguments it lists the available sites.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "what you want to do, e.g. 'search companies'"},
            "site": {"type": "string", "description": "a site's domain, e.g. linkedin.com"},
        },
        "additionalProperties": False,
    },
    annotations=types.ToolAnnotations(read_only_hint=True),
)

RUN_ACTION_TOOL = types.Tool(
    name="run_action",
    description="Run an action from find_actions. Pass its params as an object. Returns JSON with a `status` "
    "(SUCCESS, EMPTY, NEEDS_CONFIRMATION, AUTH_REQUIRED, ...) and the data. Actions that need confirmation first "
    "return NEEDS_CONFIRMATION with a summary: show it to the user and, only after their explicit approval, call "
    "again with identical params plus confirmation_id.",
    input_schema={
        "type": "object",
        "properties": {
            "site": {"type": "string", "description": "the site's domain, from find_actions"},
            "action": {"type": "string", "description": "the action's name, from find_actions"},
            "params": {"type": "object", "description": "the action's parameters, as described by find_actions"},
            "confirmation_id": {
                "type": "string",
                "description": "from a NEEDS_CONFIRMATION result, only after the user explicitly approved it",
            },
        },
        "required": ["site", "action"],
        "additionalProperties": False,
    },
)

HEAL_STATUS_TOOL = types.Tool(
    name="heal_status",
    description="Status of a heal job started by a PAGE_CHANGED result (its heal.job_id). When the status is "
    "'succeeded', call the original tool again.",
    input_schema={
        "type": "object",
        "properties": {"job_id": {"type": "string", "description": "heal.job_id from a PAGE_CHANGED result"}},
        "required": ["job_id"],
        "additionalProperties": False,
    },
)


def _domain_tool(
    name: str,
    description: str,
    extra: dict[str, Any] | None = None,
    annotations: types.ToolAnnotations | None = None,
) -> types.Tool:
    props: dict[str, Any] = {"domain": {"type": "string", "description": "the site's domain, e.g. linkedin.com"}}
    props.update(extra or {})
    return types.Tool(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": props, "required": ["domain"], "additionalProperties": False},
        annotations=annotations,
    )


CONNECTION_TOOLS = {
    t.name: t
    for t in (
        _domain_tool(
            "start_connection",
            "Get a sign-in link for a site, after a tool result says the user needs to sign in (AUTH_REQUIRED or "
            "AUTH_EXPIRED). The user can't see tool results: write the result's user_message (it contains the link) "
            "in your reply to them first. Then call poll_connection: it waits until they've signed in.",
            {"provider": {"type": "string", "description": "optional connect provider id, if the site has several"}},
        ),
        _domain_tool(
            "poll_connection",
            "Wait for the user to finish signing in. Holds for up to wait_seconds (default 25, max 45) and returns "
            "the moment they're signed in. If status is still 'pending', call it again; when 'connected', retry the "
            "original tool.",
            {"wait_seconds": {"type": "number", "minimum": 0, "maximum": 45, "description": "how long to wait (default 25)"}},
        ),
        _domain_tool(
            "connection_status",
            "Whether the user is signed in to a site through this service (connected, expired, pending or "
            "not_connected).",
        ),
        _domain_tool(
            "disconnect",
            "Sign the user out of a site in this service (it forgets their sign-in; the site itself is not "
            "touched). Only call this when the user asks to disconnect or sign out of that site.",
            {"provider": {"type": "string", "description": "optional connect provider id; default: all of them"}},
            types.ToolAnnotations(destructive_hint=True, idempotent_hint=True, read_only_hint=False),
        ),
    )
}


class _Server(Server[dict[str, Any]]):
    """Advertises `tools.listChanged` on handshake-era connections too."""

    def create_initialization_options(
        self,
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
        extensions: dict[str, dict[str, Any]] | None = None,
    ) -> InitializationOptions:
        return super().create_initialization_options(
            notification_options or NotificationOptions(tools_changed=True), experimental_capabilities, extensions
        )


class Gateway:
    def __init__(
        self,
        registry: Registry,
        executor: Executor,
        poll_interval_s: float = 1.0,
        healer: HealManager | None = None,
        connections: ConnectionManager | None = None,
        pool: BrowserPool | None = None,
        keepalive_s: float = 0.0,
        tools_mode: str = "generic",
    ) -> None:
        if tools_mode not in TOOLS_MODES:
            raise ValueError(f"tools_mode must be one of {TOOLS_MODES}")
        self.registry = registry
        self.executor = executor
        self.healer = healer
        self.connections = connections
        self.pool = pool
        self.keepalive_s = keepalive_s
        self.tools_mode = tools_mode
        self.poll_interval_s = poll_interval_s
        self.bus = InMemorySubscriptionBus()  # 2026-07-28 clients: subscriptions/listen
        self._legacy_connections: weakref.WeakSet[Connection] = weakref.WeakSet()  # handshake-era clients
        self._tools: dict[str, ToolEntry] = {}
        self._seen_clients: set[tuple[object, object, object]] = set()
        self._dirty = threading.Event()
        self.server = _Server(
            "htb-runtime",
            version="0.1.0",
            instructions=INSTRUCTIONS,
            lifespan=self._lifespan,
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
            on_subscriptions_listen=ListenHandler(self.bus),
        )
        self.server.add_notification_handler(
            "notifications/initialized", types.NotificationParams, self._on_initialized
        )
        registry.on_change(lambda _domain: self._dirty.set())
        self.rebuild()

    # ---- tool table --------------------------------------------------------

    def rebuild(self) -> None:
        tools: dict[str, ToolEntry] = {}
        if self.tools_mode == "generic":
            # Actions are reached through find_actions / run_action; the tool list doesn't depend on the cards.
            self._tools = tools
            log.info("serving generic tools over %d sites", len(self.registry.active_cards()))
            return
        for domain, stored in sorted(self.registry.active_cards().items()):
            card = stored.card
            for action in card.actions:
                name = f"{card.tool_prefix}_{action.name}"
                if name in tools:
                    log.warning("tool %s from %s clashes with %s; skipped", name, domain, tools[name].domain)
                    continue
                tool = types.Tool(
                    name=name,
                    description=tool_description(action),
                    input_schema=input_schema(action.params, action.requires_confirmation),
                )
                tools[name] = ToolEntry(domain, action.name, tool)
        self._tools = tools
        log.info("serving %d tools", len(tools))

    def tool_names(self) -> list[str]:
        return list(self._tools)

    async def reload_if_changed(self) -> bool:
        """Pick up registry changes (including from other processes) and notify clients."""
        self.registry.refresh()
        if not self._dirty.is_set():
            return False
        self._dirty.clear()
        self.rebuild()
        if self.tools_mode == "per_action":  # generic tools don't change when cards do
            await self.notify_tools_changed()
        return True

    async def notify_tools_changed(self) -> None:
        await self.bus.publish(ToolsListChanged())
        for conn in list(self._legacy_connections):
            await conn.send_tool_list_changed()  # best-effort; never raises

    # ---- MCP handlers ------------------------------------------------------

    def _track(self, ctx: Any) -> None:
        """Remember handshake-era connections so list-changed can reach their standalone stream.

        `ctx.session` is a per-request proxy; the long-lived per-client object is its `Connection`,
        which the 2.3.0 SDK only exposes as `ServerSession._connection` (see DECISIONS.md).
        """
        conn = getattr(getattr(ctx, "session", None), "_connection", None)
        if isinstance(conn, Connection) and conn.protocol_version not in MODERN_PROTOCOL_VERSIONS:
            self._legacy_connections.add(conn)

    async def _on_initialized(self, ctx: Any, params: types.NotificationParams) -> None:
        self._track(ctx)

    def _log_client(self, ctx: Any) -> None:
        """Log each client's identity and elicitation support once (decides how sign-in links are shown)."""
        session = getattr(ctx, "session", None)
        if session is None:
            return
        try:
            caps = session.client_capabilities
            params = session.client_params
            info = getattr(params, "client_info", None) if params is not None else None
            key = (getattr(info, "name", None), getattr(info, "version", None), session.protocol_version)
        except Exception:
            return
        if key in self._seen_clients:
            return
        self._seen_clients.add(key)
        elicitation = getattr(caps, "elicitation", None) if caps is not None else None
        log.info(
            "client %s %s (protocol %s), elicitation: %s",
            key[0], key[1], key[2],
            elicitation.model_dump(exclude_none=True) if elicitation is not None else None,
        )

    async def _list_tools(self, ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        self._track(ctx)
        self._log_client(ctx)
        tools = [e.tool for e in self._tools.values()]
        if self.tools_mode == "generic":
            tools = [FIND_ACTIONS_TOOL, RUN_ACTION_TOOL]
        if self.healer is not None:
            tools.append(HEAL_STATUS_TOOL)
        if self.connections is not None:
            tools.extend(CONNECTION_TOOLS.values())
        return types.ListToolsResult(tools=tools)

    async def _call_tool(
        self, ctx: Any, params: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        self._track(ctx)
        if params.name in CONNECTION_TOOLS and self.connections is not None:
            return await self._connection_tool(params.name, dict(params.arguments or {}), ctx, params)
        if params.name == HEAL_STATUS_TOOL.name and self.healer is not None:
            return self._heal_status(dict(params.arguments or {}))
        if self.tools_mode == "generic" and params.name == FIND_ACTIONS_TOOL.name:
            return self._find_actions(dict(params.arguments or {}))
        if self.tools_mode == "generic" and params.name == RUN_ACTION_TOOL.name:
            return await self._run_action(dict(params.arguments or {}))
        entry = self._tools.get(params.name)
        stored = self.registry.get(entry.domain) if entry else None
        if entry is None or stored is None or stored.card.action(entry.action) is None:
            return _error_result(f"unknown tool {params.name!r}")
        return await self._execute(stored, entry.action, dict(params.arguments or {}), params.name)

    def _find_actions(self, args: dict[str, Any]) -> types.CallToolResult:
        query, site = args.get("query"), args.get("site")
        if (query is not None and not isinstance(query, str)) or (site is not None and not isinstance(site, str)):
            return _error_result("query and site must be strings")
        out = find_actions(self.registry.active_cards(), query or None, site or None)
        if "error" in out:
            return _error_result(str(out["error"]))
        return _json_result(out)

    async def _run_action(self, args: dict[str, Any]) -> types.CallToolResult:
        site, action, params = args.get("site"), args.get("action"), args.get("params", {})
        if not isinstance(site, str) or not isinstance(action, str):
            return _error_result("site and action (strings) are required; use find_actions to look them up")
        if not isinstance(params, dict):
            return _error_result("params must be an object")
        stored = self.registry.get(site)
        if stored is None:
            return _error_result(f"unknown site {site!r}; use find_actions to see the available sites")
        if stored.card.action(action) is None:
            return _error_result(f"{site} has no action {action!r}; use find_actions(site={site!r}) to list them")
        call_args = dict(params)
        confirmation_id = args.get("confirmation_id")
        if confirmation_id is not None:
            call_args["_confirmation_id"] = confirmation_id
        return await self._execute(stored, action, call_args, f"{site}/{action}")

    async def _execute(
        self, stored: StoredCard, action: str, args: dict[str, Any], label: str
    ) -> types.CallToolResult:
        try:
            env = await self.executor.run(stored, action, args, user=DEFAULT_USER)
        except Exception:
            log.exception("executor crashed on %s", label)
            return _error_result(f"internal error running {label}")
        payload = env.model_dump(mode="json")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload))],
            structured_content=payload,
            is_error=False,  # the envelope's `status` carries the outcome, including failures
        )

    def _heal_status(self, args: dict[str, Any]) -> types.CallToolResult:
        assert self.healer is not None
        job_id = args.get("job_id")
        if not isinstance(job_id, str):
            return _error_result("job_id (string) is required")
        status = self.healer.heal_status(job_id)
        if status is None:
            return _error_result(f"no heal job {job_id!r}")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(status))], structured_content=status
        )

    async def _connection_tool(
        self, name: str, args: dict[str, Any], ctx: Any = None, params: types.CallToolRequestParams | None = None
    ) -> types.CallToolResult | types.InputRequiredResult:
        assert self.connections is not None
        domain = args.get("domain")
        if not isinstance(domain, str) or not domain:
            return _error_result("domain (string) is required")
        if name == "start_connection" and params is not None and params.request_state:
            return await self._finish_url_sign_in(params)
        if name == "start_connection":
            provider = args.get("provider")
            out = self.connections.start(DEFAULT_USER, domain, provider if isinstance(provider, str) else None)
            if out.get("status") == "pending" and "url" in out and _supports_url_elicitation(ctx):
                # Let the client itself show the link (MCP URL elicitation), so it never depends on the model
                # copying it into its reply. The client retries this call once the user has responded.
                token = str(out["url"]).rsplit("/", 1)[1]
                request = types.ElicitRequest(
                    params=types.ElicitRequestURLParams(
                        mode="url",
                        message=f"Sign in to {domain} to continue. You sign in on {domain}'s own page; "
                        "your password isn't shared with the assistant.",
                        url=str(out["url"]),
                        elicitation_id=token,
                    )
                )
                log.info("start_connection: asking the client to show the sign-in link (URL elicitation)")
                return types.InputRequiredResult(input_requests={SIGN_IN_REQUEST: request}, request_state=token)
        elif name == "poll_connection":
            wait = args.get("wait_seconds", 25)
            wait_s = float(wait) if isinstance(wait, (int, float)) and not isinstance(wait, bool) else 25.0
            out = await self.connections.poll(DEFAULT_USER, domain, wait_s)
        elif name == "disconnect":
            provider = args.get("provider")
            out = await self.connections.disconnect(
                DEFAULT_USER, domain, provider if isinstance(provider, str) else None
            )
        else:
            out = self.connections.status(DEFAULT_USER, domain)
        if "error" in out:
            return _error_result(str(out["error"]))
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(out))], structured_content=out)

    async def _finish_url_sign_in(self, params: types.CallToolRequestParams) -> types.CallToolResult:
        """The client's retry after showing the sign-in link: wait for the sign-in, then report."""
        assert self.connections is not None
        log.info("start_connection: client retried after the sign-in prompt: %s",
                 {k: getattr(v, "action", type(v).__name__) for k, v in (params.input_responses or {}).items()})
        conn = self.connections.by_token(params.request_state or "")
        if conn is None:
            return _error_result("unknown or expired sign-in; call start_connection again")
        response = (params.input_responses or {}).get(SIGN_IN_REQUEST)
        action = getattr(response, "action", None)
        if action == "decline":  # the user said no
            out = await self.connections.poll(conn.user, conn.domain)
            out.pop("url", None)
            out["user_message"] = f"Signing in to {conn.domain} was cancelled."
            out["hint"] = "the user declined to open the sign-in page; ask whether they want to sign in"
            return _json_result(out)
        if action != "accept":
            # "cancel" (or anything else): the app couldn't show the prompt (some clients cancel URL requests
            # automatically). Fall back to the agent showing the link and waiting in the same turn.
            out = await self.connections.poll(conn.user, conn.domain)
            out["hint"] = (
                "Your app couldn't show the sign-in link to the user. In this same reply, write the user_message "
                "exactly as given (a markdown link), then immediately call poll_connection; don't end your turn, it "
                "waits for them to sign in."
            )
            return _json_result(out)
        out = await self.connections.poll(conn.user, conn.domain, wait_s=40, opened=True)
        return _json_result(out)

    @asynccontextmanager
    async def _lifespan(self, _server: Server[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._poll)
            if self.healer is not None:
                tg.start_soon(self.healer.run_forever)
                tg.start_soon(self.healer.health_checks_forever)
            if self.pool is not None:
                tg.start_soon(self.pool.reap_forever)
            if self.connections is not None:
                tg.start_soon(self.connections.keepalive_forever, self.keepalive_s)
                if self.connections.neko is not None:
                    if not await self.connections.neko.available():
                        log.warning("Neko image not available (docker pull %s); 'log in here' is off",
                                    self.connections.neko.config.image)
                        self.connections.neko = None
                    else:
                        removed = await self.connections.neko.cleanup_stale()
                        if removed:
                            log.info("removed %d leftover login browsers", removed)
            try:
                yield {}
            finally:
                tg.cancel_scope.cancel()
                if self.connections is not None:
                    await self.connections.close()
                if self.pool is not None:
                    await self.pool.close()

    async def _poll(self) -> None:
        while True:
            await anyio.sleep(self.poll_interval_s)
            try:
                await self.reload_if_changed()
            except Exception:
                log.exception("registry reload failed")

    # ---- HTTP --------------------------------------------------------------

    def asgi_app(self, api_key: str, host: str = "127.0.0.1") -> ASGIApp:
        extra = connect_routes(self.connections) if self.connections is not None else []
        inner = self.server.streamable_http_app(host=host, custom_starlette_routes=extra)  # type: ignore[arg-type]
        return BearerAuth(inner, api_key, exempt_prefixes=("/connect/", "/neko/"))


SIGN_IN_REQUEST = "sign_in"


def _supports_url_elicitation(ctx: Any) -> bool:
    """A 2026-07-28 client that declared URL elicitation (Claude Code does)."""
    session = getattr(ctx, "session", None)
    if session is None:
        return False
    try:
        caps = session.client_capabilities
        modern = session.protocol_version in MODERN_PROTOCOL_VERSIONS
    except Exception:
        return False
    elicitation = getattr(caps, "elicitation", None) if caps is not None else None
    return bool(modern and elicitation is not None and getattr(elicitation, "url", None) is not None)


def _json_result(out: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(out))], structured_content=out)


def _error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], is_error=True)


class BearerAuth:
    """Rejects HTTP requests without `Authorization: Bearer <api key>`."""

    def __init__(self, app: ASGIApp, api_key: str, exempt_prefixes: tuple[str, ...] = ()) -> None:
        if not api_key:
            raise ValueError("GATEWAY_API_KEY must be set")
        self.app = app
        self._key = api_key.encode()
        self._exempt = exempt_prefixes  # routes with their own credential (the login link's token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        if scope["type"] in ("http", "websocket") and not path.startswith(self._exempt):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4401})
                return
            auth = dict(scope.get("headers") or []).get(b"authorization", b"")
            scheme, _, token = auth.partition(b" ")
            if scheme.lower() != b"bearer" or not hmac.compare_digest(token.strip(), self._key):
                body = json.dumps({"error": "missing or invalid bearer token"}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b'Bearer realm="htb-runtime"'),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


# ---- entry point -----------------------------------------------------------


def load_into_registry(registry: Registry, path: Path) -> int | None:
    """Save the card at `path` unless the active version is identical. Returns the new version or None."""
    raw = json.loads(path.read_text())
    card = load_card(raw)
    active = registry.get(card.domain)
    if active is not None and active.card.to_json_dict() == card.to_json_dict():
        return None
    return registry.save(card, note=f"loaded from {path.name}")


def load_remapper(spec: str | None) -> Remapper | None:
    """`module.path:Name` -> an instance of Name (or Name itself if it is already an instance)."""
    if not spec:
        return None
    module_name, _, attr = spec.partition(":")
    obj = getattr(importlib.import_module(module_name), attr)
    remapper: Remapper = obj() if isinstance(obj, type) else obj
    log.info("using remapper %s", spec)
    return remapper


class _DropWebSocketLines(logging.Filter):
    """uvicorn logs each WebSocket handshake with its full URL (login tokens, Neko session tokens)."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not (message.startswith(("connection open", "connection closed")) or '"WebSocket ' in message)


def main() -> None:
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description="Run the MCP gateway")
    parser.add_argument("--load", action="append", default=[], type=Path, help="card JSON to save on startup")
    parser.add_argument("--host", default=os.environ.get("GATEWAY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("GATEWAY_PORT", "8765")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # URL-level request logs would record login tokens (/connect/<token>), the login browser's password and users'
    # query parameters, so they stay off; the runtime logs its own redacted events.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").addFilter(_DropWebSocketLines())

    db_path = os.environ.get("REGISTRY_DB", "./runtime.db")
    registry = Registry(db_path)
    for path in args.load:
        version = load_into_registry(registry, path)
        log.info("%s: %s", path, f"saved as version {version}" if version else "unchanged")
    session_store = EncryptedSessionStore(db_path, os.environ.get("SESSION_KEY", ""))
    pool = BrowserPool(
        BrowserConfig(
            profiles_dir=Path(os.environ.get("PROFILES_DIR", "./profiles")),
            channel=os.environ.get("BROWSER_CHANNEL", "chrome"),
            headless=os.environ.get("BROWSER_HEADLESS", "1") != "0",
            idle_s=float(os.environ.get("BROWSER_IDLE_S", "600")),
        )
    )
    tiers = default_tiers()
    tiers["browser"] = BrowserTier(pool)
    executor = Executor(
        ChainedSessionStore(session_store, DevEnvSessionStore(os.environ.get("DEV_SESSION_COOKIES"))),
        Confirmer(os.environ.get("CONFIRMATION_SECRET", "")),
        tiers=tiers,
    )
    public_url = os.environ.get("PUBLIC_BASE_URL", f"http://{args.host}:{args.port}")
    neko = None
    if os.environ.get("NEKO_ENABLED", "1") != "0":
        low, _, high = os.environ.get("NEKO_UDP_PORTS", "52000-52099").partition("-")
        neko = NekoManager(
            NekoConfig(
                image=os.environ.get("NEKO_IMAGE", "ghcr.io/m1k1o/neko/chromium:latest"),
                public_ip=os.environ.get("NEKO_PUBLIC_IP", "auto"),
                udp_ports=(int(low), int(high or low)),
                ice_servers=os.environ.get("NEKO_ICE_SERVERS", ""),
            )
        )
    connections = ConnectionManager(
        registry, session_store, executor, public_url, pool=pool,
        timeout_s=float(os.environ.get("CONNECT_TIMEOUT_S", "600")), neko=neko,
    )
    config = HealConfig(
        retry_after_s=int(os.environ.get("HEAL_RETRY_AFTER_S", "30")),
        health_interval_s=float(os.environ.get("HEALTH_CHECK_INTERVAL_S", "0")),
        health_disabled_domains=frozenset(
            d.strip() for d in os.environ.get("HEALTH_CHECK_DISABLED_DOMAINS", "").split(",") if d.strip()
        ),
    )
    healer = HealManager(registry, StateStore(db_path), executor, load_remapper(os.environ.get("REMAPPER")), config)
    gateway = Gateway(
        registry, executor, healer=healer, connections=connections, pool=pool,
        keepalive_s=float(os.environ.get("KEEPALIVE_INTERVAL_S", "900")),
        tools_mode=os.environ.get("TOOLS_MODE", "generic"),
    )
    app = gateway.asgi_app(os.environ.get("GATEWAY_API_KEY", ""), host=args.host)
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
