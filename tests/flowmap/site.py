"""A small bookshop that behaves like a modern site, for flowmap's capture, inference and healing tests.

Pages fetch JSON the way real sites do, with the traps a mapper must get right:
- search (GET /api/search): csrf header copied from a quoted cookie (JSESSIONID="ajax:..."), results nested
  under data.elements[].book, offset paging (start/count, 10 per page) with data.paging.total;
- a typeahead (GET /api/suggest) that sends only the first 3 characters typed (a partial marker);
- an analytics beacon to another host (localhost instead of 127.0.0.1) that carries the query (a decoy);
- a book page that fetches a one-time token first, then the book with it (a two-step action);
- add to cart (POST /api/cart, a write) with the csrf header.

Breaks, for healing (POST /_admin/break/{kind}, POST /_admin/reset); the pages follow the change, as a
redesigned site would: ``endpoint`` (/api/search → /api/v2/search), ``field`` (title → name),
``nested`` (elements → data.results.items) and ``param`` (q → query).

Run standalone:  uv run uvicorn tests.flowmap.site:app --port 8910   (open http://127.0.0.1:8910)
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

BREAKS = ("endpoint", "field", "nested", "param")
PAGE_SIZE = 10

_TITLES = [
    "The Quiet Harbour",
    "Salt and Iron",
    "A Map of Small Rooms",
    "Northern Lights",
    "The Glass Orchard",
    "Paper Birds",
    "Midnight Ferry",
    "The Long Garden",
    "Winter Letters",
    "Copper Moon",
    "Field Notes",
    "The Lantern Keeper",
    "River of Stones",
    "Summer Atlas",
    "The Last Lighthouse",
    "Harbour Lights",
    "Under the Linden",
    "The Silver Key",
    "Stone Circles",
    "Tidewater",
    "The Orchard Road",
    "Blue Hours",
    "Kettle and Ash",
]
# Each title also comes as a second edition, so common words find more than one page.
BOOKS: list[dict[str, Any]] = [
    {"id": f"b{i:03d}", "title": t, "author": f"Author {chr(65 + i % 26)}", "price": round(5 + i * 1.25, 2)}
    for i, t in enumerate(_TITLES + [f"{t} (Second Edition)" for t in _TITLES], start=1)
]
BY_ID = {b["id"]: b for b in BOOKS}


@dataclass
class State:
    breaks: set[str] = field(default_factory=set)
    tokens: set[str] = field(default_factory=set)
    carts: dict[str, list[str]] = field(default_factory=dict)

    def reset(self) -> None:
        self.breaks.clear()
        self.tokens.clear()
        self.carts.clear()


state = State()
app = FastAPI()


def _shape() -> dict[str, str]:
    b = state.breaks
    return {
        "search": "/api/v2/search" if "endpoint" in b else "/api/search",
        "param": "query" if "param" in b else "q",
        "title": "name" if "field" in b else "title",
        "list": "results.items" if "nested" in b else "elements",
    }


def _session(request: Request) -> tuple[str | None, str | None]:
    """(session id, csrf) from the JSESSIONID cookie, which holds a quoted "ajax:<n>"."""
    raw = request.cookies.get("JSESSIONID")
    if not raw:
        return None, None
    value = raw.strip('"')
    return value, value


def _csrf_ok(request: Request) -> bool:
    _, csrf = _session(request)
    return csrf is not None and request.headers.get("csrf-token") == csrf


def _book_out(book: dict[str, Any]) -> dict[str, Any]:
    out = dict(book)
    if "field" in state.breaks:
        out["name"] = out.pop("title")
    return out


# ---------- pages ----------

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Bookshop</title>
<style>body{font:15px system-ui;margin:32px}.result{padding:6px 0}</style></head><body>
<form id="f"><input id="q" placeholder="Search books" autocomplete="off">
<select id="sort" aria-label="Sort by"><option value="relevance">Relevance</option>
<option value="price">Price</option></select><button type="submit">Search</button></form>
<ul id="suggest"></ul><p id="count"></p><div id="results"></div>
<button id="next" hidden>Next</button>
<script>
const SHAPE = %SHAPE%;
const csrf = () => (document.cookie.match(/JSESSIONID="?([^";]+)/) || [])[1] || "";
let start = 0;
function pick(obj, path) { return path.split(".").reduce((o, k) => o && o[k], obj); }
document.getElementById("q").addEventListener("input", e => {
  const v = e.target.value;
  if (v.length >= 3) fetch("/api/suggest?q=" + encodeURIComponent(v.slice(0, 3)))
    .then(r => r.json()).then(d => {
      document.getElementById("suggest").innerHTML = d.suggestions.map(s => "<li>" + s + "</li>").join("");
    });
});
async function search() {
  const q = document.getElementById("q").value, sort = document.getElementById("sort").value;
  const url = SHAPE.search + "?" + SHAPE.param + "=" + encodeURIComponent(q) + "&sort=" + sort +
    "&start=" + start + "&count=10";
  fetch(BEACON + "/collect?e=search&term=" + encodeURIComponent(q), {method: "POST", mode: "no-cors"});
  const r = await fetch(url, {headers: {"csrf-token": csrf(), "accept": "application/json"}});
  const d = await r.json();
  const items = pick(d.data, SHAPE.list) || [];
  document.getElementById("count").textContent = d.data.paging.total + " results";
  document.getElementById("results").innerHTML = items.map(e =>
    '<div class="result"><h3><a href="/book/' + e.book.id + '">' + e.book[SHAPE.title] + "</a></h3><span>" +
    e.book.author + "</span></div>").join("");
  document.getElementById("next").hidden = start + 10 >= d.data.paging.total;
}
document.getElementById("f").addEventListener("submit", e => { e.preventDefault(); start = 0; search(); });
document.getElementById("next").addEventListener("click", () => { start += 10; search(); });
</script></body></html>"""

