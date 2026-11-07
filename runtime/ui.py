"""The Margin design every page shares (DECISIONS.md L13): a left panel where a spider spins orb webs, a hairline,
and a calm column of small type on the right, centred vertically. Linen palette (paper, ink, umber, brick), IBM
Plex Sans, stroke icons instead of symbols, minimal copy. Errors show next to the field they are about.
"""

from __future__ import annotations

import html
import os
from collections.abc import Iterable

from starlette.responses import HTMLResponse

PRODUCT_NAME = os.environ.get("PRODUCT_NAME", "spider")
HEADERS = {"cache-control": "no-store", "referrer-policy": "no-referrer", "x-frame-options": "DENY"}

FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500'
    '&family=IBM+Plex+Mono:wght@400&display=swap">'
)

# Linen: paper, ink, a muted ink, umber for actions, brick for errors. `.inv` is the same palette at night (the
# finished website sign-in turns dark).
CSS = """
:root,.wrap{--paper:#F5EDE2;--ink:#2A2421;--muted:#6A5E55;--action:#6B5A35;--on-action:#F5EDE2;--error:#9C3F33;
  --line:#E0D3C1;--tint:#EDE2D2}
.wrap.inv{--paper:#2A2421;--ink:#F5EDE2;--muted:#BBAE9F;--action:#D9C08A;--on-action:#2A2421;--error:#E8A08C;
  --line:#4A403A;--tint:#352D29}
*{box-sizing:border-box}
html,body{margin:0;background:#F5EDE2}
body{font-family:'IBM Plex Sans',Arial,sans-serif;font-size:14px;line-height:1.5;color:#2A2421}
a{color:inherit}
.wrap{display:flex;min-height:100vh;background:var(--paper);color:var(--ink);transition:background-color .8s,color .8s}
.art{position:relative;flex:1 1 420px;min-height:100vh;overflow:hidden}
.art canvas{position:absolute;inset:0;width:100%;height:100%;display:block}
.brand{position:absolute;z-index:1;top:32px;left:40px;font-weight:500;text-decoration:none}
.foot{position:absolute;z-index:1;left:40px;right:40px;bottom:32px;display:flex;flex-wrap:wrap;gap:8px 20px;
  align-items:center}
.foot a,.foot button{font:inherit;color:var(--action);background:none;border:0;padding:0;cursor:pointer;
  text-decoration:underline;text-underline-offset:3px}
.hair{flex:none;width:1px;background:var(--ink);opacity:.18}
.main{flex:0 1 460px;min-width:0;display:flex;flex-direction:column;justify-content:center;padding:64px 56px}
.wrap.wide .art{flex:0 0 140px}
.wrap.wide .main{flex:1 1 auto}
.title,.headline,.giant{margin:0 0 20px;font-size:15px;font-weight:500;line-height:1.4;overflow-wrap:anywhere}
.main a:not(.bar):not(.tile):not(.row){color:var(--action)}
.rule{border-top:1px solid var(--line);margin:4px 0}
.row{display:flex;justify-content:space-between;align-items:center;gap:16px;min-height:48px;
  border-bottom:1px solid var(--line);text-decoration:none;color:inherit}
.row.strong{background:var(--tint);border-bottom:0;padding:6px 6px 6px 14px;margin-bottom:6px}
.big,.mid{display:flex;align-items:center;gap:10px;min-width:0;overflow-wrap:anywhere}
.struck{text-decoration:line-through;opacity:.5}
.mono{font-family:'IBM Plex Mono',monospace;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  min-width:0;color:var(--muted)}
.pill,.bar{display:inline-flex;align-items:center;gap:10px;flex:none;font:inherit;font-weight:500;
  background:var(--action);color:var(--on-action);border:0;cursor:pointer;text-decoration:none}
.pill{padding:8px 14px;min-height:40px;font-size:13px}
.bar{align-self:flex-start;padding:12px 20px;min-height:44px;margin-top:12px}
.sq{width:44px;height:44px;flex:none;display:inline-flex;align-items:center;justify-content:center;border:0;
  background:transparent;color:var(--muted);cursor:pointer;padding:0}
.sq:hover{color:var(--ink)}
form.stack{display:flex;flex-direction:column;gap:22px;margin:0}
.fld{display:flex;flex-direction:column;gap:4px}
.lbl{font-size:12px;color:var(--muted)}
.field{font:inherit;color:inherit;background:transparent;border:0;border-bottom:1px solid var(--ink);padding:6px 0 7px;
  outline:none;width:100%;border-radius:0}
.field:focus{border-bottom:2px solid var(--action);padding-bottom:6px}
.field[aria-invalid="true"]{border-bottom-color:var(--error)}
.fld:has(.field[aria-invalid="true"]) .lbl{color:var(--error)}
.ferr{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--error)}
.notice{display:flex;align-items:center;gap:8px;margin:0 0 20px;font-size:13px;color:var(--error)}
.notice.info{color:var(--muted)}
.tiles{display:flex;flex-wrap:wrap;gap:8px;margin-top:28px}
.tile{display:inline-flex;align-items:center;gap:8px;font:inherit;font-size:13px;color:inherit;background:transparent;
  border:1px solid var(--line);padding:10px 14px;min-height:44px;cursor:pointer;text-decoration:none}
.tile:hover{background:var(--tint)}
.tile.on{background:var(--ink);color:var(--paper);border-color:var(--ink)}
.center{display:flex;flex-direction:column;align-items:flex-start;gap:16px}
.center > svg{width:28px;height:28px;color:var(--muted)}
.state{display:flex;flex-direction:column}
.empty{color:var(--muted);margin:8px 0}
.main svg,.foot svg{width:16px;height:16px}
.notice svg,.ferr svg{width:14px;height:14px;flex:none}
[hidden]{display:none!important}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
:focus-visible{outline:2px solid var(--action);outline-offset:2px}
.field:focus,.field:focus-visible{outline:none}
@media (max-width:860px){
  .wrap{flex-direction:column}
  .art{flex:none;height:240px;min-height:240px}
  .wrap.wide .art{flex:none;height:120px;min-height:120px}
  .hair{display:none}
  .main{flex:1 1 auto;padding:32px 20px 48px;justify-content:flex-start}
  .brand{top:20px;left:20px}
  .foot{left:20px;right:20px;bottom:16px}
}
"""

