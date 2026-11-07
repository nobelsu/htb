"""A tiny local web app: the offline stand-in for real sites.

Features (see mapper/CLAUDE.md, build step 2):

* Login cookie: ``POST /login`` sets ``sid`` (HttpOnly) and a LinkedIn-style quoted
  ``JSESSIONID="ajax:..."``. Every ``/api/*`` call needs a valid ``sid`` (else 401).
* Token flow: ``GET /api/session`` returns ``{"token": ...}``; ``/api/search`` needs it as ``x-token``.
* Session-cookie header: ``/api/search`` also needs ``csrf-token`` = ``JSESSIONID`` without quotes.
* Paginated search: ``GET /api/search?q=...&start=N`` → ``{"total", "start", "count", "elements"}``.
* CSRF meta: ``/compose`` has ``<meta name="csrf" content="...">``; ``POST /api/notes`` (form body
  ``csrf=..&text=..``) must send it back.
* ``__NEXT_DATA__``: the ``/search`` page embeds a Next.js-style JSON blob.

Run by hand: ``PYTHONPATH=tests/mapper python -m fixture_site --port 8765``.
"""

from __future__ import annotations

import json
import secrets
import socketserver
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import data, pages

BUILD_ID = "fx-build-20261107"

STATIC = {
    "/static/app.css": ("text/css", pages.APP_CSS),
    "/static/search.js": ("application/javascript", pages.SEARCH_JS),
    "/static/compose.js": ("application/javascript", pages.COMPOSE_JS),
}


@dataclass
class Session:
    sid: str
    jsessionid: str  # the value inside the quotes, e.g. "ajax:123..."
    tokens: set[str] = field(default_factory=set)
    csrf: set[str] = field(default_factory=set)
    notes: list[dict] = field(default_factory=list)


class SiteState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sessions: dict[str, Session] = {}
        self.requests: list[tuple[str, str]] = []  # (method, path) log for tests
        self.note_seq = 0
        # "site redesign" switch for healing tests: search moves from /api/search to /api/v2/search
        self.search_endpoint = "/api/search"

    def new_session(self) -> Session:
        with self.lock:
            sess = Session(sid=secrets.token_hex(16), jsessionid=f"ajax:{secrets.randbelow(10**18):018d}")
            self.sessions[sess.sid] = sess
            return sess


