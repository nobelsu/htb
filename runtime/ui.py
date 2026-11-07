"""The Spine design every page shares (DECISIONS.md L12): a full-height black spine on the left with the page's
name rotated and huge, giant stacked lines of type on the right, and the page's main action as a full-width black
bar at the bottom. Black and white, typography first, icons instead of symbols, no subtitles.
"""

from __future__ import annotations

import html
import os
from collections.abc import Iterable

from starlette.responses import HTMLResponse

PRODUCT_NAME = os.environ.get("PRODUCT_NAME", "htb")
HEADERS = {"cache-control": "no-store", "referrer-policy": "no-referrer", "x-frame-options": "DENY"}

FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;700;800'
    '&family=IBM+Plex+Mono:wght@400;500&display=swap">'
)

CSS = """
*{box-sizing:border-box}
html,body{margin:0;background:#fff;color:#000}
body{font-family:'Inter Tight',Arial,sans-serif}
a{color:inherit}
.wrap{display:flex;min-height:100vh}
.spine{flex:0 0 200px;background:#000;color:#fff;display:flex;flex-direction:column;justify-content:space-between;
  align-items:center;padding:28px 0;overflow:hidden}
.brand{font-weight:800;font-size:15px;color:inherit;text-decoration:none;letter-spacing:-.01em}
.word{margin:0;writing-mode:vertical-rl;transform:rotate(180deg);font-weight:800;letter-spacing:-.06em;line-height:.8;
  white-space:nowrap}
.spine-end{width:44px;height:44px;display:flex;align-items:center;justify-content:center}
.main{flex:1;min-width:0;display:flex;flex-direction:column}
.nav{display:flex;justify-content:flex-end;gap:32px;padding:28px 48px;font-size:20px;flex-wrap:wrap}
.nav a,.nav button{text-underline-offset:4px;font:inherit;background:none;border:0;padding:0;cursor:pointer;
  text-decoration:underline;color:inherit}
.headline{margin:0;padding:24px 48px 40px;font-size:clamp(56px,9vw,120px);line-height:.9;font-weight:800;
  letter-spacing:-.055em;overflow-wrap:anywhere}
.rule{border-top:3px solid #000}
.row{display:flex;justify-content:space-between;align-items:center;gap:24px;padding:26px 48px;
  border-bottom:1px solid #000}
.row.strong{border-bottom-width:3px}
.big{font-size:clamp(36px,5.6vw,72px);line-height:.95;font-weight:800;letter-spacing:-.05em;min-width:0;
  overflow-wrap:anywhere;display:flex;align-items:center;gap:20px}
.mid{font-size:clamp(28px,4vw,52px);line-height:1;font-weight:800;letter-spacing:-.045em}
.struck{text-decoration:line-through;text-decoration-thickness:6px}
.mono{font-family:'IBM Plex Mono',monospace;font-size:18px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  min-width:0}
.pill{background:#000;color:#fff;padding:16px 26px;font-size:20px;font-weight:700;display:inline-flex;
  align-items:center;gap:12px;text-decoration:none;flex:none;border:0;cursor:pointer;font-family:inherit}
form.stack{flex:1;display:flex;flex-direction:column;margin:0}
.field{font:inherit;font-size:clamp(40px,6.6vw,88px);font-weight:800;letter-spacing:-.05em;border:0;
  border-bottom:3px solid #000;padding:28px 48px 18px;outline:none;background:transparent;color:#000;width:100%;
  border-radius:0}
.field::placeholder{color:#a3a3a3}
.field:focus{background:#f2f2f2}
.bar{margin-top:auto;font:inherit;font-size:clamp(24px,3.2vw,40px);font-weight:800;letter-spacing:-.04em;
  background:#000;color:#fff;border:0;padding:36px 48px;display:flex;justify-content:space-between;
  align-items:center;text-decoration:none;cursor:pointer;width:100%;text-align:left}
.bar:hover{background:#222}
.sq{width:52px;height:52px;border:2px solid #000;background:#fff;display:inline-flex;align-items:center;
  justify-content:center;cursor:pointer;flex:none;padding:0;color:#000}
.sq:hover{background:#000;color:#fff}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));border-top:3px solid #000;
  margin-top:auto}
.tile{font:inherit;background:#fff;color:#000;border:0;border-right:1px solid #000;padding:28px 24px;cursor:pointer;
  display:flex;flex-direction:column;align-items:flex-start;gap:18px;text-align:left;text-decoration:none;
  font-size:26px;font-weight:700;letter-spacing:-.03em}
.tile:last-child{border-right:0}
.tile.on{background:#000;color:#fff}
.tile:hover{background:#000;color:#fff}
.notice{padding:22px 48px;background:#000;color:#fff;font-size:clamp(20px,2.4vw,28px);font-weight:700;
  letter-spacing:-.02em}
.center{flex:1;display:flex;flex-direction:column;justify-content:center;padding:48px}
.giant{margin:40px 0 0;font-size:clamp(64px,11vw,150px);line-height:.88;font-weight:800;letter-spacing:-.06em}
.inv{background:#000;color:#fff}
.inv .spine{background:#fff;color:#000}
[hidden]{display:none!important}
.state{flex:1;display:flex;flex-direction:column}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.empty{padding:26px 48px;font-size:clamp(28px,4vw,52px);font-weight:800;letter-spacing:-.045em;color:#a3a3a3}
:focus-visible{outline:3px solid #000;outline-offset:3px}
.inv :focus-visible,.spine :focus-visible,.bar:focus-visible,.notice :focus-visible{outline-color:#fff}
@media (max-width:760px){
  .wrap{flex-direction:column}
  .spine{flex:none;flex-direction:row;padding:16px 20px;align-items:center}
  .word{writing-mode:horizontal-tb;transform:none;font-size:44px!important;line-height:1}
  .nav,.row,.field,.bar,.headline,.notice,.empty{padding-left:20px;padding-right:20px}
  .center{padding:20px}
  .row{flex-wrap:wrap}
  .big{font-size:40px;overflow-wrap:break-word}
  .tile{border-right:0;border-bottom:1px solid #000}
}
"""

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