# Motion, kept calm: lines rise into place on load, buttons darken on hover, waiting sign-ins breathe. All of it
# stops for people who ask their system to reduce motion.
MOTION_CSS = """
.pill,.bar,.tile,.sq{transition:filter .2s,background-color .2s,color .2s}
.pill:hover,.bar:hover{filter:brightness(.9)}
.field{transition:border-color .2s}
.reveal{animation:rise .6s cubic-bezier(.2,.7,.1,1) both;animation-delay:calc(var(--i, 0) * 50ms + 80ms)}
@keyframes rise{from{transform:translateY(8px);opacity:0}to{transform:none;opacity:1}}
.arrive{animation:rise .5s cubic-bezier(.2,.7,.1,1) both}
.pulse{animation:pulse 2s ease-out infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb, var(--action) 40%, transparent)}
  100%{box-shadow:0 0 0 10px transparent}}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation:none!important;transition:none!important}
}
"""

MOTION_SCRIPT = """<script>
(() => {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  let i = 0;
  for (const el of document.querySelectorAll(".main > *:not(script), .main .stack > *, .main .state > *")) {
    if (el.closest("[hidden]") || el.classList.contains("stack") || el.classList.contains("state")) continue;
    el.style.setProperty("--i", i++); el.classList.add("reveal");
  }
  document.addEventListener("animationend", (e) => {
    if (e.animationName === "rise") e.target.classList.remove("reveal", "arrive");
  }, true);
})();
</script>"""

