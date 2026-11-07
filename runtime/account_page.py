"""The sign-in page (where regular Claude's "Connect" button lands) and the account page.

GET/POST /oauth/login?request=...   sign in for an app (OAuth): password, API key, or a new account
GET/POST /account/login             sign in to the account page
GET      /account                   waiting sign-ins (live), connected sites, connected apps, API key
GET      /account/status            the live part, as JSON
POST     /account/disconnect        sign out of a site
POST     /account/key               a new API key (the old one stops working)
POST     /account/apps/revoke       disconnect an app (its OAuth tokens)
POST     /account/logout

The account page is a cookie session (HttpOnly, SameSite=Lax); POSTs from another site are refused.
"""

from __future__ import annotations

import asyncio
import hmac
import html
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Route

from runtime.connect import ConnectionManager
from runtime.keys import ENV_KEY_USER, KeyStore, KeyStoreError, mcp_add_command
from runtime.oauth import ACCOUNT_SESSION_TTL_S, OAuth, PendingSignIn
from runtime.session_store import EncryptedSessionStore
from runtime.signup_page import HEADERS, STYLE, AttemptLimiter

log = logging.getLogger("runtime.account")
COOKIE = "htb_account"
MAX_BODY = 4096

EXTRA_STYLE = """
  .card { margin-top:22px; padding:18px 20px; border:1px solid var(--line); border-radius:10px; }
  .card h2 { margin:0 0 10px; font-size:17px; }
  .row { display:flex; gap:12px; align-items:center; justify-content:space-between; padding:8px 0;
         border-top:1px solid var(--line); flex-wrap:wrap; }
  .row:first-of-type { border-top:0; }
  .row form { margin:0; display:inline; }
  .small { font-size:14px; } .empty { color:var(--muted); font-size:14px; margin:0; }
  .linkbtn { background:none; color:var(--accent); padding:0; border:0; text-decoration:underline; cursor:pointer; }
  .secondary { background:transparent; color:var(--accent); border:1px solid var(--line); }
  .waiting { background:var(--ok-bg); border-color:color-mix(in srgb, var(--ok) 35%, transparent); }
  a.button { display:inline-block; padding:8px 14px; border-radius:6px; background:var(--accent); color:#fff;
             text-decoration:none; }
  details { margin-top:16px; } summary { cursor:pointer; color:var(--accent); }
  header { display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; }
"""


def _page(title: str, body: str, status: int = 200, script: str = "") -> HTMLResponse:
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{STYLE}{EXTRA_STYLE}</style></head>"
        f"<body><main>{body}</main>{script}</body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=HEADERS)


def _when(ts: float | str | None) -> str:
    if ts is None:
        return ""
    if isinstance(ts, str):
        return ts[:10]
    return datetime.fromtimestamp(ts, UTC).strftime("%d %b %Y")


def _error(text: str) -> str:
    return f'<p class="error" role="alert">{html.escape(text)}</p>' if text else ""


