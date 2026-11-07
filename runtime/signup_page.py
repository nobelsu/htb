"""The sign-up page: people with the invite code (SIGNUP_CODE) get their own account, then see how to connect
Claude. Off (404) when no code is set. The invite code is its credential, so the gateway's Bearer check skips
/signup; wrong codes are rate limited per address. Drawn in the Spine design (runtime/ui.py).

GET  /signup   the form (invite code, username, email, password)
POST /signup   create the user; shows the connector URL and the Claude Code command (with the key) once
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from collections import deque
from collections.abc import Callable
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from runtime.keys import KeyStore, KeyStoreError, check_email, check_name, mcp_add_command
from runtime.ui import COPY_SCRIPT, HEADERS, bar_button, bar_link, esc, field, icon, notice, page

log = logging.getLogger("runtime.signup")
MAX_BODY = 4096
WRONG_CODE_LIMIT = 10  # failed attempts per address...
WRONG_CODE_WINDOW_S = 600.0  # ...within this many seconds, then every attempt is refused until they age out

__all__ = ["HEADERS", "AttemptLimiter", "routes"]


def form_page(error: str = "", name: str = "", email: str = "", status: int = 200) -> Response:
    body = (
        notice(error)
        + '<form class="stack" method="post" action="/signup">'
        + field("code", "invite code", code=True)
        + field("name", "username", value=name, autocomplete="username")
        + field("email", "email", "email", value=email, autocomplete="email")
        + field("password", "password", "password", autocomplete="new-password")
        + bar_button("Create account")
        + "</form>"
    )
    return page(
        title="Create account", spine="Join", body=body, status=status,
        nav=['<a href="/account/login">Sign in</a>'],
    )


def copy_row(title: str, shown: str, value: str, label: str) -> str:
    return (
        f'<div class="row"><span class="big">{esc(title)}</span>'
        f'<span style="display:flex;align-items:center;gap:16px;min-width:0;max-width:440px">'
        f'<code class="mono">{esc(shown)}</code>'
        f'<button class="sq" type="button" data-copy="{esc(value)}" aria-label="{esc(label)}">{icon("copy", 22)}</button>'
        "</span></div>"
    )


def done_page(name: str, base: str, key: str) -> Response:
    url = base.rstrip("/") + "/mcp"
    command = mcp_add_command(base, key)
    body = (
        f'<p class="headline" style="padding-top:72px">{esc(name)}</p><div class="rule"></div>'
        + copy_row("Claude", url.split("://", 1)[-1], url, "Copy the connector URL")
        + copy_row("Claude Code", "claude mcp add", command, "Copy the Claude Code command, which holds your key")
        + bar_link("Go to your account", "/account")
    )
    return page(title="Account created", spine="Ready", body=body, script=COPY_SCRIPT)


class AttemptLimiter:
    """Failed attempts (invite codes, passwords, keys) per client address, so they can't be guessed."""

    def __init__(self) -> None:
        self._seen: dict[str, deque[float]] = {}

    def _recent(self, addr: str) -> deque[float]:
        times = self._seen.setdefault(addr, deque())
        cutoff = time.monotonic() - WRONG_CODE_WINDOW_S
        while times and times[0] < cutoff:
            times.popleft()
        return times

    def blocked(self, addr: str) -> bool:
        return len(self._recent(addr)) >= WRONG_CODE_LIMIT

    def add(self, addr: str) -> None:
        self._recent(addr).append(time.monotonic())


def routes(
    keys: KeyStore,
    code: str,
    public_url: Callable[[], str | None],
    start_session: Callable[[Response, str], Response] | None = None,
) -> list[BaseRoute]:
    wrong = AttemptLimiter()

    async def show(request: Request) -> Response:
        if not code:
            return Response("Not Found", status_code=404)
        return form_page()

    async def submit(request: Request) -> Response:
        if not code:
            return Response("Not Found", status_code=404)
        addr = request.client.host if request.client else "?"
        if wrong.blocked(addr):
            return form_page("Too many wrong invite codes. Try again in a few minutes.", status=429)
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return form_page("That form is too large.", status=413)
        fields = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        given = fields.get("code", "").strip()
        name, email = fields.get("name", "").strip(), fields.get("email", "").strip()
        if not hmac.compare_digest(given.encode(), code.encode()):
            wrong.add(addr)
            return form_page("That invite code isn't right.", name, email, 403)
        try:
            check_name(name)
            check_email(email)
            key = await asyncio.to_thread(keys.create, name, email, fields.get("password", ""))
        except KeyStoreError as e:
            status = 409 if "taken" in str(e) or "already" in str(e) else 400
            return form_page(str(e)[:1].upper() + str(e)[1:] + ".", name, email, status)
        log.info("new user signed up: %s", name)  # never the key, the code or the email
        response = done_page(name, public_url() or str(request.base_url), key)
        return start_session(response, name) if start_session is not None else response

    return [Route("/signup", show, methods=["GET"]), Route("/signup", submit, methods=["POST"])]