# The left panel: a spider spins an orb web (radials in a scrambled order, the frame, then the spiral), rests,
# wanders off and spins the next; old webs stay and fade back, the oldest go. It never moves quite regularly.
# Colours come from the page's palette. With reduced motion: one finished web and a resting spider.
SPIDER_SCRIPT = """<script>
(() => {
  const el = document.getElementById("web");
  if (!el) return;
  const wrap = document.querySelector(".wrap");
  const ctx = el.getContext("2d");
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const KEEP = 6, SPEED = 1, JITTER = 1;
  const rnd = (a, b) => a + Math.random() * (b - a);
  let col = {};
  const readColours = () => {
    const cs = getComputedStyle(wrap);
    col = {ink: cs.getPropertyValue("--ink").trim(), dew: cs.getPropertyValue("--action").trim(),
           mark: cs.getPropertyValue("--error").trim()};
  };
  const s = {w: 1, h: 1, dpr: 1, web: null, next: null, route: [], old: [], mi: 0, p: 0, phase: "build",
    timer: 0, wait: 0, look: 0, lookAt: 0, x: 0, y: 0, heading: 0, moving: false, t: 0, last: 0};
  const mood = (m) => {
    m.pace = Math.max(0.25, 1 + rnd(-0.45, 0.55) * JITTER);
    m.pause = Math.random() < 0.16 * JITTER ? rnd(0.15, 1.1) : 0;
    return m;
  };
  const makeWeb = (w, h) => {
    const R = Math.min(w, h) * rnd(0.26, 0.42), pad = R * 0.95;
    const cx = pad + Math.random() * Math.max(1, w - 2 * pad), cy = pad + Math.random() * Math.max(1, h - 2 * pad);
    const n = 12 + Math.floor(Math.random() * 8), rot = Math.random() * Math.PI * 2;
    const spokes = [];
    for (let i = 0; i < n; i++) {
      const a = rot + (i / n) * Math.PI * 2 + rnd(-0.12, 0.12), len = R * rnd(0.68, 1);
      spokes.push({a, len, x: cx + Math.cos(a) * len, y: cy + Math.sin(a) * len});
    }
    const c = {x: cx, y: cy}, moves = [];
    for (const i of spokes.map((_, i) => i).sort(() => Math.random() - 0.5)) {
      moves.push(mood({a: c, b: spokes[i], kind: "radial"})); moves.push(mood({a: spokes[i], b: c, kind: null}));
    }
    moves.push(mood({a: c, b: spokes[0], kind: null}));
    for (let i = 0; i < n; i++) moves.push(mood({a: spokes[i], b: spokes[(i + 1) % n], kind: "frame"}));
    const gap = rnd(5.5, 10), minLen = Math.min(...spokes.map((sp) => sp.len)), pts = [];
    for (let k = 0; ; k++) {
      const r = 9 + gap * (k / n) + rnd(-0.2, 0.2) * gap;
      if (r > minLen * 0.9) break;
      const sp = spokes[k % n];
      pts.push({x: cx + Math.cos(sp.a) * r, y: cy + Math.sin(sp.a) * r});
    }
    moves.push(mood({a: spokes[0], b: pts[0], kind: null}));
    for (let j = 1; j < pts.length; j++) moves.push(mood({a: pts[j - 1], b: pts[j], kind: "spiral"}));
    moves.push(mood({a: pts[pts.length - 1], b: c, kind: null}));
    return {c, moves, segs: [], dew: [], alpha: 1, target: 1};
  };
  const speedOf = (k) => ({radial: 120, frame: 95, spiral: 85})[k] || 280;
  const turnTo = (target, dt, rate) => {
    let d = target - s.heading;
    while (d > Math.PI) d -= Math.PI * 2;
    while (d < -Math.PI) d += Math.PI * 2;
    s.heading += d * Math.min(1, dt * (rate || 12));
  };
  const fidget = (dt) => {
    s.look -= dt;
    if (s.look <= 0) { s.lookAt = s.heading + rnd(-1.4, 1.4) * JITTER; s.look = rnd(0.4, 1.6); }
    turnTo(s.lookAt, dt, 5); s.moving = false;
  };
  const step = (dt) => {
    for (const w of s.old) w.alpha += (w.target - w.alpha) * Math.min(1, dt * 1.2);
    s.old = s.old.filter((w) => w.target > 0 || w.alpha > 0.02);
    if (s.wait > 0) { s.wait -= dt; fidget(dt); return; }
    if (s.phase === "build") {
      const m = s.web.moves[s.mi], len = Math.hypot(m.b.x - m.a.x, m.b.y - m.a.y) || 0.001;
      s.p += speedOf(m.kind) * m.pace * SPEED * dt;
      if (s.p >= len) {
        if (m.kind) { s.web.segs.push(m); if (m.kind === "spiral" && Math.random() < 0.1) s.web.dew.push(m.b); }
        s.p = 0; s.mi++; s.x = m.b.x; s.y = m.b.y;
        if (s.mi >= s.web.moves.length) { s.phase = "rest"; s.timer = rnd(1.5, 4.5); s.moving = false; return; }
        s.wait = s.web.moves[s.mi].pause; return;
      }
      const f = s.p / len;
      s.x = m.a.x + (m.b.x - m.a.x) * f; s.y = m.a.y + (m.b.y - m.a.y) * f;
      turnTo(Math.atan2(m.b.y - m.a.y, m.b.x - m.a.x), dt); s.moving = true;
    } else if (s.phase === "rest") {
      s.timer -= dt; fidget(dt);
      if (s.timer <= 0) {
        s.next = makeWeb(s.w, s.h); s.route = [];
        for (let i = Math.random() < 0.5 ? 1 : 2; i > 0; i--) {
          s.route.push({x: rnd(0.08, 0.92) * s.w, y: rnd(0.08, 0.92) * s.h, pace: rnd(0.5, 1.3)});
        }
        s.route.push({x: s.next.c.x, y: s.next.c.y, pace: rnd(0.6, 1.2)});
        s.phase = "travel";
      }
    } else {
      const goal = s.route[0], dx = goal.x - s.x, dy = goal.y - s.y, d = Math.hypot(dx, dy);
      const v = 160 * goal.pace * SPEED * dt;
      if (d <= v) {
        s.x = goal.x; s.y = goal.y; s.route.shift();
        if (Math.random() < 0.4 * JITTER) s.wait = rnd(0.2, 0.9);
        if (s.route.length) return;
        for (const w of s.old) w.target *= 0.72;
        s.web.target = 0.6; s.old.push(s.web);
        let live = s.old.filter((w) => w.target > 0);
        while (live.length > KEEP) { live[0].target = 0; live = live.slice(1); }
        s.web = s.next; s.next = null; s.mi = 0; s.p = 0; s.phase = "build";
        s.wait = s.web.moves[0].pause; s.moving = false;
      } else {
        const a = Math.atan2(dy, dx) + Math.sin(s.t * (goal.freq || (goal.freq = rnd(2, 4)))) * 0.17 * JITTER;
        s.x += Math.cos(a) * v; s.y += Math.sin(a) * v; turnTo(a, dt); s.moving = true;
      }
    }
  };
  const line = (a, b, f) => {
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f); ctx.stroke();
  };
  const drawWeb = (web) => {
    if (!web || web.alpha <= 0) return;
    ctx.lineCap = "round"; ctx.strokeStyle = col.ink;
    for (const m of web.segs) {
      ctx.globalAlpha = (m.kind === "spiral" ? 0.5 : 0.3) * web.alpha;
      ctx.lineWidth = m.kind === "spiral" ? 0.7 : 0.9;
      line(m.a, m.b, 1);
    }
    if (web === s.web && s.phase === "build" && s.wait <= 0) {
      const m = web.moves[s.mi];
      if (m && m.kind) {
        ctx.globalAlpha = 0.55; ctx.lineWidth = 0.8;
        line(m.a, m.b, Math.min(1, s.p / (Math.hypot(m.b.x - m.a.x, m.b.y - m.a.y) || 1)));
      }
    }
    ctx.fillStyle = col.dew;
    for (const d of web.dew) {
      ctx.globalAlpha = 0.8 * web.alpha; ctx.beginPath(); ctx.arc(d.x, d.y, 1.6, 0, Math.PI * 2); ctx.fill();
    }
    ctx.globalAlpha = 1;
  };
  const drawSpider = () => {
    ctx.save(); ctx.translate(s.x, s.y); ctx.rotate(s.heading);
    ctx.strokeStyle = col.ink; ctx.lineWidth = 1.1; ctx.lineCap = "round"; ctx.lineJoin = "round";
    const base = [0.6, 1.1, 1.75, 2.35];
    for (const side of [1, -1]) {
      for (let i = 0; i < 4; i++) {
        const ph = (i % 2 ? Math.PI : 0) + (side > 0 ? 0 : Math.PI);
        const a = side * (base[i] + (s.moving ? Math.sin(s.t * 16 + ph) * 0.22 : 0));
        const kx = Math.cos(a) * 7, ky = Math.sin(a) * 7, fa = a + side * (i < 2 ? -0.6 : 0.6);
        ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(kx, ky);
        ctx.lineTo(kx + Math.cos(fa) * 8, ky + Math.sin(fa) * 8); ctx.stroke();
      }
    }
    ctx.fillStyle = col.ink;
    ctx.beginPath(); ctx.ellipse(-5, 0, 5.4, 4.1, 0, 0, Math.PI * 2); ctx.fill();
    ctx.beginPath(); ctx.arc(1.8, 0, 2.9, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = col.mark; ctx.beginPath(); ctx.ellipse(-5.6, 0, 1.9, 1.2, 0, 0, Math.PI * 2); ctx.fill();
    ctx.restore();
  };
  const draw = () => {
    ctx.setTransform(s.dpr, 0, 0, s.dpr, 0, 0); ctx.clearRect(0, 0, s.w, s.h);
    for (const w of s.old) drawWeb(w);
    drawWeb(s.web); drawSpider();
  };
  let frames = 0;
  const frame = (now) => {
    const dt = s.last ? Math.min(0.05, (now - s.last) / 1000) : 0;
    s.last = now; s.t += dt;
    if (++frames % 30 === 0) readColours();  // the palette can change (the done page turns dark)
    step(dt); draw(); requestAnimationFrame(frame);
  };
  const resize = () => {
    const r = el.getBoundingClientRect();
    const changed = Math.abs(r.width - s.w) > 40 || Math.abs(r.height - s.h) > 40;
    s.w = Math.max(1, r.width); s.h = Math.max(1, r.height); s.dpr = Math.min(2, devicePixelRatio || 1);
    el.width = Math.round(s.w * s.dpr); el.height = Math.round(s.h * s.dpr);
    if (!s.web || changed) {
      s.web = makeWeb(s.w, s.h); s.mi = 0; s.p = 0; s.phase = "build"; s.next = null; s.old = []; s.wait = 0;
      s.x = s.web.c.x; s.y = s.web.c.y;
      if (still) {
        for (const m of s.web.moves) if (m.kind) s.web.segs.push(m);
        s.phase = "rest";
      }
    }
    readColours(); draw();
  };
  new ResizeObserver(resize).observe(el);
  new MutationObserver(() => { readColours(); draw(); }).observe(wrap, {attributes: true, attributeFilter: ["class"]});
  resize();
  if (!still) requestAnimationFrame(frame);
})();
</script>"""