def _sign_in_body(
    *, action: str, request_id: str, heading: str, intro: str, error: str, signed_in_as: str | None,
    signup_open: bool, login: str = "",
) -> str:
    hidden = f'<input type="hidden" name="request" value="{html.escape(request_id)}">' if request_id else ""
    parts = [f"<h1>{html.escape(heading)}</h1>", f'<p class="muted">{intro}</p>', _error(error)]
    if signed_in_as:
        parts.append(
            f'<form method="post" action="{action}">{hidden}<input type="hidden" name="action" value="continue">'
            f'<p>Signed in as <strong>{html.escape(signed_in_as)}</strong>.</p>'
            '<button type="submit">Continue</button></form>'
            '<p class="small muted" style="margin-top:12px">Not you? Sign in with another account below.</p>'
        )
    parts.append(
        f'<form method="post" action="{action}">{hidden}<input type="hidden" name="action" value="password">'
        f'<label>Name or email<input name="login" required autocomplete="username" value="{html.escape(login)}"></label>'
        '<label>Password<input name="password" type="password" required autocomplete="current-password"></label>'
        "<button type=\"submit\">Sign in</button></form>"
        f'<details><summary>Sign in with an API key instead</summary><form method="post" action="{action}">{hidden}'
        '<input type="hidden" name="action" value="key">'
        '<label>API key<input name="key" type="password" required autocomplete="off"></label>'
        '<button type="submit">Sign in with key</button></form></details>'
    )
    if signup_open:
        parts.append(
            f'<details><summary>New here? Create an account</summary><form method="post" action="{action}">{hidden}'
            '<input type="hidden" name="action" value="signup">'
            '<label>Invite code <span>from whoever runs this server</span>'
            '<input name="code" type="password" required autocomplete="off"></label>'
            '<label>Name <span>lowercase letters, digits, - or _</span>'
            '<input name="name" required minlength="2" maxlength="32" pattern="[a-z0-9][a-z0-9_-]{1,31}"></label>'
            '<label>Email<input name="email" type="email" required maxlength="254"></label>'
            '<label>Password <span>at least 8 characters</span>'
            '<input name="password" type="password" required minlength="8" autocomplete="new-password"></label>'
            '<button type="submit">Create account</button></form></details>'
        )
    if request_id:
        parts.append(
            f'<form method="post" action="{action}" style="margin-top:20px">{hidden}'
            '<input type="hidden" name="action" value="deny">'
            '<button class="linkbtn" type="submit">Cancel and go back</button></form>'
        )
    return "".join(parts)


ACCOUNT_SCRIPT = """<script>
const seen = new Set(__SEEN__);
const list = document.getElementById("waiting-list");
const card = document.getElementById("waiting");
function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
function render(pending) {
  card.classList.toggle("waiting", pending.length > 0);
  list.innerHTML = pending.length ? pending.map(p =>
    `<div class="row"><span>Your agent needs you to sign in to <strong>${esc(p.domain)}</strong>
     <span class="small muted">(link works for ${p.minutes_left} more min)</span></span>
     <a class="button" href="${esc(p.url)}" target="_blank" rel="noopener">Sign in</a></div>`).join("")
    : '<p class="empty">Nothing waiting. When your agent needs you to sign in to a site, it shows up here.</p>';
  for (const p of pending) {
    if (!seen.has(p.url) && "Notification" in window && Notification.permission === "granted") {
      const n = new Notification("Sign in to " + p.domain, {body: "Your agent is waiting for you."});
      n.onclick = () => window.open(p.url, "_blank");
    }
    seen.add(p.url);
  }
}
async function poll() {
  try {
    const r = await fetch("/account/status", {cache: "no-store"});
    if (r.status === 401) { location.href = "/account/login"; return; }
    render((await r.json()).pending);
  } catch (e) {}
  setTimeout(poll, 2000);
}
const notify = document.getElementById("notify");
if (notify) {
  if (!("Notification" in window) || Notification.permission !== "default") notify.hidden = true;
  notify.addEventListener("click", async () => { await Notification.requestPermission(); notify.hidden = true; });
}
for (const b of document.querySelectorAll("button[data-copy]")) {
  b.addEventListener("click", async () => {
    await navigator.clipboard.writeText(document.getElementById(b.dataset.copy).textContent);
    const label = b.textContent; b.textContent = "Copied"; setTimeout(() => (b.textContent = label), 1500);
  });
}
setTimeout(poll, 2000);
</script>"""


def session_cookie(oauth: OAuth, public_url: Callable[[], str]) -> Callable[[Response, str], Response]:
    """Signs `user` in to the account page on this response (shared with the sign-up page)."""

    def set_cookie(response: Response, user: str) -> Response:
        response.set_cookie(
            COOKIE, oauth.new_session(user), max_age=ACCOUNT_SESSION_TTL_S, httponly=True, samesite="lax",
            secure=public_url().startswith("https://"), path="/",
        )
        return response

    return set_cookie


