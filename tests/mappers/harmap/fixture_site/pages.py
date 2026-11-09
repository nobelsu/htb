"""HTML pages and JS for the fixture site. Plain strings: no template engine."""

from __future__ import annotations

import html
import json

STYLE = "body{font-family:sans-serif;max-width:40rem;margin:2rem auto}li{margin:.3rem 0}"

NAV = (
    '<nav><a href="/">Home</a> · <a href="/search">Search</a> · <a href="/compose">New note</a>'
    ' · <a href="/logout">Log out</a></nav>'
)


def _page(title: str, body: str, head: str = "") -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)} · Acme Directory</title>"
        f'<link rel="stylesheet" href="/static/app.css">{head}</head>'
        f"<body>{NAV}<h1>{html.escape(title)}</h1>{body}</body></html>"
    )


def home() -> str:
    return _page("Acme Directory", "<p>Find companies and keep notes.</p>")


def login(next_path: str = "/") -> str:
    return _page(
        "Log in",
        '<form method="post" action="/login">'
        f'<input type="hidden" name="next" value="{html.escape(next_path)}">'
        '<label>User <input name="username" value="demo"></label> '
        '<label>Password <input name="password" type="password" value="demo"></label> '
        '<button type="submit">Log in</button></form>',
    )


def search(next_data: dict, endpoint: str = "/api/search") -> str:
    blob = json.dumps(next_data).replace("</", "<\\/")
    body = (
        f'<form id="search-form" data-endpoint="{html.escape(endpoint)}">'
        '<input name="q" id="q" autocomplete="off" placeholder="Search companies">'
        '<button type="submit">Search</button></form>'
        '<p id="summary"></p><ul id="results"></ul>'
        '<button id="next" type="button" hidden>Next page</button>'
        f'<script id="__NEXT_DATA__" type="application/json">{blob}</script>'
        '<script src="/static/search.js"></script>'
    )
    return _page("Search", body)


def compose(csrf: str) -> str:
    head = f'<meta name="csrf" content="{html.escape(csrf)}">'
    body = (
        '<form id="note-form">'
        '<textarea name="text" id="text" rows="3" placeholder="Write a note"></textarea>'
        '<button type="submit">Save note</button></form>'
        '<p id="status"></p>'
        '<script src="/static/compose.js"></script>'
    )
    return _page("New note", body, head=head)


def stream_search() -> str:
    """A search whose answer is streamed data rows (React Server Components style), like LinkedIn's."""
    return _page(
        "Stream search",
        '<input id="sq" placeholder="Find companies"> <button id="sgo" type="button">Find</button>'
        '<p id="sstatus"></p><ul id="sresults"></ul><script src="/static/stream.js"></script>',
    )


def me() -> str:
    """The signed-in member's own page: filled in from GET /api/me (a read that takes no input)."""
    return _page("Me", '<h2 id="name"></h2><p id="headline"></p><script src="/static/me.js"></script>')


def company() -> str:
    """A company page; the slug in the address is the input (GET /api/companies/<slug>)."""
    return _page(
        "Company", '<h2 id="cname"></h2><p id="cinfo"></p><script src="/static/company.js"></script>'
    )


def messages() -> str:
    """Threads to pick from (GET /api/conversations); picking one loads its messages; Send posts one."""
    return _page(
        "Messages",
        '<ul id="threads"></ul><h2 id="who"></h2><ul id="msgs"></ul>'
        '<textarea id="msg" aria-label="Write a message"></textarea> '
        '<button id="send" type="button">Send</button>'
        '<script src="/static/messages.js"></script>',
    )


APP_CSS = "body{color:#222}"

SEARCH_JS = r"""
(function () {
  const PAGE_SIZE = 10;
  const state = { q: "", start: 0 };

  function cookie(name) {
    for (const part of document.cookie.split(";")) {
      const [k, ...rest] = part.trim().split("=");
      if (k === name) return rest.join("=");
    }
    return "";
  }

  async function sessionToken() {
    const r = await fetch("/api/session", { credentials: "same-origin" });
    return (await r.json()).token;
  }

  async function run() {
    const token = await sessionToken();
    const endpoint = document.getElementById("search-form").dataset.endpoint;
    const url = endpoint + "?q=" + encodeURIComponent(state.q) + "&start=" + state.start;
    const r = await fetch(url, {
      credentials: "same-origin",
      headers: {
        "x-token": token,
        "csrf-token": cookie("JSESSIONID").replace(/"/g, ""),
        "accept": "application/json",
      },
    });
    const data = await r.json();
    const list = document.getElementById("results");
    list.innerHTML = "";
    for (const c of data.elements) {
      const li = document.createElement("li");
      li.textContent = c.name + " (" + c.industry + ")";
      list.appendChild(li);
    }
    document.getElementById("summary").textContent = data.total + " results";
    document.getElementById("next").hidden = state.start + PAGE_SIZE >= data.total;
  }

  document.getElementById("search-form").addEventListener("submit", function (e) {
    e.preventDefault();
    state.q = document.getElementById("q").value;
    state.start = 0;
    run();
  });
  document.getElementById("next").addEventListener("click", function () {
    state.start += PAGE_SIZE;
    run();
  });
})();
"""