# Form errors: the browser's "Please fill out this field" bubbles are replaced by a line under the field itself,
# naming it, and the field is marked. Server errors show as a notice at the top. Runs with or without motion.
FORM_SCRIPT = """<script>
(() => {
  const ALERT = `{ALERT_SVG}`;
  function mark(field, text) {
    field.setAttribute("aria-invalid", "true");
    const box = field.closest(".fld") || field.parentNode;
    let e = box.querySelector(".ferr");
    if (!e) {
      e = document.createElement("span"); e.className = "ferr"; e.setAttribute("role", "alert");
      e.id = "err-" + (field.name || Math.random().toString(36).slice(2)); box.append(e);
      field.setAttribute("aria-describedby", e.id);
    }
    e.innerHTML = ALERT; e.append(text);
  }
  function clear(field) {
    field.removeAttribute("aria-invalid");
    const e = (field.closest(".fld") || field.parentNode).querySelector(".ferr");
    if (e) { e.remove(); field.removeAttribute("aria-describedby"); }
  }
  for (const form of document.querySelectorAll("form")) {
    form.noValidate = true;
    form.addEventListener("submit", (e) => {
      const fields = [...form.querySelectorAll("input, select, textarea")].filter((f) => f.type !== "hidden");
      fields.forEach(clear);
      const bad = fields.find((f) => !f.disabled && !f.checkValidity());
      if (!bad) return;
      e.preventDefault(); e.stopImmediatePropagation();
      const name = bad.getAttribute("aria-label") || bad.name;
      mark(bad, bad.validity.valueMissing ? `Fill in your ${name}.`
        : bad.type === "email" ? "That email doesn't look right." : `Check your ${name}.`);
      bad.focus();
    }, true);
    form.addEventListener("input", (e) => { if (e.target.matches && e.target.matches(".field")) clear(e.target); });
  }
})();
</script>"""

