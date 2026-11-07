"""The streamed login page: GET /connect/{token} shows the server-side browser, and a WebSocket carries
screenshots (Chrome DevTools screencast) down and the user's clicks and typing up. The unguessable,
expiring token in the URL is the page's only credential, so these routes skip the gateway's Bearer check."""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from playwright.async_api import Error as PlaywrightError
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import BaseRoute, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from runtime.browser.connect import Connection, ConnectionManager
from runtime.browser.pool import VIEWPORT

log = logging.getLogger("runtime.stream")

SPECIAL_KEYS = {
    "Enter", "Backspace", "Tab", "Escape", "Delete", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
    "Home", "End", "PageUp", "PageDown",
}

PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect __DOMAIN__</title>
<style>
  :root { color-scheme: light dark; --bg:#f6f6f4; --fg:#1d1d1b; --muted:#6b6b66; --ok:#1f7a4d; --bad:#b3261e; }
  @media (prefers-color-scheme: dark) { :root { --bg:#161615; --fg:#ececea; --muted:#9a9a94; --ok:#5fc48f; --bad:#f2827a; } }
  body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.4 system-ui, sans-serif; }
  header { padding:12px 16px; display:flex; gap:12px; align-items:baseline; flex-wrap:wrap; }
  h1 { font-size:16px; margin:0; }
  #status { color:var(--muted); }
  #status.connected { color:var(--ok); font-weight:600; } #status.failed, #status.expired { color:var(--bad); }
  #wrap { padding:0 16px 16px; }
  #screen { width:100%; max-width:1280px; aspect-ratio:1280/800; display:block; border-radius:8px;
            outline:1px solid color-mix(in srgb, var(--fg) 15%, transparent); cursor:pointer; background:#fff; }
  #screen:focus { outline:2px solid #4a7bd0; }
  p.note { color:var(--muted); font-size:13px; margin:8px 0 0; }
</style></head>
<body>
<header><h1>Log in to __DOMAIN__</h1><span id="status">Connecting to the browser…</span></header>
<div id="wrap">
  <img id="screen" tabindex="0" alt="Remote browser showing the __DOMAIN__ login page">
  <p class="note">This is a browser running on our server. Click to focus, then click and type as usual.
  Your password goes straight to __DOMAIN__; only the resulting login session is kept.</p>
</div>
<script>
const W = __W__, H = __H__;
const img = document.getElementById('screen'), statusEl = document.getElementById('status');
const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + location.pathname + '/ws');
const send = (m) => { if (ws.readyState === 1) ws.send(JSON.stringify(m)); };
const LABELS = {pending: 'Waiting for you to log in…', connected: 'Connected. You can close this tab.',
                expired: 'This link expired. Ask for a new one.', failed: 'Something went wrong. Ask for a new link.'};
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.t === 'frame') img.src = 'data:image/jpeg;base64,' + m.data;
  if (m.t === 'status') { statusEl.textContent = LABELS[m.status] || m.status; statusEl.className = m.status; }
};
ws.onclose = () => { if (!statusEl.className) statusEl.textContent = 'Disconnected.'; };
const pos = (e) => { const r = img.getBoundingClientRect();
  return {x: Math.round((e.clientX - r.left) * W / r.width), y: Math.round((e.clientY - r.top) * H / r.height)}; };
