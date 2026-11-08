"""The sign-in pages (where regular Claude's "Connect" button lands, and the account sign-in) and the account page,
drawn in the Spine design (runtime/ui.py).

GET/POST /oauth/login?request=...   sign in for an app (OAuth): password, API key, or a new account
GET/POST /account/login             sign in to the account page (same three ways)
GET      /account                   waiting sign-ins (live), sites, connected apps, connector, key, password
GET      /account/status            the live part, as JSON
POST     /account/disconnect        sign out of a site
POST     /account/key               a new API key (the old one stops working)
POST     /account/apps/revoke       disconnect an app (its OAuth tokens)
GET/POST /account/password          change your password
POST     /account/logout

Sign-in pages take `?with=key` or `?with=signup` to switch how you sign in. The account page is a cookie session
(HttpOnly, SameSite=Lax); POSTs from another site are refused.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Route

from runtime.connect import ConnectionManager
from runtime.keys import ENV_KEY_USER, KeyStore, KeyStoreError, mcp_add_command
from runtime.oauth import ACCOUNT_SESSION_TTL_S, OAuth, PendingSignIn
from runtime.session_store import EncryptedSessionStore
from runtime.signup_page import AttemptLimiter, copy_row
from runtime.ui import COPY_SCRIPT, HEADERS, bar_button, bar_link, esc, field, hidden, icon, notice, page

log = logging.getLogger("runtime.account")
COOKIE = "spider_account"
MAX_BODY = 4096
MODES = ("password", "key", "signup")


def session_cookie(oauth: OAuth, public_url: Callable[[], str]) -> Callable[[Response, str], Response]:
    """Signs `user` in to the account page on this response (shared with the sign-up page)."""

    def set_cookie(response: Response, user: str) -> Response:
        response.set_cookie(
            COOKIE, oauth.new_session(user), max_age=ACCOUNT_SESSION_TTL_S, httponly=True, samesite="lax",
            secure=public_url().startswith("https://"), path="/",
        )
        return response

    return set_cookie


def sign_in_page(
    *,
    action: str,
    spine: str,
    title: str,
    mode: str,
    signup_open: bool,
    request_id: str = "",
    app_name: str = "",
    signed_in_as: str | None = None,
    error: str = "",
    login: str = "",
    status: int = 200,
) -> Response:
    """The one sign-in layout, for an app (OAuth: `request_id`, `app_name`) or for the account page."""
    for_app = bool(request_id)
    query = {"request": request_id} if request_id else {}

    def switch(to: str, text: str) -> str:
        return f'<a href="{esc(action)}?{esc(urlencode({**query, "with": to}))}">{esc(text)}</a>'

    nav = []
    if mode != "password":
        nav.append(switch("password", "Use a password"))
    if mode != "key":
        nav.append(switch("key", "Use a key"))
    if mode != "signup" and signup_open:
        nav.append(switch("signup", "Create account"))
    if for_app:
        nav.append(
            f'<form method="post" action="{esc(action)}" style="display:inline">{hidden("request", request_id)}'
            '<button type="submit" name="action" value="deny">Cancel</button></form>'
        )

    parts = [notice(error)]
    if signed_in_as and mode == "password":
        parts.append(
            f'<form method="post" action="{esc(action)}" class="row strong" style="margin:0">'
            f'{hidden("request", request_id)}<span class="big">{esc(signed_in_as)}</span>'
            f'<button class="pill" type="submit" name="action" value="continue">Continue{icon("arrow", 20)}</button></form>'
        )
    verb = "connect" if for_app else ""
    if mode == "key":
        fields = field("key", "API key", "password")
        bar = bar_button("Sign in and connect" if for_app else "Sign in")
    elif mode == "signup":
        fields = (
            field("code", "invite code", code=True)
            + field("name", "username", autocomplete="username")
            + field("email", "email", "email", autocomplete="email")
            + field("password", "password", "password", autocomplete="new-password")
        )
        bar = bar_button("Create account and connect" if for_app else "Create account")
    else:
        fields = field("login", "username", value=login, autocomplete="username") + field(
            "password", "password", "password", autocomplete="current-password"
        )
        bar = bar_button("Sign in and connect" if verb else "Sign in")
    parts.append(
        f'<form class="stack" method="post" action="{esc(action)}">{hidden("request", request_id)}'
        f'{hidden("action", mode)}{fields}{bar}</form>'
    )
    return page(title=title, spine=spine, body="".join(parts), nav=nav, status=status)


def message_page(spine: str, title: str, line: str, bar: str, status: int = 200, inverted: bool = False) -> Response:
    """A page that says one thing (expired, done...), with one way forward."""
    mark = icon("check" if inverted else "clock", 96, 1.2)
    body = f'<div class="center">{mark}<h1 class="giant">{esc(line)}</h1></div>{bar}'
    return page(title=title, spine=spine, body=body, status=status, inverted=inverted)


ACCOUNT_SCRIPT = """<script>
const waiting = document.getElementById("waiting");
const seen = new Set(JSON.parse(waiting.dataset.seen || "[]"));
const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
let shown = [...seen].sort().join(" ");
function arrow(n) {
  return `<svg width="${n}" height="${n}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
    stroke-linecap="square" aria-hidden="true"><path d="M4 12h16"/><path d="M13 5l7 7-7 7"/></svg>`;
}
function render(pending) {
  const now = pending.map((p) => p.url).sort().join(" ");
  if (now === shown) return;  // nothing changed: leave the rows (and their pulse) alone
  shown = now;
  waiting.replaceChildren(...pending.map((p) => {
    const a = document.createElement("a");
    a.className = "row strong" + (seen.has(p.url) ? "" : " arrive"); a.href = p.url; a.target = "_blank"; a.rel = "noopener";
    a.style.textDecoration = "none";
    const name = document.createElement("span"); name.className = "big"; name.textContent = p.domain;
    const pill = document.createElement("span"); pill.className = "pill pulse"; pill.innerHTML = "Sign in" + arrow(20);
    a.append(name, pill);
    return a;
  }));
  const empty = document.getElementById("empty");
  if (empty) empty.hidden = pending.length > 0 || empty.dataset.hasRows === "1";
  for (const p of pending) seen.add(p.url);
}
async function poll() {
  try {
    const r = await fetch("/account/status", {cache: "no-store"});
    if (r.status === 401) { location.href = "/account/login"; return; }
    render((await r.json()).pending);
  } catch (e) {}
  setTimeout(poll, 2000);
}
// Disconnect: strike the name through, fold the row away, then send it.
for (const f of document.querySelectorAll("form.disconnect")) {
  f.addEventListener("submit", (e) => {
    if (still) return;
    e.preventDefault();
    const row = f.closest(".row");
    row.querySelector(".big").classList.add("struck");
    row.style.overflow = "hidden";
    row.animate(
      [{height: row.offsetHeight + "px", opacity: 1}, {height: "0px", paddingTop: "0px", paddingBottom: "0px", opacity: 0}],
      {duration: 450, delay: 350, easing: "cubic-bezier(.7,0,.2,1)", fill: "forwards"}
    ).onfinish = () => f.submit();
  });
}
setTimeout(poll, 2000);
</script>"""


def _app_detail(app: dict[str, Any]) -> str:
    """How to tell connected apps apart: where they run and when they were connected."""
    mins = int((time.time() - float(app.get("signed_in") or time.time())) // 60)
    when = (
        "just now" if mins < 1 else f"{mins} min ago" if mins < 60
        else f"{mins // 60} h ago" if mins < 48 * 60 else f"{mins // 1440} days ago"
    )
    where = str(app.get("where") or "")
    return f"{where} · connected {when}" if where else f"connected {when}"


MAP_LABELS = {  # a site being set up for this user (runtime/mapping.py)
    "queued": "Queued",
    "running": "Setting up",
    "succeeded": "Ready",
    "failed": "Didn't work",
    "needs_sign_in": "Needs sign-in",
}


def routes(
    *,
    oauth: OAuth,
    keys: KeyStore,
    key_user: Callable[[str], str | None],
    connections: ConnectionManager | None,
    sessions: EncryptedSessionStore | None,
    signup_code: str,
    public_url: Callable[[], str],
    maps: Any = None,
) -> list[BaseRoute]:
    limiter = AttemptLimiter()
    sign_in_cookie = session_cookie(oauth, public_url)

    def cookie_user(request: Request) -> str | None:
        return oauth.session_user(request.cookies.get(COOKIE))

    def same_site(request: Request) -> bool:
        """POSTs must come from our own pages (the cookie is SameSite=Lax; this is belt and braces)."""
        if request.headers.get("sec-fetch-site") == "cross-site":
            return False
        origin = request.headers.get("origin")
        return not origin or origin == "null" or urlsplit(origin).netloc == request.headers.get("host")

    async def form_of(request: Request) -> dict[str, str] | None:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return None
        return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}

    def mode_of(request: Request, form: dict[str, str] | None = None) -> str:
        wanted = (form or {}).get("action") or request.query_params.get("with", "password")
        return wanted if wanted in MODES and (wanted != "signup" or signup_code) else "password"

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
                status = 409 if "taken" in str(e) or "already" in str(e) else 400
                return None, str(e)[:1].upper() + str(e)[1:] + ".", status
            log.info("new user signed up: %s", name)  # never the password, key or email
            return name, "", 200
        return None, "Something went wrong with that form. Try again.", 400

    # ---- sign-in for an app (OAuth) ---------------------------------------------------------------------

    def oauth_page(
        pending: PendingSignIn, request: Request, mode: str, error: str = "", status: int = 200, login: str = ""
    ) -> Response:
        return sign_in_page(
            action="/oauth/login", spine="Connect", title=f"Connect {pending.app_name}", mode=mode,
            signup_open=bool(signup_code), request_id=pending.id, app_name=pending.app_name,
            signed_in_as=cookie_user(request), error=error, login=login, status=status,
        )

    def expired() -> Response:
        return message_page("Expired", "Sign-in expired", "This sign-in expired.", bar_link("Go to your account", "/account"), 400)

    async def oauth_login(request: Request) -> Response:
        pending = oauth.pending(request.query_params.get("request", ""))
        return oauth_page(pending, request, mode_of(request)) if pending else expired()

    async def oauth_login_post(request: Request) -> Response:
        if not same_site(request):
            return Response("Forbidden", status_code=403)
        form = await form_of(request)
        pending = oauth.pending((form or {}).get("request", ""))
        if form is None or pending is None:
            return expired()
        if form.get("action") == "deny":
            back = oauth.deny(pending.id)
            return RedirectResponse(back, status_code=302) if back else expired()
        user, error, status = await authenticate(request, form)
        if user is None:
            return oauth_page(pending, request, mode_of(request, form), error, status, form.get("login", ""))
        log.info("%s connected %s", user, pending.app_name)
        return sign_in_cookie(RedirectResponse(oauth.approve(pending.id, user), status_code=302), user)

    # ---- the account page --------------------------------------------------------------------------------

    def account_sign_in(request: Request, mode: str, error: str = "", status: int = 200, login: str = "") -> Response:
        return sign_in_page(
            action="/account/login", spine="Sign in", title="Sign in", mode=mode, signup_open=bool(signup_code),
            error=error, login=login, status=status,
        )

    async def account_login(request: Request) -> Response:
        if cookie_user(request):
            return RedirectResponse("/account", status_code=302)
        return account_sign_in(request, mode_of(request))

    async def account_login_post(request: Request) -> Response:
        if not same_site(request):
            return Response("Forbidden", status_code=403)
        form = await form_of(request) or {}
        user, error, status = await authenticate(request, form)
        if user is None:
            return account_sign_in(request, mode_of(request, form), error, status, form.get("login", ""))
        return sign_in_cookie(RedirectResponse("/account", status_code=302), user)

    def connected_sites(user: str) -> list[dict[str, Any]]:
        if sessions is None:
            return []
        return [{"domain": r.domain, "status": r.status} for r in sessions.records() if r.user == user]

    def square(action: str, name: str, value: str, label: str) -> str:
        return (
            f'<form class="disconnect" method="post" action="{action}" style="margin:0">{hidden(name, value)}'
            f'<button class="sq" type="submit" aria-label="{esc(label)}">{icon("x", 22)}</button></form>'
        )

    def account_page(user: str, new_key: str | None = None, message: str = "") -> Response:
        base = public_url()
        pending = connections.pending_for(user) if connections else []
        sites, apps = connected_sites(user), oauth.apps(user)
        waiting_rows = "".join(
            f'<a class="row strong" href="{esc(p["url"])}" target="_blank" rel="noopener" style="text-decoration:none">'
            f'<span class="big">{esc(p["domain"])}</span><span class="pill pulse">Sign in{icon("arrow", 20)}</span></a>'
            for p in pending
        )
        site_rows = "".join(
            f'<div class="row"><span class="big{" struck" if s["status"] != "connected" else ""}">{esc(s["domain"])}</span>'
            + square("/account/disconnect", "domain", s["domain"], f"Disconnect {s['domain']}")
            + "</div>"
            for s in sites
        )
        app_rows = "".join(
            f'<div class="row"><span class="big">{icon("link", 44, label="Connected app")}'
            f'<span class="stack"><span>{esc(a["name"])}</span>'
            f'<span class="mono">{esc(_app_detail(a))}</span></span></span>'
            + square("/account/apps/revoke", "client_id", a["client_id"], f"Disconnect {a['name']}")
            + "</div>"
            for a in apps
        )
        map_rows = "".join(
            f'<div class="row"><span class="big">{esc(j["domain"])}</span>'
            f'<span class="pill{" pulse" if j["status"] in ("queued", "running") else ""}">'
            f'{MAP_LABELS.get(j["status"], j["status"])}</span></div>'
            for j in (maps.jobs_for(user)[:5] if maps is not None else [])
        )
        has_rows = bool(site_rows or app_rows or map_rows)
        empty = (
            f'<p class="empty" id="empty" data-has-rows="{1 if has_rows else 0}"'
            f'{" hidden" if has_rows or pending else ""}>Nothing yet</p>'
        )
        tiles = [
            f'<button class="tile" type="button" data-copy="{esc(base)}/mcp">{icon("copy", 28, 1.8)}<span>Connector</span></button>'
        ]
        if user != ENV_KEY_USER:
            tiles.append(
                '<form method="post" action="/account/key" style="display:contents">'
                f'<button class="tile" type="submit">{icon("key", 28, 1.8)}<span>New key</span></button></form>'
            )
            tiles.append(f'<a class="tile" href="/account/password">{icon("lock", 28, 1.8)}<span>Password</span></a>')
        new_key_rows = (
            notice("Your new key is below. Your old key no longer works.", error=False)
            + copy_row("Claude Code", "claude mcp add", mcp_add_command(base, new_key), "Copy the Claude Code command, which holds your new key")
            if new_key else ""
        )
        body = (
            f'<h1 class="title">{esc(user)}</h1>' + notice(message, error=False) + new_key_rows
            + f'<div id="waiting" data-seen="{esc(json.dumps([p["url"] for p in pending]))}">{waiting_rows}</div>'
            + map_rows + site_rows + app_rows + empty
            + f'<div class="tiles">{"".join(tiles)}</div>'
        )
        sign_out = (
            '<form method="post" action="/account/logout" style="margin:0">'
            '<button type="submit">Sign out</button></form>'
        )
        return page(
            title=f"Account, {user}", spine="Account", body=body, spine_end=sign_out,
            script=COPY_SCRIPT + ACCOUNT_SCRIPT,
        )

    async def account(request: Request) -> Response:
        user = cookie_user(request)
        if user is None:
            return RedirectResponse("/account/login", status_code=302)
        message = "Password changed." if request.query_params.get("changed") == "password" else ""
        return account_page(user, message=message)

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
        return account_page(user, new_key=key)

    async def revoke_app(request: Request) -> Response:
        user, form, refused = await guarded(request)
        if refused is not None or user is None:
            return refused or Response(status_code=400)
        oauth.revoke_app(user, form.get("client_id", ""))
        return RedirectResponse("/account", status_code=302)

    def password_page(error: str = "", status: int = 200) -> Response:
        body = (
            notice(error)
            + '<form class="stack" method="post" action="/account/password">'
            + field("current", "current password", "password", autocomplete="current-password")
            + field("new", "new password", "password", autocomplete="new-password")
            + bar_button("Change password")
            + "</form>"
        )
        return page(title="Change password", spine="Password", body=body, status=status,
                    nav=['<a href="/account">Account</a>'])

    async def password(request: Request) -> Response:
        user = cookie_user(request)
        if user is None or user == ENV_KEY_USER:
            return RedirectResponse("/account/login" if user is None else "/account", status_code=302)
        return password_page()

    async def password_post(request: Request) -> Response:
        user, form, refused = await guarded(request)
        if refused is not None or user is None:
            return refused or Response(status_code=400)
        if user == ENV_KEY_USER:
            return RedirectResponse("/account", status_code=302)
        addr = request.client.host if request.client else "?"
        if limiter.blocked(addr):
            return password_page("Too many failed attempts. Try again in a few minutes.", 429)
        if await asyncio.to_thread(keys.check_login, user, form.get("current", "")) != user:
            limiter.add(addr)
            return password_page("That isn't your current password.", 401)
        try:
            await asyncio.to_thread(keys.set_password, user, form.get("new", ""))
        except KeyStoreError as e:
            return password_page(str(e)[:1].upper() + str(e)[1:] + ".", 400)
        log.info("%s changed their password", user)
        return RedirectResponse("/account?changed=password", status_code=302)

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
        Route("/account/password", password, methods=["GET"]),
        Route("/account/password", password_post, methods=["POST"]),
        Route("/account/logout", logout, methods=["POST"]),
    ]