_ICONS = {
    "arrow": '<path d="M4 12h16"/><path d="M13 5l7 7-7 7"/>',
    "x": '<path d="M5 5l14 14"/><path d="M19 5L5 19"/>',
    "copy": '<rect x="9" y="9" width="11" height="11"/><path d="M5 15V4h11"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9"/><path d="M17 6l3 3"/>',
    "lock": '<rect x="4" y="11" width="16" height="10"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    "bell": '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/>'
    '<path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
    "signout": '<path d="M14 4h6v16h-6"/><path d="M10 8l-4 4 4 4"/><path d="M6 12h10"/>',
    "user": '<circle cx="9" cy="8" r="4"/><path d="M2 21c0-3.9 3.1-7 7-7s7 3.1 7 7"/><path d="M19 8v6"/><path d="M16 11h6"/>',
    "check": '<circle cx="12" cy="12" r="10"/><path d="M7.5 12.5l3 3 6-6.5"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "alert": '<circle cx="12" cy="12" r="10"/><path d="M12 7v6"/><path d="M12 16.5v.01"/>',
}


def icon(name: str, size: int = 16, stroke: float = 2, label: str = "") -> str:
    """An inline stroke icon in the current text colour. With `label` it is announced, else hidden."""
    a11y = f'role="img" aria-label="{html.escape(label)}"' if label else 'aria-hidden="true"'
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round" {a11y}>{_ICONS[name]}</svg>'
    )