img.addEventListener('click', (e) => { img.focus(); send({t: 'click', ...pos(e)}); });
img.addEventListener('wheel', (e) => { e.preventDefault(); send({t: 'wheel', ...pos(e), dy: Math.round(e.deltaY)}); }, {passive: false});
img.addEventListener('keydown', (e) => {
  if (e.metaKey || e.ctrlKey) return;  // let copy/paste shortcuts through
  if (e.key.length === 1) { send({t: 'text', text: e.key}); e.preventDefault(); }
  else if (__KEYS__.includes(e.key)) { send({t: 'key', key: e.key}); e.preventDefault(); }
});
img.addEventListener('paste', (e) => { const t = e.clipboardData.getData('text'); if (t) send({t: 'text', text: t}); e.preventDefault(); });
</script></body></html>"""


def _page(conn: Connection) -> str:
    keys = "[" + ",".join(f"'{k}'" for k in sorted(SPECIAL_KEYS)) + "]"
    return (
        PAGE_HTML.replace("__DOMAIN__", conn.domain)
        .replace("__W__", str(VIEWPORT["width"]))
        .replace("__H__", str(VIEWPORT["height"]))
        .replace("__KEYS__", keys)
    )


def routes(manager: ConnectionManager) -> list[BaseRoute]:
    async def page(request: Request) -> Response:
        conn = manager.by_token(request.path_params["token"])
        if conn is None:
            return HTMLResponse("<p>Unknown or expired login link.</p>", status_code=404)
        return HTMLResponse(_page(conn), headers={"cache-control": "no-store", "referrer-policy": "no-referrer"})

    async def stream(ws: WebSocket) -> None:
        conn = manager.by_token(ws.path_params["token"])
        if conn is None:
            await ws.close(code=4404)
            return
        await ws.accept()
        try:
            await _serve(ws, conn)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("login stream failed")

    return [Route("/connect/{token}", page), WebSocketRoute("/connect/{token}/ws", stream)]


async def _serve(ws: WebSocket, conn: Connection) -> None:
    sent_status: str | None = None

    async def push_status() -> None:
        nonlocal sent_status
        if conn.status != sent_status:
            sent_status = conn.status
            await ws.send_json({"t": "status", "status": conn.status})

    await push_status()
    for _ in range(100):  # the watcher opens the page right after start_connection
        if conn.page is not None or conn.status != "pending":
            break
        await asyncio.sleep(0.1)
    page = conn.page
    if page is None:
        await push_status()
        await ws.close()
        return

    frames: asyncio.Queue[str] = asyncio.Queue(maxsize=2)
    cdp = await page.context.new_cdp_session(page)

    async def on_frame(event: dict[str, Any]) -> None:
        try:
            await cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
        except PlaywrightError:
            return
        if frames.full():
            frames.get_nowait()  # drop the stale frame: the latest one is what matters
        frames.put_nowait(event["data"])

    cdp.on("Page.screencastFrame", lambda e: asyncio.ensure_future(on_frame(e)))
    await cdp.send(
        "Page.startScreencast",
        {"format": "jpeg", "quality": 70, "maxWidth": VIEWPORT["width"], "maxHeight": VIEWPORT["height"]},
    )
    first = await page.screenshot(type="jpeg", quality=70)
    await ws.send_json({"t": "frame", "data": base64.b64encode(first).decode()})

    async def pump_frames() -> None:
        while True:
            data = await frames.get()
            await ws.send_json({"t": "frame", "data": data})

    pump = asyncio.create_task(pump_frames())
    try:
        while conn.status == "pending" and conn.page is not None:
            await push_status()
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=0.5)
            except TimeoutError:
                continue
            await _apply_input(page, msg)
        await push_status()
    finally:
        pump.cancel()
        try:
            await cdp.send("Page.stopScreencast")
            await cdp.detach()
        except PlaywrightError:
            pass
    await ws.close()


async def _apply_input(page: Any, msg: Any) -> None:
    if not isinstance(msg, dict):
        return
    kind = msg.get("t")
    try:
        if kind in ("click", "wheel"):
            x, y = int(msg.get("x", -1)), int(msg.get("y", -1))
            if not (0 <= x <= VIEWPORT["width"] and 0 <= y <= VIEWPORT["height"]):
                return
            if kind == "click":
                await page.mouse.click(x, y)
            else:
                await page.mouse.move(x, y)
                await page.mouse.wheel(0, max(-2000, min(2000, int(msg.get("dy", 0)))))
        elif kind == "text":
            text = msg.get("text")
            if isinstance(text, str) and 0 < len(text) <= 1000:
                await page.keyboard.insert_text(text)
        elif kind == "key":
            if msg.get("key") in SPECIAL_KEYS:
                await page.keyboard.press(msg["key"])
    except PlaywrightError as e:
        log.debug("input %s failed: %s", kind, e.message.splitlines()[0])