COMPOSE_JS = r"""
(function () {
  document.getElementById("note-form").addEventListener("submit", async function (e) {
    e.preventDefault();
    const csrf = document.querySelector('meta[name="csrf"]').content;
    const body = new URLSearchParams({ csrf: csrf, text: document.getElementById("text").value });
    const r = await fetch("/api/notes", { method: "POST", credentials: "same-origin", body: body });
    const data = await r.json();
    document.getElementById("status").textContent = data.ok ? "Saved " + data.note.id : "Failed";
  });
})();
"""

STREAM_JS = r"""
(function () {
  const post = (url, body) =>
    fetch(url, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
    });
  const rows = (text) => {
    const out = {};
    for (const line of text.split("\n")) {
      const m = /^([0-9a-f]+):(\[.*\]|\{.*\})$/.exec(line);
      if (m) out[m[1]] = JSON.parse(m[2]);
    }
    return out;
  };
  document.getElementById("sgo").addEventListener("click", async function () {
    const q = document.getElementById("sq").value;
    // fires first and its reply is read (as on LinkedIn), but it is the same every time: a decoy
    post("/api/stream/log", { q: q, event: "search" }).then((r) => r.text());
    const text = await (await post("/api/stream/search", { q: q })).text();
    const r = rows(text);
    const list = document.getElementById("sresults");
    list.innerHTML = "";
    for (const id of Object.keys(r)) {
      const row = r[id];
      if (Array.isArray(row) && row[1] === "$L1") {
        const info = r[row[3].info.slice(1)] || {};
        const li = document.createElement("li");
        li.textContent = info.name + " (" + row[3].followers + ")";
        list.appendChild(li);
      }
    }
    document.getElementById("sstatus").textContent = list.children.length + " results";
  });
})();
"""

ME_JS = r"""
fetch("/api/me").then((r) => r.json()).then((d) => {
  document.getElementById("name").textContent = d.member.first_name + " " + d.member.last_name;
  document.getElementById("headline").textContent = d.member.headline;
});
"""

COMPANY_JS = r"""
const slug = location.pathname.split("/")[2];
fetch("/api/companies/" + encodeURIComponent(slug)).then((r) => r.json()).then((d) => {
  const c = d.company || { name: "No such company", industry: "", followers: 0 };
  document.getElementById("cname").textContent = c.name;
  document.getElementById("cinfo").textContent = c.industry + " · " + c.followers + " followers";
});
"""

MESSAGES_JS = r"""
let current = null;
const path = (urn) => "/api/conversations/" + encodeURIComponent(urn) + "/messages";
fetch("/api/conversations").then((r) => r.json()).then((d) => {
  const list = document.getElementById("threads");
  for (const c of d.elements) {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = c.title;
    b.addEventListener("click", () => {
      current = c.conversation_urn;
      document.getElementById("who").textContent = c.title;
      fetch(path(current)).then((r) => r.json()).then((m) => {
        const msgs = document.getElementById("msgs");
        msgs.innerHTML = "";
        for (const x of m.elements) {
          const item = document.createElement("li");
          item.textContent = x.sender + ": " + x.text;
          msgs.appendChild(item);
        }
      });
    });
    li.appendChild(b);
    list.appendChild(li);
  }
});
// Like LinkedIn's messenger: a failed send stays queued and is retried until it gets through, and
// typing saves a draft (which carries the text) as you go.
const send = (urn, text, tries) => fetch(path(urn), {
  method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: text }),
}).catch(() => { if (tries > 0) setTimeout(() => send(urn, text, tries - 1), 100); });
document.getElementById("msg").addEventListener("input", (e) => {
  fetch("/api/drafts", { method: "POST", body: JSON.stringify({ text: e.target.value }) }).catch(() => {});
});
document.getElementById("send").addEventListener("click", () => {
  send(current, document.getElementById("msg").value, 100);
});
"""

CONVERSATIONS = [
    {"conversation_urn": "urn:fx:conv:1", "title": "Ada Lovelace", "unread": 1},
    {"conversation_urn": "urn:fx:conv:2", "title": "Grace Hopper", "unread": 0},
    {"conversation_urn": "urn:fx:conv:3", "title": "Alan Turing", "unread": 2},
    {"conversation_urn": "urn:fx:conv:4", "title": "Katherine Johnson", "unread": 0},
]
