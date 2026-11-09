"""A compact, text-only view of a page for the explorer's LLM: headings, links, fields and buttons, each
with the CSS selector the capture driver can use. Never includes field values (no typed text, no hidden
inputs, no tokens), only what the page offers.
"""

from __future__ import annotations

SNAPSHOT_JS = r"""
() => {
  const sel = window.__mapperSelectorFor || ((el) => el.tagName.toLowerCase());
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  };
  const clean = (t, n) => (t || "").trim().replace(/\s+/g, " ").slice(0, n);
  const labelOf = (el) => {
    if (el.getAttribute("aria-label")) return el.getAttribute("aria-label");
    if (el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) return l.innerText;
    }
    const wrap = el.closest("label");
    return wrap ? wrap.innerText : "";
  };
  const out = { url: location.href, title: document.title, headings: [], links: [], fields: [], buttons: [] };
  for (const h of document.querySelectorAll("h1,h2,h3")) {
    if (visible(h) && out.headings.length < 12) out.headings.push(clean(h.innerText, 80));
  }
  const hrefs = new Set();
  for (const a of document.querySelectorAll("a[href]")) {
    const href = a.getAttribute("href");
    if (!visible(a) || hrefs.has(href) || href.startsWith("javascript:") || out.links.length >= 50) continue;
    hrefs.add(href);
    const text = clean(a.innerText || a.getAttribute("aria-label"), 50);
    out.links.push({ text: text, href: href, selector: sel(a) });
  }
  for (const f of document.querySelectorAll("input,textarea,select,[contenteditable=true]")) {
    const type = (f.getAttribute("type") || f.tagName).toLowerCase();
    const skip = ["hidden", "submit", "button", "image", "reset"].includes(type);
    if (skip || !visible(f) || out.fields.length >= 40) continue;
    out.fields.push({
      selector: sel(f),
      type: type,
      name: f.getAttribute("name") || "",
      placeholder: clean(f.getAttribute("placeholder"), 60),
      label: clean(labelOf(f), 60),
    });
  }
  for (const b of document.querySelectorAll("button,[role=button],input[type=submit],input[type=button]")) {
    if (!visible(b) || out.buttons.length >= 40) continue;
    const label = clean(b.innerText || b.value || b.getAttribute("aria-label"), 50);
    out.buttons.push({ selector: sel(b), text: label });
  }
  return out;
}
"""


def page_snapshot(page) -> dict:
    return page.evaluate(SNAPSHOT_JS)


def as_text(snap: dict) -> str:
    """Render a snapshot for a prompt."""
    lines = [f"URL: {snap['url']}", f"Title: {snap['title']}"]
    if snap["headings"]:
        lines.append("Headings: " + " | ".join(snap["headings"]))
    if snap["fields"]:
        lines.append("Fields:")
        for f in snap["fields"]:
            desc = ", ".join(
                x for x in (f["label"], f["placeholder"] and f"placeholder {f['placeholder']!r}") if x
            )
            lines.append(f"  - {f['selector']}  ({f['type']}{', ' + desc if desc else ''})")
    if snap["buttons"]:
        lines.append("Buttons:")
        lines += [f"  - {b['selector']}  {b['text']!r}" for b in snap["buttons"]]
    if snap["links"]:
        lines.append("Links:")
        lines += [f"  - {a['text']!r} -> {a['href']}  ({a['selector']})" for a in snap["links"]]
    return "\n".join(lines)