def _parse_cookies(header: str | None) -> dict[str, str]:
    """Raw cookie values (quotes kept), unlike SimpleCookie which unquotes."""
    jar: dict[str, str] = {}
    for part in (header or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep:
            jar[name] = value
    return jar


class Handler(BaseHTTPRequestHandler):
    server_version = "FixtureSite/1.0"
    state: SiteState  # set on the subclass made by FixtureSite

    # --- plumbing -------------------------------------------------------------------------
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # keep test output quiet

    def _send(self, status: int, body: str | bytes, ctype: str, headers: dict | None = None):
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            if isinstance(value, list):
                for v in value:
                    self.send_header(key, v)
            else:
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj), "application/json; charset=utf-8")

    def _html(self, status: int, text: str, headers: dict | None = None) -> None:
        self._send(status, text, "text/html; charset=utf-8", headers)

    def _redirect(self, location: str, headers: dict | None = None) -> None:
        self._send(303, b"", "text/plain", {"Location": location, **(headers or {})})

    def _session(self) -> Session | None:
        sid = _parse_cookies(self.headers.get("Cookie")).get("sid")
        return self.state.sessions.get(sid or "")

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    # --- routing --------------------------------------------------------------------------
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        self.state.requests.append(("GET", url.path))
        query = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        route = {
            "/": self._home,
            "/login": self._login_page,
            "/logout": self._logout,
            "/search": self._search_page,
            "/compose": self._compose_page,
            "/api/session": self._api_session,
            self.state.search_endpoint: self._api_search,
            "/api/notes": self._api_notes_list,
        }.get(url.path)
        if route:
            route(query)
        elif url.path in STATIC:
            ctype, body = STATIC[url.path]
            self._send(200, body, ctype)
        elif url.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._json(404, {"error": "not found"})

    do_HEAD = do_GET

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        self.state.requests.append(("POST", url.path))
        if url.path == "/login":
            self._login_submit()
        elif url.path == "/api/notes":
            self._api_notes_create()
        else:
            self._json(404, {"error": "not found"})

    # --- pages ----------------------------------------------------------------------------
    def _home(self, _query) -> None:
        self._html(200, pages.home())

    def _login_page(self, query) -> None:
        self._html(200, pages.login(query.get("next", "/")))

    def _logout(self, _query) -> None:
        """Ends the session server-side: an explorer that clicks this would lose its login."""
        sid = _parse_cookies(self.headers.get("Cookie")).get("sid")
        with self.state.lock:
            self.state.sessions.pop(sid or "", None)
        self._redirect("/login")

    def _login_submit(self) -> None:
        form = self._form()
        if not form.get("username") or not form.get("password"):
            self._html(400, pages.login(form.get("next", "/")))
            return
        sess = self.state.new_session()
        cookies = [
            f"sid={sess.sid}; Path=/; HttpOnly; SameSite=Lax",
            f'JSESSIONID="{sess.jsessionid}"; Path=/; SameSite=Lax',
        ]
        nxt = form.get("next") or "/"
        self._redirect(nxt if nxt.startswith("/") else "/", {"Set-Cookie": cookies})

    def _require_page_login(self) -> Session | None:
        sess = self._session()
        if sess is None:
            self._redirect(f"/login?next={urlsplit(self.path).path}")
        return sess

    def _search_page(self, _query) -> None:
        if self._require_page_login() is None:
            return
        next_data = {
            "props": {"pageProps": {"page": "search", "featured": [c["id"] for c in data.COMPANIES[:3]]}},
            "page": "/search",
            "buildId": BUILD_ID,
        }
        self._html(200, pages.search(next_data, self.state.search_endpoint))

    def _compose_page(self, _query) -> None:
        sess = self._require_page_login()
        if sess is None:
            return
        token = secrets.token_urlsafe(18)
        with self.state.lock:
            sess.csrf.add(token)
        self._html(200, pages.compose(token))

    # --- API ------------------------------------------------------------------------------
    def _require_api_login(self) -> Session | None:
        sess = self._session()
        if sess is None:
            self._json(401, {"error": "login required"})
        return sess

    def _api_session(self, _query) -> None:
        sess = self._require_api_login()
        if sess is None:
            return
        token = secrets.token_hex(12)
        with self.state.lock:
            sess.tokens.add(token)
        self._json(200, {"token": token, "expires_in": 300})

    def _api_search(self, query) -> None:
        sess = self._require_api_login()
        if sess is None:
            return
        if self.headers.get("x-token") not in sess.tokens:
            self._json(400, {"error": "missing or invalid x-token"})
            return
        if self.headers.get("csrf-token") != sess.jsessionid:
            self._json(400, {"error": "csrf-token does not match session"})
            return
        try:
            start = int(query.get("start", "0"))
        except ValueError:
            self._json(400, {"error": "start must be an integer"})
            return
        q = query.get("q", "")
        total, elements = data.search(q, max(start, 0))
        self._json(
            200,
            {"query": q, "start": start, "count": len(elements), "total": total, "elements": elements},
        )

    def _api_notes_list(self, _query) -> None:
        sess = self._require_api_login()
        if sess is not None:
            self._json(200, {"elements": sess.notes})

    def _api_notes_create(self) -> None:
        sess = self._require_api_login()
        if sess is None:
            return
        form = self._form()
        if form.get("csrf") not in sess.csrf:
            self._json(400, {"ok": False, "error": "bad csrf"})
            return
        text = form.get("text", "").strip()
        if not text:
            self._json(400, {"ok": False, "error": "text required"})
            return
        with self.state.lock:
            self.state.note_seq += 1
            note = {"id": f"n_{self.state.note_seq}", "text": text}
            sess.notes.append(note)
        self._json(201, {"ok": True, "note": note})


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer.server_bind does a reverse-DNS getfqdn(), which can stall for 30 s+.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class FixtureSite:
    """Run the fixture site on a background thread. Use as a context manager."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.state = SiteState()
        handler = type("BoundHandler", (Handler,), {"state": self.state})
        self.httpd = _Server((host, port), handler)
        self._thread: threading.Thread | None = None

    @property
    def host(self) -> str:
        return self.httpd.server_address[0]

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> FixtureSite:
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self) -> FixtureSite:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def move_search(self, moved: bool = True) -> None:
        """Simulate a site change: the search API moves to /api/v2/search (the old path 404s)."""
        self.state.search_endpoint = "/api/v2/search" if moved else "/api/search"

    def login_state(self, path: str | Path | None = None) -> dict:
        """A logged-in Playwright ``storage_state`` (optionally written to ``path``)."""
        sess = self.state.new_session()
        common = {"domain": self.host, "path": "/", "expires": -1, "secure": False, "sameSite": "Lax"}
        state = {
            "cookies": [
                {"name": "sid", "value": sess.sid, "httpOnly": True, **common},
                {"name": "JSESSIONID", "value": f'"{sess.jsessionid}"', "httpOnly": False, **common},
            ],
            "origins": [],
        }
        if path is not None:
            Path(path).write_text(json.dumps(state, indent=2))
        return state