FORM_SCRIPT = FORM_SCRIPT.replace("{ALERT_SVG}", icon("alert", 14))


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def page(
    *,
    title: str,
    spine: str = "",
    body: str,
    nav: Iterable[str] = (),
    spine_end: str = "",
    inverted: bool = False,
    script: str = "",
    status: int = 200,
) -> HTMLResponse:
    """One Margin page. `body` goes in the right-hand column; without an <h1> of its own, `title` heads it.
    `nav` links and `spine_end` (e.g. sign out) sit at the foot of the art panel. `spine` is unused (kept for
    callers from the Spine design)."""
    del spine
    foot = "".join([*nav, spine_end])
    heading = "" if "<h1" in body else f'<h1 class="title">{esc(title)}</h1>'
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title>{FONTS}<style>{CSS}{MOTION_CSS}</style></head>"
        f'<body><div class="wrap{" inv" if inverted else ""}">'
        '<aside class="art"><canvas id="web" role="img" aria-label="A spider spinning a web"></canvas>'
        f'<a class="brand" href="/">{esc(PRODUCT_NAME)}</a>'
        f'{f"<nav class=foot>{foot}</nav>" if foot else ""}</aside>'
        '<div class="hair"></div>'
        f'<main class="main">{heading}{body}</main>'
        f"</div>{script}{FORM_SCRIPT}{SPIDER_SCRIPT}{MOTION_SCRIPT}</body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=HEADERS)


def bar_link(text: str, href: str, *, target_blank: bool = False) -> str:
    extra = ' target="_blank" rel="noopener"' if target_blank else ""
    return f'<a class="bar" href="{esc(href)}"{extra}>{esc(text)}{icon("arrow")}</a>'


def bar_button(text: str, *, name: str = "", value: str = "", button_id: str = "") -> str:
    attrs = (f' name="{esc(name)}" value="{esc(value)}"' if name else "") + (f' id="{esc(button_id)}"' if button_id else "")
    return f'<button class="bar" type="submit"{attrs}>{esc(text)}{icon("arrow")}</button>'


def notice(text: str, *, error: bool = True) -> str:
    """A line the person must see: an error (brick, with an alert icon) or, with `error=False`, news."""
    if not text:
        return ""
    if error:
        return f'<p class="notice" role="alert">{icon("alert", 14)}{esc(text)}</p>'
    return f'<p class="notice info" role="status">{esc(text)}</p>'


def field(
    name: str, label: str, kind: str = "text", value: str = "", autocomplete: str = "off", *, code: bool = False
) -> str:
    """One labelled input. `code=True` is for a code someone was given (an invite code): plain text, and browsers
    and password managers are told not to fill it from saved logins."""
    shown = f' value="{esc(value)}"' if value and kind != "password" else ""
    extra = (
        ' spellcheck="false" autocapitalize="off" data-1p-ignore data-lpignore="true" data-bwignore'
        ' data-form-type="other"'
        if code
        else ""
    )
    return (
        f'<label class="fld"><span class="lbl">{esc(label[:1].upper() + label[1:])}</span>'
        f'<input class="field" name="{esc(name)}" type="{esc(kind)}" aria-label="{esc(label)}" '
        f'autocomplete="{esc(autocomplete)}"{shown}{extra} required></label>'
    )


def hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">' if value else ""


COPY_SCRIPT = """<script>
const TICK = `{CHECK_SVG}`;
for (const b of document.querySelectorAll("[data-copy]")) {
  b.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); } catch (e) { return; }
    const svg = b.querySelector("svg"); if (!svg || b.dataset.busy) return;
    b.dataset.busy = "1"; const before = svg.outerHTML;
    svg.outerHTML = TICK;
    setTimeout(() => { b.querySelector("svg").outerHTML = before; delete b.dataset.busy; }, 1200);
  });
}
</script>"""
COPY_SCRIPT = COPY_SCRIPT.replace("{CHECK_SVG}", icon("check"))


def short_name(domain: str) -> str:
    """A website's name without www. or the ending (github.com: github)."""
    parts = [p for p in domain.lower().split(".") if p and p != "www"]
    return parts[0] if len(parts) > 1 else (parts[0] if parts else domain)
