"""Routes for the connect link. The unguessable, expiring token in the URL is their only credential, so the
gateway's Bearer check skips /connect/.

GET  /connect/{token}          the page the user opens: log in to the site, then click Connect
GET  /connect/{token}/status   JSON status, polled by the page
POST /connect/{token}/cookies  called by the extension with the captured cookies
"""

from __future__ import annotations

import html
import json
import logging

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import BaseRoute, Route

from runtime.connect import Connection, ConnectionManager, CookieImportError

log = logging.getLogger("runtime.connect_page")
MAX_BODY = 512_000
NO_STORE = {"cache-control": "no-store", "referrer-policy": "no-referrer"}

PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect __DOMAIN__</title>
<style>
  :root { color-scheme: light dark; --bg:#f7f7f5; --fg:#1c1c1a; --muted:#66665f; --accent:#2f5fb3; --ok:#1f7a4d; --bad:#b3261e; }
  @media (prefers-color-scheme: dark) { :root { --bg:#171716; --fg:#ececea; --muted:#a0a09a; --accent:#7aa2e8; --ok:#5fc48f; --bad:#f2827a; } }
  body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 system-ui, sans-serif; }
  main { max-width:560px; margin:48px auto; padding:0 16px; }
  h1 { font-size:22px; margin:0 0 4px; }
  p.lead { color:var(--muted); margin:0 0 24px; }
  ol { padding-left:22px; } li { margin:12px 0; }
  a { color:var(--accent); }
  button { font:inherit; padding:10px 18px; border-radius:6px; border:0; background:var(--accent); color:#fff; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  #status { margin-top:20px; min-height:1.5em; }
  #status.connected { color:var(--ok); font-weight:600; } #status.expired, #status.bad { color:var(--bad); }
  .hint { color:var(--muted); font-size:14px; }
  code { font-size:14px; }
</style></head>
<body><main>
<h1>Connect __DOMAIN__</h1>
<p class="lead">Your agent needs your __DOMAIN__ login. Your password stays with you: we only receive the site's
login session, and store it encrypted.</p>
<ol>
  <li><a href="__LOGIN_URL__" target="_blank" rel="noopener">Log in to __DOMAIN__</a> in this browser, as you
      normally would (skip this if you're already logged in).</li>
  <li id="ext-step">Then click <button id="connect" type="button" disabled>Connect __DOMAIN__</button>
      <div id="ext-missing" class="hint" hidden>The <strong>HTB Connect</strong> extension isn't installed in this
      browser. Install it (Chrome: <code>chrome://extensions</code> → Developer mode → Load unpacked → the
      <code>extension</code> folder), then reload this page.</div></li>
</ol>
<p id="status" role="status"></p>
</main>
<script>
const CFG = __CONFIG__;
const statusEl = document.getElementById("status"), btn = document.getElementById("connect");
const LABELS = {pending: "", connected: "Connected. You can close this tab and go back to your agent.",
                expired: "This link has expired. Ask your agent for a new one."};
function setStatus(s, text) { statusEl.className = s; statusEl.textContent = text ?? LABELS[s] ?? s; }
function ask(msg) {
  return new Promise((resolve) => {
    if (!(window.chrome && chrome.runtime && chrome.runtime.sendMessage)) return resolve(null);
    try { chrome.runtime.sendMessage(CFG.extensionId, msg, (r) => resolve(chrome.runtime.lastError ? null : r)); }
    catch (e) { resolve(null); }
  });
}
ask({type: "ping"}).then((r) => {
  if (r && r.ok) btn.disabled = false; else document.getElementById("ext-missing").hidden = false;
});
btn.addEventListener("click", async () => {
  const r = await ask({type: "connect", token: CFG.token, domain: CFG.domain, baseUrl: CFG.baseUrl, origins: CFG.origins});
  setStatus("pending", r && r.ok ? "Approve the request in the tab the extension opened…" : "The extension didn't respond. Reload and try again.");
});
async function poll() {
  try {
    const s = await (await fetch(location.pathname + "/status", {cache: "no-store"})).json();
    if (s.status !== "pending") { setStatus(s.status); btn.disabled = true; return; }
  } catch (e) {}
  setTimeout(poll, 1000);
}
poll();
</script></body></html>"""


def render_page(conn: Connection, extension_id: str, card_domain: str) -> str:
    config = {
        "token": conn.token,
        "domain": conn.domain,
        "baseUrl": conn.base_url,
        "origins": conn.cookie_origins(card_domain),
        "extensionId": extension_id,
    }
    safe_config = json.dumps(config).replace("<", "\\u003c")
    return (
        PAGE_HTML.replace("__DOMAIN__", html.escape(conn.domain))
        .replace("__LOGIN_URL__", html.escape(conn.login_url, quote=True))
        .replace("__CONFIG__", safe_config)
    )


def routes(manager: ConnectionManager, extension_id: str) -> list[BaseRoute]:
    async def page(request: Request) -> Response:
        conn = manager.by_token(request.path_params["token"])
        if conn is None:
            return HTMLResponse("<p>Unknown login link.</p>", status_code=404, headers=NO_STORE)
        return HTMLResponse(render_page(conn, extension_id, conn.domain), headers=NO_STORE)

    async def status(request: Request) -> Response:
        conn = manager.by_token(request.path_params["token"])
        if conn is None:
            return JSONResponse({"error": "unknown login link"}, status_code=404, headers=NO_STORE)
        return JSONResponse({"status": conn.status, "message": conn.message}, headers=NO_STORE)

    async def cookies(request: Request) -> Response:
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "request too large"}, status_code=413)
        try:
            payload = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        try:
            conn = await manager.import_cookies(
                request.path_params["token"], payload.get("cookies") if isinstance(payload, dict) else None
            )
        except CookieImportError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"status": conn.status, "message": conn.message})

    return [
        Route("/connect/{token}", page),
        Route("/connect/{token}/status", status),
        Route("/connect/{token}/cookies", cookies, methods=["POST"]),
    ]
