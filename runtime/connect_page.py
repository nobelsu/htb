"""Routes for the connect link. The unguessable, expiring token in the URL is their only credential, so the
gateway's Bearer check skips /connect/ and /neko/.

GET  /connect/{token}          the page the user opens
GET  /connect/{token}/status   JSON status, polled by the page
POST /connect/{token}/browser  start the login browser; returns the viewer URL
POST /connect/{token}/done     the user clicked "I've signed in"
*    /neko/{token}/...         reverse proxy to that login's Neko container (HTTP + its signalling WebSocket)
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import websockets
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from runtime.connect import Connection, ConnectError, ConnectionManager
from runtime.neko.manager import NekoSession
from runtime.ui import bar_link, esc, icon, short_name
from runtime.ui import page as spine_page

log = logging.getLogger("runtime.connect_page")
NO_STORE = {"cache-control": "no-store", "referrer-policy": "no-referrer"}
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade", "host", "content-length", "content-encoding",
}

CONNECT_SCRIPT = """<script>
const $ = (id) => document.getElementById(id);
const wrap = document.querySelector(".wrap"), word = document.querySelector(".word");
function setStatus(text) {
  const s = $("status"); s.textContent = text || ""; s.hidden = !text; s.className = text ? "notice bad" : "notice";
}
function show(state) {
  for (const id of ["start", "viewer", "done-panel", "expired-panel"]) $(id).hidden = id !== state;
}
function signedIn() {
  $("frame").src = "about:blank"; show("done-panel"); setStatus("");
  wrap.classList.add("inv"); word.textContent = "Done"; word.style.fontSize = "min(200px, calc((100vh - 150px) / 2.4))";
  document.title = "Signed in to __DOMAIN__";
}
function expired() {
  $("frame").src = "about:blank"; show("expired-panel"); setStatus("");
  word.textContent = "Expired"; word.style.fontSize = "min(180px, calc((100vh - 150px) / 4.2))";
}
$("browser").addEventListener("click", async () => {
  $("browser").disabled = true; setStatus("");
  try {
    const resp = await fetch(location.pathname + "/browser", {method: "POST"});
    const body = await resp.json();
    if (!resp.ok) throw new Error(body.error || ("HTTP " + resp.status));
    $("frame").src = body.viewer; show("viewer"); $("frame").focus();
  } catch (e) {
    setStatus("The private browser didn't start: " + e.message);
  }
  $("browser").disabled = false;
});
$("done").addEventListener("click", async () => {
  $("done").disabled = true;
  try {
    const resp = await fetch(location.pathname + "/done", {method: "POST"});
    const body = await resp.json();
    if (resp.ok) signedIn(); else setStatus(body.error || ("HTTP " + resp.status));
  } catch (e) {
    setStatus("Couldn't check: " + e.message);
  }
  $("done").disabled = false;
});
async function poll() {
  try {
    const s = await (await fetch(location.pathname + "/status", {cache: "no-store"})).json();
    if (s.status === "connected") { signedIn(); return; }
    if (s.status === "expired") { expired(); return; }
  } catch (e) {}
  setTimeout(poll, 1000);
}
poll();
</script>"""


def render_page(conn: Connection, width: int = 1280, height: int = 800) -> Response:
    """The website sign-in page, in the Spine design: start, live browser, done (inverted), expired."""
    domain = esc(conn.domain)
    body = (
        '<p id="status" class="notice" role="alert" hidden></p>'
        '<section id="start" class="state">'
        f'<h1 class="headline" style="padding-top:88px">Sign in to {domain}</h1>'
        f'<div class="center">{icon("lock", 120, 1.2, label="Private and encrypted")}</div>'
        f'<button id="browser" class="bar" type="button">Open private browser{icon("arrow", 44)}</button>'
        "</section>"
        '<section id="viewer" class="state" hidden>'
        f'<iframe id="frame" title="Private browser for signing in to {domain}" '
        f'style="width:100%;aspect-ratio:{width}/{height};border:0;border-bottom:3px solid #000;background:#000;display:block" '
        'allow="autoplay; fullscreen; clipboard-read; clipboard-write"></iframe>'
        f'<button id="done" class="bar" type="button">I have signed in{icon("arrow", 44)}</button>'
        "</section>"
        '<section id="done-panel" class="state" hidden aria-live="polite">'
        f'<div class="center">{icon("check", 96, 1.2)}<p class="giant">You&#39;re in.<br>Close this tab.</p></div>'
        "</section>"
        '<section id="expired-panel" class="state" hidden>'
        f'<div class="center">{icon("clock", 96, 1.2)}<p class="giant">This link expired.</p></div>'
        '<a class="bar" href="/account">Go to your account' + icon("arrow", 44) + "</a>"
        "</section>"
    )
    return spine_page(
        title=f"Sign in to {conn.domain}", spine=short_name(conn.domain), body=body,
        script=CONNECT_SCRIPT.replace("__DOMAIN__", domain),
    )


def routes(manager: ConnectionManager) -> list[BaseRoute]:
    http = httpx.AsyncClient(timeout=30, follow_redirects=False)
    size = (manager.neko.config.width, manager.neko.config.height) if manager.neko else (1280, 800)

    async def page(request: Request) -> Response:
        conn = manager.by_token(request.path_params["token"])
        if conn is None:
            return spine_page(title="Link expired", spine="Expired", status=404, body=(
                f'<div class="center">{icon("clock", 96, 1.2)}<h1 class="giant">This link expired.</h1></div>'
                + bar_link("Go to your account", "/account")))
        manager.mark_opened(conn.token)
        return render_page(conn, *size)

    async def status(request: Request) -> Response:
        conn = manager.by_token(request.path_params["token"])
        if conn is None:
            return JSONResponse({"error": "unknown login link"}, status_code=404, headers=NO_STORE)
        return JSONResponse({"status": conn.status, "message": conn.message}, headers=NO_STORE)

    async def browser(request: Request) -> Response:
        try:
            started = await manager.start_browser_login(request.path_params["token"])
        except ConnectError as e:
            return JSONResponse({"error": str(e)}, status_code=400, headers=NO_STORE)
        return JSONResponse(started, headers=NO_STORE)

    async def done(request: Request) -> Response:
        try:
            conn = await manager.confirm_browser_login(request.path_params["token"])
        except ConnectError as e:
            return JSONResponse({"error": str(e)}, status_code=400, headers=NO_STORE)
        return JSONResponse({"status": conn.status, "message": conn.message}, headers=NO_STORE)


    def session_for(token: str) -> NekoSession | None:
        conn = manager.by_token(token)
        if conn is None or conn.status != "pending" or manager.neko is None:
            return None
        return manager.neko.get(token)

    async def neko_root(request: Request) -> Response:
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"/neko/{request.path_params['token']}/{query}", status_code=307)

    async def neko_http(request: Request) -> Response:
        session = session_for(request.path_params["token"])
        if session is None:
            return Response("login browser not available", status_code=404)
        url = session.upstream + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP]
        try:
            upstream = await http.request(request.method, url, headers=headers, content=await request.body())
        except httpx.HTTPError:
            return Response("login browser not reachable", status_code=502)
        out = Response(content=upstream.content, status_code=upstream.status_code)
        for k, v in upstream.headers.multi_items():
            if k.lower() not in HOP_BY_HOP:
                out.headers.append(k, v)
        return out

    async def neko_ws(ws: WebSocket) -> None:
        session = session_for(ws.path_params["token"])
        if session is None:
            await ws.close(code=4404)
            return
        path = ws.url.path + (f"?{ws.url.query}" if ws.url.query else "")
        extra = [("cookie", ws.headers["cookie"])] if "cookie" in ws.headers else []
        try:
            upstream = await websockets.connect(
                f"ws://127.0.0.1:{session.http_port}{path}", additional_headers=extra, max_size=None
            )
        except (OSError, websockets.WebSocketException):
            await ws.close(code=1011)
            return
        await ws.accept()

        async def client_to_upstream() -> None:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])

        async def upstream_to_client() -> None:
            async for data in upstream:
                if isinstance(data, str):
                    await ws.send_text(data)
                else:
                    await ws.send_bytes(data)

        tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except WebSocketDisconnect:
            pass
        finally:
            for t in tasks:
                t.cancel()
            await upstream.close()
            try:
                await ws.close()
            except (RuntimeError, WebSocketDisconnect):
                pass  # the viewer tab already went away (normal once the user is signed in)

    return [
        Route("/connect/{token}", page),
        Route("/connect/{token}/status", status),
        Route("/connect/{token}/browser", browser, methods=["POST"]),
        Route("/connect/{token}/done", done, methods=["POST"]),
        Route("/neko/{token}", neko_root),
        WebSocketRoute("/neko/{token}/api/ws", neko_ws),
        Route("/neko/{token}/{path:path}", neko_http, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]),
    ]