def routes(
    *,
    oauth: OAuth,
    keys: KeyStore,
    key_user: Callable[[str], str | None],
    connections: ConnectionManager | None,
    sessions: EncryptedSessionStore | None,
    signup_code: str,
    public_url: Callable[[], str],
) -> list[BaseRoute]:
    limiter = AttemptLimiter()

    def cookie_user(request: Request) -> str | None:
        return oauth.session_user(request.cookies.get(COOKIE))

    def same_site(request: Request) -> bool:
        """POSTs must come from our own pages (the cookie is SameSite=Lax; this is belt and braces)."""
        if request.headers.get("sec-fetch-site") == "cross-site":
            return False
        origin = request.headers.get("origin")
        return not origin or origin == "null" or urlsplit(origin).netloc == request.headers.get("host")

    sign_in = session_cookie(oauth, public_url)

    async def form_of(request: Request) -> dict[str, str] | None:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return None
        return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}

    async def authenticate(request: Request, form: dict[str, str]) -> tuple[str | None, str, int]:
        """(user, error, status) for a sign-in form: continue / password / key / signup."""
        addr = request.client.host if request.client else "?"
        action = form.get("action", "")
        if action == "continue":
            user = cookie_user(request)
            return (user, "", 200) if user else (None, "Your session ended. Sign in again.", 401)
        if limiter.blocked(addr):
            return None, "Too many failed attempts. Try again in a few minutes.", 429
        if action == "password":
            user = await asyncio.to_thread(keys.check_login, form.get("login", ""), form.get("password", ""))
            if user is None:
                limiter.add(addr)
                return None, "That name or email and password don't match.", 401
            return user, "", 200
        if action == "key":
            user = key_user(form.get("key", "").strip())
            if user is None:
                limiter.add(addr)
                return None, "That API key isn't valid.", 401
            return user, "", 200
        if action == "signup":
            if not signup_code:
                return None, "Sign-up is turned off on this server.", 403
            if not hmac.compare_digest(form.get("code", "").strip().encode(), signup_code.encode()):
                limiter.add(addr)
                return None, "That invite code isn't right.", 403
            name, email = form.get("name", "").strip(), form.get("email", "").strip()
            try:
                await asyncio.to_thread(keys.create, name, email, form.get("password", ""))
            except KeyStoreError as e:
                return None, str(e)[:1].upper() + str(e)[1:] + ".", 409 if "taken" in str(e) or "already" in str(e) else 400
            log.info("new user signed up: %s", name)  # never the password, key or email
            return name, "", 200
        return None, "Something went wrong with that form. Try again.", 400

    # ---- sign-in for an app (OAuth) ---------------------------------------------------------------------

    def oauth_page(pending: PendingSignIn, request: Request, error: str = "", status: int = 200, login: str = "") -> Response:
        app = html.escape(pending.app_name)
        body = _sign_in_body(
            action="/oauth/login", request_id=pending.id, heading="Sign in to htb",
            intro=f"<strong>{app}</strong> wants to use websites through this server as you: your own sign-ins, kept "
            "separate from everyone else's. You can disconnect it any time from your account page.",
            error=error, signed_in_as=cookie_user(request), signup_open=bool(signup_code), login=login,
        )
        return _page("Sign in to htb", body, status)

    def expired_page() -> Response:
        return _page(
            "Sign-in expired",
            "<h1>This sign-in link expired</h1><p>Go back to the app and click Connect again.</p>",
            400,
        )

    async def oauth_login(request: Request) -> Response:
        pending = oauth.pending(request.query_params.get("request", ""))
        return oauth_page(pending, request) if pending else expired_page()

    async def oauth_login_post(request: Request) -> Response:
        if not same_site(request):
            return Response("Forbidden", status_code=403)
        form = await form_of(request)
        pending = oauth.pending((form or {}).get("request", ""))
        if form is None or pending is None:
            return expired_page()
        if form.get("action") == "deny":
            back = oauth.deny(pending.id)
            return RedirectResponse(back, status_code=302) if back else expired_page()
        user, error, status = await authenticate(request, form)
        if user is None:
            return oauth_page(pending, request, error, status, form.get("login", ""))
        log.info("%s connected %s", user, pending.app_name)
        return sign_in(RedirectResponse(oauth.approve(pending.id, user), status_code=302), user)

    # ---- the account page --------------------------------------------------------------------------------

    async def account_login(request: Request) -> Response:
        if cookie_user(request):
            return RedirectResponse("/account", status_code=302)
        body = _sign_in_body(
            action="/account/login", request_id="", heading="Your htb account",
            intro="See sign-ins your agent is waiting for, the sites you're signed in to, and the apps connected as you.",
            error="", signed_in_as=None, signup_open=bool(signup_code),
        )
        return _page("Your htb account", body)

    async def account_login_post(request: Request) -> Response:
        if not same_site(request):
            return Response("Forbidden", status_code=403)
        form = await form_of(request) or {}
        user, error, status = await authenticate(request, form)
        if user is None:
            body = _sign_in_body(
                action="/account/login", request_id="", heading="Your htb account", intro="", error=error,
                signed_in_as=None, signup_open=bool(signup_code), login=form.get("login", ""),
            )
            return _page("Your htb account", body, status)
        return sign_in(RedirectResponse("/account", status_code=302), user)

    def connected_sites(user: str) -> list[dict[str, Any]]:
        if sessions is None:
            return []
        return [
            {"domain": r.domain, "provider": r.provider, "status": r.status, "since": r.connected_at}
            for r in sessions.records() if r.user == user
        ]

    def account_body(user: str, new_key: str | None = None, notice: str = "") -> tuple[str, str]:
        pending = connections.pending_for(user) if connections else []
        base = public_url()
        waiting_rows = "".join(
            f'<div class="row"><span>Your agent needs you to sign in to <strong>{html.escape(p["domain"])}</strong> '
            f'<span class="small muted">(link works for {p["minutes_left"]} more min)</span></span>'
            f'<a class="button" href="{html.escape(p["url"])}" target="_blank" rel="noopener">Sign in</a></div>'
            for p in pending
        ) or '<p class="empty">Nothing waiting. When your agent needs you to sign in to a site, it shows up here.</p>'
        sites = "".join(
            f'<div class="row"><span><strong>{html.escape(s["domain"])}</strong> '
            f'<span class="small muted">{"signed in" if s["status"] == "connected" else "sign-in expired"} '
            f'since {_when(s["since"])}</span></span>'
            f'<form method="post" action="/account/disconnect"><input type="hidden" name="domain" '
            f'value="{html.escape(s["domain"])}"><button class="secondary" type="submit">Disconnect</button></form></div>'
            for s in connected_sites(user)
        ) or '<p class="empty">Not signed in to any sites yet. Your agent will ask when it needs you.</p>'
        apps = "".join(
            f'<div class="row"><span><strong>{html.escape(a["name"])}</strong> '
            f'<span class="small muted">connected {_when(a["signed_in"])}, until {_when(a["expires_at"])}</span></span>'
            f'<form method="post" action="/account/apps/revoke"><input type="hidden" name="client_id" '
            f'value="{html.escape(a["client_id"])}"><button class="secondary" type="submit">Disconnect</button></form></div>'
            for a in oauth.apps(user)
        ) or '<p class="empty">No apps connected through sign-in yet.</p>'
        if user == ENV_KEY_USER:
            key_part = '<p class="small muted">Your key is <code>GATEWAY_API_KEY</code> in the server\'s .env file.</p>'
        elif new_key:
            key_part = (
                "<p><strong>Your new key</strong> (shown once; your old key no longer works):</p>"
                f'<pre id="newkey">{html.escape(new_key)}</pre>'
                '<button class="copy" type="button" data-copy="newkey">Copy key</button>'
                "<p style=\"margin-top:12px\">For Claude Code, run:</p>"
                f'<pre id="cmd">{html.escape(mcp_add_command(base, new_key))}</pre>'
                '<button class="copy" type="button" data-copy="cmd">Copy command</button>'
            )
        else:
            key_part = (
                '<p class="small muted">For Claude Code and other apps that take a key. Making a new key turns off '
                "the old one.</p>"
                '<form method="post" action="/account/key"><button class="secondary" type="submit">Make a new key</button></form>'
            )
        body = (
            f'<header><h1>Hi, {html.escape(user)}</h1><form method="post" action="/account/logout">'
            '<button class="linkbtn" type="submit">Sign out</button></form></header>'
            + (f'<p class="muted">{html.escape(notice)}</p>' if notice else "")
            + f'<section class="card{" waiting" if pending else ""}" id="waiting"><h2>Waiting for you</h2>'
            f'<div id="waiting-list">{waiting_rows}</div>'
            '<button id="notify" class="secondary copy" type="button" style="margin-top:10px">'
            "Notify me when a sign-in is waiting</button></section>"
            f'<section class="card"><h2>Sites you\'re signed in to</h2>{sites}</section>'
            f'<section class="card"><h2>Connected apps</h2>{apps}'
            '<p class="small muted" style="margin-top:12px">To connect regular Claude (web, desktop or phone): '
            "Customize &rarr; Connectors &rarr; Add custom connector, with this URL, then click Connect:</p>"
            f'<pre id="url">{html.escape(base)}/mcp</pre>'
            '<button class="copy" type="button" data-copy="url">Copy URL</button></section>'
            f'<section class="card"><h2>API key</h2>{key_part}</section>'
        )
        return body, ACCOUNT_SCRIPT.replace("__SEEN__", repr([p["url"] for p in pending]).replace("'", '"'))

    async def account(request: Request) -> Response:
        user = cookie_user(request)
        if user is None:
            return RedirectResponse("/account/login", status_code=302)
        body, script = account_body(user)
        return _page("Your htb account", body, script=script)

    async def status(request: Request) -> Response:
        user = cookie_user(request)
        if user is None:
            return JSONResponse({"error": "signed out"}, status_code=401, headers=HEADERS)
        return JSONResponse({"pending": connections.pending_for(user) if connections else []}, headers=HEADERS)

    async def guarded(request: Request) -> tuple[str | None, dict[str, str], Response | None]:
        if not same_site(request):
            return None, {}, Response("Forbidden", status_code=403)
        user = cookie_user(request)
        if user is None:
            return None, {}, RedirectResponse("/account/login", status_code=302)
        return user, await form_of(request) or {}, None

    async def disconnect(request: Request) -> Response:
        user, form, refused = await guarded(request)
        if refused is not None or user is None:
            return refused or Response(status_code=400)
        domain = form.get("domain", "")
        if connections is not None and domain:
            await connections.disconnect(user, domain)
        return RedirectResponse("/account", status_code=302)

    async def new_key(request: Request) -> Response:
        user, _, refused = await guarded(request)
        if refused is not None or user is None:
            return refused or Response(status_code=400)
        if user == ENV_KEY_USER:
            return RedirectResponse("/account", status_code=302)
        key = keys.reissue(user)
        log.info("%s made a new API key", user)
        body, script = account_body(user, new_key=key)
        return _page("Your htb account", body, script=script)

    async def revoke_app(request: Request) -> Response:
        user, form, refused = await guarded(request)
        if refused is not None or user is None:
            return refused or Response(status_code=400)
        oauth.revoke_app(user, form.get("client_id", ""))
        return RedirectResponse("/account", status_code=302)

    async def logout(request: Request) -> Response:
        if not same_site(request):
            return Response("Forbidden", status_code=403)
        oauth.end_session(request.cookies.get(COOKIE))
        response = RedirectResponse("/account/login", status_code=302)
        response.delete_cookie(COOKIE, path="/")
        return response

    return [
        Route("/oauth/login", oauth_login, methods=["GET"]),
        Route("/oauth/login", oauth_login_post, methods=["POST"]),
        Route("/account", account, methods=["GET"]),
        Route("/account/login", account_login, methods=["GET"]),
        Route("/account/login", account_login_post, methods=["POST"]),
        Route("/account/status", status, methods=["GET"]),
        Route("/account/disconnect", disconnect, methods=["POST"]),
        Route("/account/key", new_key, methods=["POST"]),
        Route("/account/apps/revoke", revoke_app, methods=["POST"]),
        Route("/account/logout", logout, methods=["POST"]),
    ]
