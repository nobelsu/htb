"""A small shop used as the executor's test target. Every result code has an endpoint that reaches it.

Run standalone:  uv run uvicorn tests.fixture_site.app:app --port 8900
Admin:  POST /_admin/break (renames /api/items -> /api/v2/items), POST /_admin/reset, GET /_admin/hits
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

from fastapi import Cookie, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

PRODUCTS: list[dict[str, Any]] = [
    {"id": f"p{i}", "sku": f"SKU-{i:03d}", "name": name, "price": 10 + i}
    for i, name in enumerate(
        [
            "Desk lamp", "Floor lamp", "Lamp shade", "Reading lamp", "Lava lamp",
            "Wall lamp", "Clip lamp", "Table lamp", "Mug", "Teapot",
            "Lamp oil", "Lamp post", "Kettle",
        ],
        start=1,
    )
]
# Two products sharing a SKU: a single-value lookup on it is ambiguous.
PRODUCTS.append({"id": "p98", "sku": "SKU-DUP", "name": "Duplicate A", "price": 1})
PRODUCTS.append({"id": "p99", "sku": "SKU-DUP", "name": "Duplicate B", "price": 2})
BY_ID = {p["id"]: p for p in PRODUCTS}

VALID_SID = "sid-valid"
EXPIRED_SID = "sid-expired"
CSRF = "csrf-123"


@dataclass
class State:
    broken: bool = False
    hits: Counter[str] = field(default_factory=Counter)
    flaky: Counter[str] = field(default_factory=Counter)
    cart: dict[str, int] = field(default_factory=dict)

    def reset(self) -> None:
        self.broken = False
        self.hits.clear()
        self.flaky.clear()
        self.cart.clear()


state = State()
app = FastAPI(title="fixture shop")


@app.middleware("http")
async def count_hits(request: Request, call_next: Any) -> Response:
    if not request.url.path.startswith("/_admin"):
        state.hits[f"{request.method} {request.url.path}"] += 1
    return await call_next(request)


# ---- admin -----------------------------------------------------------------


@app.post("/_admin/break")
def admin_break() -> dict[str, bool]:
    state.broken = True
    return {"broken": True}


@app.post("/_admin/reset")
def admin_reset() -> dict[str, bool]:
    state.reset()
    return {"broken": False}


@app.get("/_admin/hits")
def admin_hits() -> dict[str, int]:
    return dict(state.hits)


# ---- reads -----------------------------------------------------------------


@app.get("/api/products")
def search_products(q: str = "", start: int = 0, count: int = 5) -> Response:
    if start < 0 or count < 1 or count > 50:
        return JSONResponse({"error": "bad paging"}, status_code=400)
    matches = [p for p in PRODUCTS if q.lower() in p["name"].lower()] if q else []
    return JSONResponse({"results": matches[start : start + count], "total": len(matches)})


@app.get("/api/lookup")
def lookup(sku: str) -> dict[str, Any]:
    return {"matches": [{"id": p["id"]} for p in PRODUCTS if p["sku"] == sku]}


@app.get("/api/products/{pid}")
def product(pid: str) -> Response:
    if pid not in BY_ID:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"product": BY_ID[pid]})


def _item(pid: str) -> Response:
    if pid not in BY_ID:
        return JSONResponse({"error": "no such item"}, status_code=404)
    return JSONResponse({"item": BY_ID[pid]})


@app.get("/api/items/{pid}")
def item(pid: str) -> Response:
    if state.broken:  # the break switch: this endpoint has moved
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return _item(pid)


@app.get("/api/v2/items/{pid}")
def item_v2(pid: str) -> Response:
    if not state.broken:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return _item(pid)


@app.get("/api/gql")
def gql(queryId: str = "") -> Response:  # noqa: N803 - mirrors real sites' param names
    if queryId != "current":
        return JSONResponse({"errors": [{"message": "PersistedQueryNotFound"}]}, status_code=400)
    return JSONResponse({"data": {"items": PRODUCTS[:2]}})


@app.get("/api/me")
def me(sid: str | None = Cookie(default=None)) -> Response:
    if sid != VALID_SID:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    return JSONResponse({"user": {"name": "Test User"}})


@app.get("/api/account/orders")
def orders(request: Request, sid: str | None = Cookie(default=None)) -> Response:
    if sid == EXPIRED_SID:
        return RedirectResponse("/login?next=/account", status_code=302)
    if sid != VALID_SID or request.headers.get("x-csrf") != CSRF:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    return JSONResponse({"orders": [{"id": "o1", "total": 30}, {"id": "o2", "total": 12}]})


LOGIN_USER, LOGIN_PASSWORD = "demo", "demo-password"

LOGIN_FORM = """<!doctype html><html><head><title>Shop login</title>
<style>body{font:16px system-ui;margin:40px} input{display:block;margin:8px 0 16px;padding:8px;width:260px;font-size:16px}
button{padding:8px 16px;font-size:16px}</style></head><body>
<h1>Log in to the shop</h1>__ERROR__
<form method="post" action="/login">
<label>Username<input id="username" name="username" autocomplete="username"></label>
<label>Password<input id="password" name="password" type="password" autocomplete="current-password"></label>
<button id="submit" type="submit">Log in</button></form></body></html>"""


@app.get("/login")
def login_page() -> HTMLResponse:
    return HTMLResponse(LOGIN_FORM.replace("__ERROR__", ""))


@app.post("/login")
async def login_submit(request: Request) -> Response:
    form = parse_qs((await request.body()).decode())
    user, password = form.get("username", [""])[0], form.get("password", [""])[0]
    if (user, password) != (LOGIN_USER, LOGIN_PASSWORD):
        return HTMLResponse(LOGIN_FORM.replace("__ERROR__", "<p id='error'>Wrong username or password.</p>"), 401)
    resp = RedirectResponse("/account", status_code=303)
    resp.set_cookie("sid", VALID_SID, httponly=True)
    resp.set_cookie("csrftoken", CSRF)
    return resp


@app.get("/account")
def account_page(sid: str | None = Cookie(default=None)) -> HTMLResponse:
    if sid != VALID_SID:
        return HTMLResponse("<p>Not logged in.</p>", status_code=401)
    return HTMLResponse("<h1>Your account</h1><p>Logged in as demo.</p>")


@app.get("/api/browser-only/orders")
def browser_only_orders(request: Request, sid: str | None = Cookie(default=None)) -> Response:
    """Only answers fetch() from a real browser page (sec-fetch-mode: cors); everything else gets a challenge."""
    if request.headers.get("sec-fetch-mode") != "cors":
        return HTMLResponse(CHALLENGE_HTML, status_code=403)
    if sid != VALID_SID:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    return JSONResponse({"orders": [{"id": "o1", "total": 30}, {"id": "o2", "total": 12}]})


@app.get("/api/private")
def private() -> Response:
    return JSONResponse({"error": "unauthorized"}, status_code=401)


@app.get("/api/limited")
def limited() -> Response:
    return JSONResponse({"error": "slow down"}, status_code=429, headers={"Retry-After": "7"})


@app.get("/api/forbidden")
def forbidden() -> Response:
    return JSONResponse({"error": "forbidden"}, status_code=403)


@app.get("/api/flaky")
def flaky(key: str, fail: int = 1) -> Response:
    """Returns 500 for the first `fail` calls with this key, then succeeds."""
    state.flaky[key] += 1
    if state.flaky[key] <= fail:
        return JSONResponse({"error": "boom"}, status_code=500)
    return JSONResponse({"status": {"ok": True, "attempt": state.flaky[key]}})


@app.get("/api/slow")
async def slow(ms: int = 1000) -> dict[str, Any]:
    await asyncio.sleep(ms / 1000)
    return {"status": {"ok": True}}


# ---- bot-guarded endpoints (for the impersonate tier) ------------------------
# A real site would check the TLS fingerprint; here a Chrome User-Agent plus client-hint headers stand in.


def looks_like_browser(request: Request) -> bool:
    return "Chrome/" in request.headers.get("user-agent", "") and "sec-ch-ua" in request.headers


CHALLENGE_HTML = "<html><title>Just a moment...</title><div id='cf-challenge'></div></html>"


@app.get("/api/guarded/products")
def guarded_products(request: Request, q: str = "") -> Response:
    if not looks_like_browser(request):
        return HTMLResponse(CHALLENGE_HTML, status_code=403)
    return JSONResponse({"results": [p for p in PRODUCTS if q.lower() in p["name"].lower()][:5]})


@app.get("/api/guarded/flow-finish")
def guarded_flow_finish(request: Request, flow: str | None = Cookie(default=None)) -> Response:
    if not looks_like_browser(request):
        return HTMLResponse(CHALLENGE_HTML)  # 200 challenge page
    if flow != "flow-token-1":
        return JSONResponse({"error": "flow cookie missing"}, status_code=400)
    return JSONResponse({"result": [{"order_id": "o-guarded", "status": "done"}]})


@app.get("/api/guarded/token")
def guarded_token(request: Request) -> Response:
    if not looks_like_browser(request):
        return HTMLResponse(CHALLENGE_HTML, status_code=403)
    return JSONResponse({"token": "t-1"})


@app.post("/api/guarded/cart")
async def guarded_cart(request: Request) -> Response:
    """Hard block: non-browsers get a 403 and nothing is stored."""
    if not looks_like_browser(request):
        return HTMLResponse(CHALLENGE_HTML, status_code=403)
    body = await request.json()
    state.cart[str(body["sku"])] = state.cart.get(str(body["sku"]), 0) + 1
    return JSONResponse({"ok": True}, status_code=201)


@app.post("/api/guarded/cart-soft")
async def guarded_cart_soft(request: Request) -> Response:
    """Soft block: the write is applied, then a 200 challenge page is returned to non-browsers."""
    body = await request.json()
    state.cart[str(body["sku"])] = state.cart.get(str(body["sku"]), 0) + 1
    if not looks_like_browser(request):
        return HTMLResponse(CHALLENGE_HTML)
    return JSONResponse({"ok": True}, status_code=201)


@app.get("/api/echo")
def echo(request: Request) -> dict[str, Any]:
    return {"url": str(request.url), "headers": dict(request.headers), "cookies": dict(request.cookies)}


@app.get("/api/redirect-to-echo")
def redirect_to_echo() -> Response:
    resp = RedirectResponse("/api/echo?via=redirect", status_code=302)
    resp.set_cookie("hop", "1")
    return resp


@app.get("/api/challenge")
def challenge() -> HTMLResponse:
    return HTMLResponse("<html><title>Just a moment...</title><div id='cf-challenge'></div></html>")


@app.get("/api/changed")
def changed(mode: str = "structure") -> Response:
    if mode == "html":
        return HTMLResponse("<html>products moved</html>")
    if mode == "status":
        return Response(status_code=204)
    return JSONResponse({"results_v2": {"rows": PRODUCTS[:3]}})


@app.get("/api/flow/start")
def flow_start() -> Response:
    resp = JSONResponse({"step": "started"})
    resp.set_cookie("flow", "flow-token-1")
    return resp


@app.get("/api/flow/finish")
def flow_finish(token: str, flow: str | None = Cookie(default=None)) -> Response:
    if flow != "flow-token-1" or token != flow:
        return JSONResponse({"error": "flow cookie missing"}, status_code=400)
    return JSONResponse({"result": [{"order_id": "o-flow", "status": "done"}]})


# ---- writes ----------------------------------------------------------------


@app.post("/api/cart")
async def add_to_cart(request: Request) -> Response:
    """Magic SKU prefixes: SLOW- stores then stalls, FAIL- 500s without storing, GHOST- claims success."""
    body = await request.json()
    sku, qty = str(body.get("sku", "")), body.get("qty", 1)
    if not isinstance(qty, int):
        return JSONResponse({"error": "qty must be an integer"}, status_code=400)
    if sku.startswith("FAIL-"):
        return JSONResponse({"error": "boom"}, status_code=500)
    if not sku.startswith("GHOST-"):
        state.cart[sku] = state.cart.get(sku, 0) + qty
    if sku.startswith("SLOW-"):
        await asyncio.sleep(2)
    return JSONResponse({"ok": True}, status_code=201)


@app.get("/api/cart/{sku}")
def cart_item(sku: str) -> dict[str, Any]:
    return {"in_cart": sku in state.cart, "qty": state.cart.get(sku, 0)}
