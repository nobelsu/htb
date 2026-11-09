import json
import re
from urllib.parse import quote

from fixture_site import BUILD_ID, PAGE_SIZE


def _search(client, q: str, start: int = 0, token: str | None = None, csrf: str | None = None):
    if token is None:
        token = client.get("/api/session").json()["token"]
    if csrf is None:
        csrf = (client.cookie("JSESSIONID") or "").strip('"')
    return client.get(
        f"/api/search?q={quote(q)}&start={start}", headers={"x-token": token, "csrf-token": csrf}
    )


def test_home_is_public(client):
    r = client.get("/")
    assert r.status == 200
    assert "Acme Directory" in r.body


def test_api_needs_login_cookie(client):
    assert client.get("/api/session").status == 401
    assert client.get("/api/search?q=x&start=0").status == 401


def test_pages_redirect_to_login(client):
    r = client.get("/search")
    assert r.status == 200  # urllib followed the 303
    assert 'action="/login"' in r.body


def test_login_sets_session_and_quoted_jsessionid(client):
    client.login()
    assert re.fullmatch(r"[0-9a-f]{32}", client.cookie("sid") or "")
    assert re.fullmatch(r'"ajax:\d{18}"', client.cookie("JSESSIONID") or "")


def test_session_token_issued_fresh_each_time(client):
    client.login()
    a = client.get("/api/session").json()["token"]
    b = client.get("/api/session").json()["token"]
    assert a != b and len(a) >= 16


def test_search_paginates_with_total(client):
    client.login()
    page1 = _search(client, "software", 0).json()
    page2 = _search(client, "software", PAGE_SIZE).json()
    page3 = _search(client, "software", 2 * PAGE_SIZE).json()
    assert page1["total"] == page2["total"] == 23
    assert [p["count"] for p in (page1, page2, page3)] == [10, 10, 3]
    ids = [e["id"] for p in (page1, page2, page3) for e in p["elements"]]
    assert len(set(ids)) == 23
    item = page1["elements"][0]
    assert set(item) >= {"id", "name", "industry", "url", "followers", "logo"}


def test_search_marker_returns_empty_list(client):
    client.login()
    r = _search(client, "zqxv8841").json()
    assert r["total"] == 0 and r["elements"] == [] and r["query"] == "zqxv8841"


def test_search_requires_token_and_csrf_header(client):
    client.login()
    assert _search(client, "software", token="nope").status == 400
    assert _search(client, "software", csrf="ajax:0").status == 400
    # the header must be the cookie value *without* quotes
    assert _search(client, "software", csrf=client.cookie("JSESSIONID")).status == 400


def test_search_page_embeds_next_data(client):
    client.login()
    body = client.get("/search").body
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', body)
    assert m, "no __NEXT_DATA__ blob"
    blob = json.loads(m.group(1))
    assert blob["buildId"] == BUILD_ID
    assert blob["props"]["pageProps"]["page"] == "search"


def test_compose_csrf_meta_and_note_post(client):
    client.login()
    page = client.get("/compose").body
    # the spec's own example extractor must work on this page
    csrf = re.findall(r'name="csrf" content="([^"]+)"', page)
    assert len(csrf) == 1
    r = client.post_form("/api/notes", {"csrf": csrf[0], "text": "hello zqxv8841"})
    assert r.status == 201
    note = r.json()["note"]
    assert note["text"] == "hello zqxv8841"
    assert client.get("/api/notes").json()["elements"] == [note]


def test_note_post_rejects_bad_csrf(client):
    client.login()
    r = client.post_form("/api/notes", {"csrf": "forged", "text": "hi"})
    assert r.status == 400 and r.json()["ok"] is False


def test_login_state_is_usable(site, client):
    state = site.login_state()
    cookies = "; ".join(f"{c['name']}={c['value']}" for c in state["cookies"])
    assert client.get("/api/session", headers={"Cookie": cookies}).status == 200


def test_static_assets_served(client):
    assert client.get("/static/search.js").headers["Content-Type"] == "application/javascript"
    assert client.get("/static/app.css").status == 200