_BOOK_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Book</title></head><body>
<h1 id="title"></h1><p id="author"></p><p id="price"></p>
<button id="add">Add to cart</button><p id="cart"></p>
<script>
const SHAPE = %SHAPE%;
const csrf = () => (document.cookie.match(/JSESSIONID="?([^";]+)/) || [])[1] || "";
const id = location.pathname.split("/").pop();
(async () => {
  const t = await (await fetch("/api/token")).json();
  const d = await (await fetch("/api/books/" + id + "?token=" + t.token)).json();
  document.getElementById("title").textContent = d.book[SHAPE.title];
  document.getElementById("author").textContent = d.book.author;
  document.getElementById("price").textContent = d.book.price;
})();
document.getElementById("add").addEventListener("click", async () => {
  const r = await fetch("/api/cart", {method: "POST", headers: {"content-type": "application/json",
    "csrf-token": csrf()}, body: JSON.stringify({book_id: id, quantity: 1})});
  const d = await r.json();
  document.getElementById("cart").textContent = d.items.length + " in cart";
});
</script></body></html>"""


def _with_session(request: Request, html: str) -> HTMLResponse:
    response = HTMLResponse(html)
    if not request.cookies.get("JSESSIONID"):
        # raw header: Starlette's set_cookie would escape the quotes the way real sites don't
        value = f"ajax:{secrets.randbelow(10**18):018d}"
        response.headers.append("set-cookie", f'JSESSIONID="{value}"; Path=/; SameSite=Lax')
    return response


@app.get("/")
async def home(request: Request) -> HTMLResponse:
    beacon = f"http://localhost:{request.url.port}" if request.url.hostname == "127.0.0.1" else ""
    html = _PAGE.replace("%SHAPE%", json.dumps(_shape())).replace(
        "const SHAPE", f"const BEACON = {json.dumps(beacon)};\nconst SHAPE", 1
    )
    return _with_session(request, html)


@app.get("/book/{book_id}")
async def book_page(request: Request, book_id: str) -> HTMLResponse:
    return _with_session(request, _BOOK_PAGE.replace("%SHAPE%", json.dumps(_shape())))


# ---------- API ----------


async def _search(request: Request) -> Response:
    if not _csrf_ok(request):
        return JSONResponse({"error": "csrf"}, status_code=403)
    q = request.query_params.get(_shape()["param"], "").strip().lower()
    sort = request.query_params.get("sort", "relevance")
    start = int(request.query_params.get("start", "0"))
    count = min(int(request.query_params.get("count", str(PAGE_SIZE))), 50)
    hits = [b for b in BOOKS if not q or any(w in b["title"].lower() for w in q.split())]
    if sort == "price":
        hits.sort(key=lambda b: b["price"])
    page = [{"book": _book_out(b), "tracking": secrets.token_hex(4)} for b in hits[start : start + count]]
    data: dict[str, Any] = {"paging": {"start": start, "count": count, "total": len(hits)}}
    if "nested" in state.breaks:
        data["results"] = {"items": page}
    else:
        data["elements"] = page
    return JSONResponse({"data": data, "meta": {"request_id": secrets.token_hex(6)}})


@app.get("/api/search")
async def search(request: Request) -> Response:
    if "endpoint" in state.breaks:
        return JSONResponse({"error": "not found"}, status_code=404)
    return await _search(request)


@app.get("/api/v2/search")
async def search_v2(request: Request) -> Response:
    if "endpoint" not in state.breaks:
        return JSONResponse({"error": "not found"}, status_code=404)
    return await _search(request)


@app.get("/api/suggest")
async def suggest(q: str = "") -> JSONResponse:
    q = q.lower()
    return JSONResponse({"suggestions": [b["title"] for b in BOOKS if q and q in b["title"].lower()][:5]})


@app.get("/api/token")
async def token() -> JSONResponse:
    value = secrets.token_hex(8)
    state.tokens.add(value)
    return JSONResponse({"token": value, "expires_in": 60})


@app.get("/api/books/{book_id}")
async def book(book_id: str, token: str = "") -> Response:
    if token not in state.tokens:
        return JSONResponse({"error": "bad token"}, status_code=401)
    state.tokens.discard(token)
    found = BY_ID.get(book_id)
    if found is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"book": _book_out(found)})


@app.post("/api/cart")
async def add_to_cart(request: Request) -> Response:
    if not _csrf_ok(request):
        return JSONResponse({"error": "csrf"}, status_code=403)
    body = await request.json()
    if body.get("book_id") not in BY_ID:
        return JSONResponse({"error": "unknown book"}, status_code=400)
    sid, _ = _session(request)
    items = state.carts.setdefault(sid or "", [])
    items.extend([body["book_id"]] * int(body.get("quantity", 1)))
    return JSONResponse({"items": items})


@app.post("/collect")
async def collect() -> Response:
    return Response(status_code=204)


# ---------- admin ----------


@app.post("/_admin/break/{kind}")
async def break_site(kind: str) -> JSONResponse:
    if kind not in BREAKS:
        return JSONResponse({"error": f"kind is one of {', '.join(BREAKS)}"}, status_code=400)
    state.breaks.add(kind)
    return JSONResponse({"breaks": sorted(state.breaks)})


@app.post("/_admin/reset")
async def reset() -> JSONResponse:
    state.reset()
    return JSONResponse({"breaks": []})
