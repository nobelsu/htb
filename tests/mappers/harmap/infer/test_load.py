import pytest
from synthetic import har_entry, write_har

from spider.mappers.harmap.infer.load import (
    content_kind,
    drop_reason,
    entry_from_har,
    load_candidates,
    load_har,
)

SITE = "https://www.example.com"


def _reason(raw: dict, domain: str = "example.com") -> str | None:
    return drop_reason(entry_from_har(0, raw), domain)


@pytest.mark.parametrize(
    ("mime", "kind"),
    [
        ("application/json; charset=utf-8", "json"),
        ("application/vnd.linkedin.normalized+json+2.1", "json"),
        ("text/html; charset=utf-8", "html"),
        ("text/plain", "text"),
        ("application/x-www-form-urlencoded", "form"),
        ("text/css", "static"),
        ("application/javascript", "static"),
        ("image/png", "static"),
        ("font/woff2", "static"),
        ("application/octet-stream", "other"),
        ("", "empty"),
    ],
)
def test_content_kind(mime, kind):
    assert content_kind(mime) == kind


def test_keeps_same_site_json_html_text():
    assert _reason(har_entry("GET", f"{SITE}/api/search?q=x")) is None
    assert _reason(har_entry("GET", f"{SITE}/", mime="text/html", resource_type="document")) is None
    assert _reason(har_entry("GET", "https://api.example.com/v1/x", mime="text/plain")) is None


def test_keeps_writes_of_any_method_even_without_body():
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        raw = har_entry(method, f"{SITE}/api/notes", status=204, mime="", post="a=1", post_mime="form")
        assert _reason(raw) is None, method
    form_post = har_entry(
        "POST",
        f"{SITE}/login",
        status=303,
        mime="",
        post="u=1",
        post_mime="application/x-www-form-urlencoded",
    )
    assert _reason(form_post) is None


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (har_entry("OPTIONS", f"{SITE}/api/search"), "preflight"),
        (har_entry("GET", "https://cdn.other.net/api"), "third-party host"),
        (har_entry("GET", "https://www.google-analytics.com/g/collect"), "analytics/ads host"),
        (har_entry("GET", "https://px.ads.example.com/collect"), "analytics/ads host"),
        (har_entry("POST", f"{SITE}/li/track", post="{}"), "analytics beacon"),
        (har_entry("POST", f"{SITE}/x", post="{}", resource_type="ping"), "analytics beacon"),
        (har_entry("GET", f"{SITE}/a.css", mime="text/css", resource_type="stylesheet"), "static asset"),
        (har_entry("GET", f"{SITE}/a.js", mime="application/javascript", resource_type=None), "static asset"),
        (har_entry("GET", f"{SITE}/logo.png", mime="image/png", resource_type="image"), "static asset"),
        (har_entry("GET", f"{SITE}/f.woff2", mime="", resource_type=None), "static asset"),
        (har_entry("GET", f"{SITE}/blob", mime="application/octet-stream"), "unsupported content type"),
        (har_entry("GET", f"{SITE}/api/x", status=0), "no response"),
        (har_entry("GET", "data:text/plain,hi"), "non-http"),
    ],
)
def test_drops(raw, reason):
    got = _reason(raw)
    assert got is not None and got.startswith(reason), got


def test_load_sorts_by_time_and_decodes(tmp_path):
    import base64

    b64 = har_entry("GET", f"{SITE}/api/b", at=2, body={"ok": 1})
    b64["response"]["content"]["text"] = base64.b64encode(b'{"ok": 1}').decode()
    b64["response"]["content"]["encoding"] = "base64"
    path = write_har(
        tmp_path / "t.har",
        [b64, har_entry("GET", f"{SITE}/api/a", at=1, body={"a": 1}, cookies={"JSESSIONID": '"ajax:1"'})],
    )
    entries = load_har(path)
    assert [e.path for e in entries] == ["/api/a", "/api/b"]
    assert entries[1].response_json == {"ok": 1}
    assert entries[0].request_cookies == [("JSESSIONID", '"ajax:1"')]
    assert entries[0].header("COOKIE") == 'JSESSIONID="ajax:1"'


def test_set_cookies_parsed():
    raw = har_entry(
        "POST",
        f"{SITE}/login",
        response_headers=[("Set-Cookie", "sid=abc; Path=/; HttpOnly"), ("set-cookie", 'J="ajax:1"; Path=/')],
    )
    assert entry_from_har(0, raw).set_cookies == [("sid", "abc"), ("J", '"ajax:1"')]


def test_load_candidates_reports_drops(tmp_path):
    path = write_har(
        tmp_path / "t.har",
        [
            har_entry("GET", f"{SITE}/search", mime="text/html", at=0, resource_type="document"),
            har_entry("GET", f"{SITE}/app.css", mime="text/css", at=1, resource_type="stylesheet"),
            har_entry("OPTIONS", f"{SITE}/api/search", at=2),
            har_entry("GET", f"{SITE}/api/search?q=x", at=3),
            har_entry("GET", "https://www.googletagmanager.com/gtm.js", mime="application/javascript", at=4),
        ],
    )
    result = load_candidates(path, "example.com")
    assert [e.path for e in result.kept] == ["/search", "/api/search"]
    assert [r for _, r in result.dropped] == ["static asset (stylesheet)", "preflight", "analytics/ads host"]
