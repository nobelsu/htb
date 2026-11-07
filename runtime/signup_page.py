"""The sign-up page: people with the invite code (SIGNUP_CODE) get their own API key, shown once with the line
that connects Claude Code. Off (404) when no code is set. The invite code is its credential, so the gateway's Bearer
check skips /signup; wrong codes are rate limited per address.

GET  /signup   the form (invite code, name, email)
POST /signup   create the user; shows their key once
"""

from __future__ import annotations

import hmac
import html
import logging
import time
from collections import deque
from collections.abc import Callable
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import BaseRoute, Route

from runtime.keys import KeyStore, KeyStoreError, check_email, check_name, mcp_add_command

log = logging.getLogger("runtime.signup")
HEADERS = {"cache-control": "no-store", "referrer-policy": "no-referrer", "x-frame-options": "DENY"}
MAX_BODY = 4096
WRONG_CODE_LIMIT = 10  # wrong invite codes per address...
WRONG_CODE_WINDOW_S = 600.0  # ...within this many seconds, then every attempt is refused until they age out

STYLE = """
  :root { color-scheme: light dark; --bg:#f7f7f5; --fg:#1c1c1a; --muted:#66665f; --line:#d9d9d4; --accent:#2f5fb3;
          --field:#ffffff; --ok:#1f7a4d; --ok-bg:#e6f3ec; --bad:#b3261e; --code:#efefeb; }
  @media (prefers-color-scheme: dark) { :root { --bg:#171716; --fg:#ececea; --muted:#a0a09a; --line:#33332f;
          --accent:#7aa2e8; --field:#1f1f1e; --ok:#5fc48f; --ok-bg:#1b2e24; --bad:#f2827a; --code:#232322; } }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 system-ui, sans-serif; }
  main { max-width:640px; margin:40px auto; padding:0 16px; }
  h1 { font-size:22px; margin:0 0 4px; } p { margin:0 0 12px; } .muted { color:var(--muted); }
  form { display:grid; gap:14px; margin-top:20px; }
  label { display:grid; gap:4px; font-weight:600; font-size:14px; }
  label span { font-weight:400; color:var(--muted); }
  input { font:inherit; padding:10px 12px; border:1px solid var(--line); border-radius:6px; background:var(--field);
          color:var(--fg); width:100%; }
  button { font:inherit; padding:10px 18px; border-radius:6px; border:0; background:var(--accent); color:#fff;
           cursor:pointer; justify-self:start; }
  .error { color:var(--bad); margin-top:16px; }
  .done { margin-top:20px; padding:20px; border-radius:10px; background:var(--ok-bg);
          border:1px solid color-mix(in srgb, var(--ok) 35%, transparent); }
  .done h2 { margin:0 0 8px; font-size:20px; color:var(--ok); }
  pre { background:var(--code); border:1px solid var(--line); border-radius:6px; padding:10px 12px; margin:6px 0 4px;
        white-space:pre-wrap; word-break:break-all; font:13px/1.45 ui-monospace, Menlo, monospace; }
  .copy { padding:6px 12px; font-size:14px; }
  ol { padding-left:20px; } li { margin-bottom:8px; }
"""

FORM = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Get an API key</title><style>__STYLE__</style></head>
<body><main>
<h1>Get an API key</h1>
<p class="muted">Your key lets your AI agent use websites through this server, as you: your own sign-ins, kept
separate from everyone else's.</p>
__ERROR__
<form method="post" action="/signup" autocomplete="off">
  <label>Invite code <span>from whoever runs this server</span>
    <input name="code" type="password" required autocomplete="off"></label>
  <label>Name <span>lowercase letters, digits, - or _ (for example "alice")</span>
    <input name="name" required minlength="2" maxlength="32" pattern="[a-z0-9][a-z0-9_-]{1,31}" value="__NAME__"></label>
  <label>Email
    <input name="email" type="email" required maxlength="254" value="__EMAIL__"></label>
  <button type="submit">Get my key</button>
</form>
</main></body></html>"""

DONE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your API key</title><style>__STYLE__</style></head>
<body><main>
<h1>Welcome, __NAME__</h1>
<div class="done">
  <h2>Your API key</h2>
  <p>This is the only time it's shown. Keep it secret: anyone with it can act as you.</p>
  <pre id="key">__KEY__</pre>
  <button class="copy" type="button" data-copy="key">Copy key</button>
</div>
<h2 style="font-size:18px;margin-top:28px">Connect Claude Code</h2>
<ol>
  <li>Run this in a terminal:
    <pre id="cmd">__CMD__</pre>
    <button class="copy" type="button" data-copy="cmd">Copy command</button></li>
  <li>Start a new Claude Code session.</li>
  <li>Ask it what it can do, for example "what sites can you use?"</li>
</ol>
</main>
<script>
for (const b of document.querySelectorAll("button[data-copy]")) {
  b.addEventListener("click", async () => {
    await navigator.clipboard.writeText(document.getElementById(b.dataset.copy).textContent);
    const label = b.textContent; b.textContent = "Copied"; setTimeout(() => (b.textContent = label), 1500);
  });
}
</script></body></html>"""


def _form(error: str = "", name: str = "", email: str = "") -> str:
    shown = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    return (
        FORM.replace("__STYLE__", STYLE)
        .replace("__ERROR__", shown)
        .replace("__NAME__", html.escape(name))
        .replace("__EMAIL__", html.escape(email))
    )


class _WrongCodes:
    """Wrong invite codes per client address, so the code can't be guessed."""

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


def routes(keys: KeyStore, code: str, public_url: Callable[[], str | None]) -> list[BaseRoute]:
    wrong = _WrongCodes()

    def page(body: str, status: int = 200) -> Response:
        return HTMLResponse(body, status_code=status, headers=HEADERS)

    async def show(request: Request) -> Response:
        if not code:
            return Response("Not Found", status_code=404)
        return page(_form())

    async def submit(request: Request) -> Response:
        if not code:
            return Response("Not Found", status_code=404)
        addr = request.client.host if request.client else "?"
        if wrong.blocked(addr):
            return page(_form("Too many wrong invite codes. Try again in a few minutes."), 429)
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return page(_form("That form is too large."), 413)
        fields = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        given = fields.get("code", "").strip()
        name, email = fields.get("name", "").strip(), fields.get("email", "").strip()
        if not hmac.compare_digest(given.encode(), code.encode()):
            wrong.add(addr)
            return page(_form("That invite code isn't right.", name, email), 403)
        try:
            check_name(name)
            check_email(email)
            key = keys.create(name, email)
        except KeyStoreError as e:
            status = 409 if "taken" in str(e) else 400
            return page(_form(str(e)[:1].upper() + str(e)[1:] + ".", name, email), status)
        log.info("new user signed up: %s", name)  # never the key, the code or the email
        base = public_url() or str(request.base_url)
        done = (
            DONE.replace("__STYLE__", STYLE)
            .replace("__NAME__", html.escape(name))
            .replace("__KEY__", html.escape(key))
            .replace("__CMD__", html.escape(mcp_add_command(base, key)))
        )
        return page(done)

    return [Route("/signup", show, methods=["GET"]), Route("/signup", submit, methods=["POST"])]