def icon(name: str, size: int = 24, stroke: float = 2, label: str = "") -> str:
    """An inline stroke icon in the current text colour. With `label` it is announced, else hidden."""
    a11y = f'role="img" aria-label="{html.escape(label)}"' if label else 'aria-hidden="true"'
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="{stroke}" stroke-linecap="square" {a11y}>{_ICONS[name]}</svg>'
    )


def spine_size(word: str) -> int:
    """The spine word's size: as big as fits the page height."""
    n = len(word)
    return 200 if n <= 6 else 180 if n <= 7 else 140 if n <= 9 else 110 if n <= 12 else 84


def spine_font(word: str) -> str:
    """CSS font size: as big as `spine_size`, but never taller than the window leaves room for (the rotated word's
    length is about 0.6 em per letter; 150 px stays free for the product name and the bottom button)."""
    per_em = max(len(word), 1) * 0.6
    return f"min({spine_size(word)}px, calc((100vh - 150px) / {per_em:.2f}))"


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def page(
    *,
    title: str,
    spine: str,
    body: str,
    nav: Iterable[str] = (),
    spine_end: str = "",
    inverted: bool = False,
    script: str = "",
    status: int = 200,
) -> HTMLResponse:
    """One Spine page. `body` goes in the main column (put the bar last); `nav` items are ready-made links."""
    nav_html = f'<nav class="nav">{"".join(nav)}</nav>' if nav else ""
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title>{FONTS}<style>{CSS}</style></head>"
        f'<body><div class="wrap{" inv" if inverted else ""}">'
        f'<aside class="spine"><a class="brand" href="/">{esc(PRODUCT_NAME)}</a>'
        f'<p class="word" style="font-size:{spine_font(spine)}">{esc(spine)}</p>'
        f'<div class="spine-end">{spine_end}</div></aside>'
        f'<main class="main">{"" if "<h1" in body else f"<h1 class=sr>{esc(title)}</h1>"}{nav_html}{body}</main>'
        f"</div>{script}</body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=HEADERS)


def bar_link(text: str, href: str, *, target_blank: bool = False) -> str:
    extra = ' target="_blank" rel="noopener"' if target_blank else ""
    return f'<a class="bar" href="{esc(href)}"{extra}>{esc(text)}{icon("arrow", 44)}</a>'


def bar_button(text: str, *, name: str = "", value: str = "", button_id: str = "") -> str:
    attrs = (f' name="{esc(name)}" value="{esc(value)}"' if name else "") + (f' id="{esc(button_id)}"' if button_id else "")
    return f'<button class="bar" type="submit"{attrs}>{esc(text)}{icon("arrow", 44)}</button>'


def notice(text: str) -> str:
    """A message the person must see (an error): one black line in display type."""
    return f'<p class="notice" role="alert">{esc(text)}</p>' if text else ""


def field(name: str, placeholder: str, kind: str = "text", value: str = "", autocomplete: str = "off") -> str:
    shown = f' value="{esc(value)}"' if value and kind != "password" else ""
    return (
        f'<input class="field" name="{esc(name)}" type="{esc(kind)}" placeholder="{esc(placeholder)}" '
        f'aria-label="{esc(placeholder)}" autocomplete="{esc(autocomplete)}"{shown} required>'
    )


def hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">' if value else ""


COPY_SCRIPT = """<script>
for (const b of document.querySelectorAll("[data-copy]")) {
  b.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); } catch (e) { return; }
    b.classList.add("on"); setTimeout(() => b.classList.remove("on"), 1200);
  });
}
</script>"""


def short_name(domain: str) -> str:
    """The spine word for a website: its name without www. or the ending (github.com: github)."""
    parts = [p for p in domain.lower().split(".") if p and p != "www"]
    return parts[0] if len(parts) > 1 else (parts[0] if parts else domain)
