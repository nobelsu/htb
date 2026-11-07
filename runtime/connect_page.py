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
import html
import logging

import httpx
import websockets
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from runtime.connect import Connection, ConnectError, ConnectionManager
from runtime.neko.manager import NekoSession

log = logging.getLogger("runtime.connect_page")
NO_STORE = {"cache-control": "no-store", "referrer-policy": "no-referrer"}
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade", "host", "content-length", "content-encoding",
}

PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in to __DOMAIN__</title>
<style>
  :root { color-scheme: light dark; --bg:#f7f7f5; --fg:#1c1c1a; --muted:#66665f; --line:#d9d9d4; --accent:#2f5fb3;
          --ok:#1f7a4d; --ok-bg:#e6f3ec; --bad:#b3261e; }
  @media (prefers-color-scheme: dark) { :root { --bg:#171716; --fg:#ececea; --muted:#a0a09a; --line:#33332f;
          --accent:#7aa2e8; --ok:#5fc48f; --ok-bg:#1b2e24; --bad:#f2827a; } }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 system-ui, sans-serif; }
  main { max-width:1280px; margin:32px auto; padding:0 16px; }
  .intro { max-width:640px; }
  h1 { font-size:22px; margin:0 0 4px; }
  p { margin:0 0 12px; } .muted { color:var(--muted); }
  button { font:inherit; padding:10px 18px; border-radius:6px; border:0; background:var(--accent); color:#fff; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  .actions { display:flex; gap:12px; flex-wrap:wrap; align-items:center; margin:16px 0; }
  #viewer { display:none; margin-top:8px; }
  #viewer iframe { width:100%; aspect-ratio:__W__/__H__; border:1px solid var(--line); border-radius:8px; background:#000; display:block; }
  #status { min-height:1.5em; margin-top:8px; }
  #status.bad, #status.expired { color:var(--bad); }
  .hint { color:var(--muted); font-size:14px; }
  #done-panel { display:none; margin-top:24px; padding:28px 24px; border-radius:12px; background:var(--ok-bg);
                border:1px solid color-mix(in srgb, var(--ok) 35%, transparent); max-width:640px; }
  #done-panel .check { font-size:40px; line-height:1; color:var(--ok); }
  #done-panel h2 { margin:12px 0 6px; font-size:22px; color:var(--ok); }
</style></head>
<body><main>
<div class="intro" id="intro">
  <h1>Sign in to __DOMAIN__</h1>
  <p class="muted">Your agent needs your __DOMAIN__ account. You sign in yourself, in a private browser below; your
  password is never shared with the agent or with us. We only keep the site's sign-in, encrypted.</p>
  <div class="actions"><button id="browser" type="button">Log in to __DOMAIN__</button></div>
  <p id="status" role="status"></p>
</div>
<div id="viewer"><iframe id="frame" title="Private browser for signing in to __DOMAIN__"
  allow="autoplay; fullscreen; clipboard-read; clipboard-write"></iframe>
  <div class="actions"><button id="done" type="button">I've signed in</button>
  <span id="done-hint" class="hint"></span></div>
  <p class="hint">This browser runs on our server, just for this sign-in, and closes once you're signed in.</p></div>
<div id="done-panel" role="status" aria-live="polite">
  <div class="check" aria-hidden="true">&#10003;</div>
  <h2>You're signed in to __DOMAIN__</h2>
  <p>You can close this tab and go back to your agent. It will carry on by itself.</p>
</div>
</main>
<script>
const $ = (id) => document.getElementById(id);
const statusEl = $("status");
function setStatus(s, text) { statusEl.className = s; statusEl.textContent = text || ""; }
function showSignedIn() {
  $("viewer").style.display = "none"; $("frame").src = "about:blank"; $("intro").style.display = "none";
  $("done-panel").style.display = "block"; document.title = "Signed in to __DOMAIN__";
}
$("browser").addEventListener("click", async () => {
  $("browser").disabled = true;
  setStatus("pending", "Starting a private browser…");
  try {
    const resp = await fetch(location.pathname + "/browser", {method: "POST"});
    const body = await resp.json();
    if (!resp.ok) throw new Error(body.error || ("HTTP " + resp.status));
    $("frame").src = body.viewer;
    $("viewer").style.display = "block";
    $("done-hint").textContent = body.confirm
      ? "Click this once you've finished signing in."
      : "We'll notice when you're signed in, or click this to check now.";
    setStatus("pending", "Sign in to __DOMAIN__ below. Click inside the browser to start.");
    $("viewer").scrollIntoView({behavior: "smooth", block: "start"});
  } catch (e) {
    setStatus("bad", "Couldn't start the browser: " + e.message);
    $("browser").disabled = false;
  }
});
$("done").addEventListener("click", async () => {
  $("done").disabled = true;
  try {
    const resp = await fetch(location.pathname + "/done", {method: "POST"});
    const body = await resp.json();
    if (resp.ok) showSignedIn();
    else setStatus("bad", body.error || ("HTTP " + resp.status));
  } catch (e) {
    setStatus("bad", "Couldn't check: " + e.message);
  }
  $("done").disabled = false;
});
async function poll() {
  try {
    const s = await (await fetch(location.pathname + "/status", {cache: "no-store"})).json();
    if (s.status === "connected") { showSignedIn(); return; }
    if (s.status === "expired") {
      $("viewer").style.display = "none"; $("browser").disabled = true;
      setStatus("expired", "This link has expired. Ask your agent for a new one."); return;
    }
  } catch (e) {}
  setTimeout(poll, 1000);
}
poll();
</script></body></html>"""


def render_page(conn: Connection, width: int = 1280, height: int = 800) -> str:
    return (
        PAGE_HTML.replace("__DOMAIN__", html.escape(conn.domain))
        .replace("__W__", str(width))
        .replace("__H__", str(height))
    )


def routes(manager: ConnectionManager) -> list[BaseRoute]:
    http = httpx.AsyncClient(timeout=30, follow_redirects=False)
    size = (manager.neko.config.width, manager.neko.config.height) if manager.neko else (1280, 800)

    async def page(request: Request) -> Response:
        conn = manager.by_token(request.path_params["token"])
        if conn is None:
            return HTMLResponse("<p>Unknown login link.</p>", status_code=404, headers=NO_STORE)
        return HTMLResponse(render_page(conn, *size), headers=NO_STORE)

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
            except RuntimeError:
                pass

    return [
        Route("/connect/{token}", page),
        Route("/connect/{token}/status", status),
        Route("/connect/{token}/browser", browser, methods=["POST"]),
        Route("/connect/{token}/done", done, methods=["POST"]),
        Route("/neko/{token}", neko_root),
        WebSocketRoute("/neko/{token}/api/ws", neko_ws),
        Route("/neko/{token}/{path:path}", neko_http, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]),
    ]
