"""MCP server: one tool per active card action, served over Streamable HTTP with Bearer-key auth.

Run:  uv run python -m runtime.gateway --load tests/fixture_site.json
"""

from __future__ import annotations

import argparse
import hmac
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

from runtime.confirm import Confirmer
from runtime.executor import Executor
from runtime.registry import Registry
from runtime.sessions import DevEnvSessionStore
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
    def __init__(self, registry: Registry, executor: Executor, poll_interval_s: float = 1.0) -> None:
        self.registry = registry
        self.executor = executor
        self.poll_interval_s = poll_interval_s
        self.bus = InMemorySubscriptionBus()  # 2026-07-28 clients: subscriptions/listen
        self._legacy_connections: weakref.WeakSet[Connection] = weakref.WeakSet()  # handshake-era clients
        self._tools: dict[str, ToolEntry] = {}
        self._dirty = threading.Event()
        self.server = _Server(
            "htb-runtime",
            version="0.1.0",
            instructions="Tools generated from website action cards. Every tool returns a JSON result envelope; "
            "check its `status` field (SUCCESS, EMPTY, NEEDS_CONFIRMATION, ...).",
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

    async def _list_tools(self, ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        self._track(ctx)
        return types.ListToolsResult(tools=[e.tool for e in self._tools.values()])

    async def _call_tool(self, ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        self._track(ctx)
        entry = self._tools.get(params.name)
        stored = self.registry.get(entry.domain) if entry else None
        if entry is None or stored is None or stored.card.action(entry.action) is None:
            return _error_result(f"unknown tool {params.name!r}")
        try:
            env = await self.executor.run(stored, entry.action, dict(params.arguments or {}), user=DEFAULT_USER)
        except Exception:
            log.exception("executor crashed on %s", params.name)
            return _error_result(f"internal error running {params.name}")
        payload = env.model_dump(mode="json")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload))],
            structured_content=payload,
            is_error=False,  # the envelope's `status` carries the outcome, including failures
        )

    @asynccontextmanager
    async def _lifespan(self, _server: Server[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._poll)
            yield {}
            tg.cancel_scope.cancel()

    async def _poll(self) -> None:
        while True:
            await anyio.sleep(self.poll_interval_s)
            try:
                await self.reload_if_changed()
            except Exception:
                log.exception("registry reload failed")

    # ---- HTTP --------------------------------------------------------------

    def asgi_app(self, api_key: str, host: str = "127.0.0.1") -> ASGIApp:
        inner = self.server.streamable_http_app(host=host)
        return BearerAuth(inner, api_key)


def _error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], is_error=True)


class BearerAuth:
    """Rejects HTTP requests without `Authorization: Bearer <api key>`."""

    def __init__(self, app: ASGIApp, api_key: str) -> None:
        if not api_key:
            raise ValueError("GATEWAY_API_KEY must be set")
        self.app = app
        self._key = api_key.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
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

    registry = Registry(os.environ.get("REGISTRY_DB", "./runtime.db"))
    for path in args.load:
        version = load_into_registry(registry, path)
        log.info("%s: %s", path, f"saved as version {version}" if version else "unchanged")
    executor = Executor(
        DevEnvSessionStore(os.environ.get("DEV_SESSION_COOKIES")),
        Confirmer(os.environ.get("CONFIRMATION_SECRET", "")),
    )
    gateway = Gateway(registry, executor)
    app = gateway.asgi_app(os.environ.get("GATEWAY_API_KEY", ""), host=args.host)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
