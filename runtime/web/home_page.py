"""The home page (GET /): an agent calling a website's own actions, typed out and answered, one site
after another. The first example is rendered on the server (no script, reduced motion); the script cycles."""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from runtime.web.ui import PRODUCT_NAME, bar_link, esc, page

# (site, tool, arguments, milliseconds, result rows). Illustrative: what an agent sees, not live data.
EXAMPLES: list[tuple[str, str, str, int, list[tuple[str, str]]]] = [
    (
        "linkedin.com",
        "linkedin_search_companies",
        'query="robotics"',
        312,
        [
            ("Boston Dynamics", "Waltham, MA"),
            ("Agility Robotics", "Salem, OR"),
            ("Figure AI", "Sunnyvale, CA"),
        ],
    ),
    (
        "github.com",
        "github_search_repositories",
        'query="web crawler"',
        204,
        [("scrapy/scrapy", "Python"), ("apify/crawlee", "TypeScript"), ("gocolly/colly", "Go")],
    ),
    (
        "crates.io",
        "crates_io_search_crates",
        'query="serde"',
        96,
        [("serde", "Serialization"), ("serde_json", "JSON"), ("serde_yaml", "YAML")],
    ),
    (
        "ocado.com",
        "ocado_add_to_basket",
        'item="oat milk", quantity=2',
        441,
        [("Oat milk 1L", "2 in basket")],
    ),
]

STYLE = """<style>
.demo{margin:4px 0 28px;transition:opacity .4s}
.demo.gone{opacity:0}
.demo .head{display:flex;justify-content:space-between;gap:16px;font-size:12px;color:var(--muted);
  margin-bottom:8px}
.demo .call{font-family:'IBM Plex Mono',monospace;font-size:13px;line-height:1.6;overflow-wrap:anywhere;
  min-height:68px;padding:12px 14px;background:var(--tint)}
.demo .arg{color:var(--action)}
.demo .call.typing::after{content:"";display:inline-block;width:1px;height:1.1em;margin-left:1px;
  vertical-align:-2px;background:var(--ink);animation:blink 1s steps(1) infinite}
@keyframes blink{50%{opacity:0}}
.demo .out{list-style:none;margin:6px 0 0;padding:0;min-height:132px}
.demo .out li{display:flex;justify-content:space-between;align-items:center;gap:16px;min-height:44px;
  border-bottom:1px solid var(--line)}
.demo .out li span:last-child{color:var(--muted)}
</style>"""

SCRIPT = """<script>
(() => {
  const demo = document.getElementById("demo");
  if (!demo || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const examples = JSON.parse(document.getElementById("demo-data").textContent);
  const site = demo.querySelector(".site"), ms = demo.querySelector(".ms");
  const call = demo.querySelector(".call"), out = demo.querySelector(".out");
  const wait = (t) => new Promise((done) => setTimeout(done, t));
  const jitter = (t) => t * (0.5 + Math.random());
  const span = (text, cls) => {
    const s = document.createElement("span"); if (cls) s.className = cls; s.textContent = text; return s;
  };

  async function show([where, tool, args, took, rows]) {
    site.textContent = where; ms.textContent = ""; out.replaceChildren(); call.replaceChildren();
    demo.classList.remove("gone");
    call.classList.add("typing");
    for (const [text, cls] of [[tool, ""], ["(", ""], [args, "arg"], [")", ""]]) {
      const s = span("", cls); call.append(s);
      for (const ch of text) { s.textContent += ch; await wait(jitter(34)); }
    }
    call.classList.remove("typing");
    await wait(jitter(500));
    ms.textContent = took + " ms";
    for (const [name, detail] of rows) {
      const li = document.createElement("li"); li.className = "arrive";
      li.append(span(name), span(detail)); out.append(li);
      await wait(130);
    }
    await wait(3000);
    demo.classList.add("gone");
    await wait(450);
  }

  (async () => {
    await wait(700);
    for (let i = 0; ; i++) await show(examples[i % examples.length]);
  })();
})();
</script>"""


def _demo() -> str:
    where, tool, args, took, rows = EXAMPLES[0]
    items = "".join(f"<li><span>{esc(name)}</span><span>{esc(detail)}</span></li>" for name, detail in rows)
    data = json.dumps(EXAMPLES).replace("</", "<\\/")
    return (
        '<div class="demo" id="demo" aria-hidden="true">'
        f'<div class="head"><span class="site">{esc(where)}</span><span class="ms">{took} ms</span></div>'
        f'<div class="call"><span>{esc(tool)}(</span><span class="arg">{esc(args)}</span><span>)</span></div>'
        f'<ol class="out">{items}</ol></div>'
        f'<script type="application/json" id="demo-data">{data}</script>'
    )


def routes(signup_open: bool) -> list[BaseRoute]:
    async def home(request: Request) -> Response:
        bar = bar_link("Create account", "/signup") if signup_open else bar_link("Sign in", "/account/login")
        body = f'{STYLE}<h1 class="title">Websites your agent can call.</h1>{_demo()}{bar}'
        return page(
            title=PRODUCT_NAME, body=body, nav=['<a href="/account/login">Sign in</a>'], script=SCRIPT
        )

    return [Route("/", home, methods=["GET"])]
