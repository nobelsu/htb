"""Unit tests for confirmation ids, signals, extraction and sessions."""

import pytest

from spider.card.model import Extract
from spider.run.confirm import Confirmer, canonical_params
from spider.run.redact import redact_headers
from spider.run.request.extract import ExtractError, ResponseView, project, run_extract
from spider.run.request.signals import content_type_ok, match_signal
from spider.run.tiers.http import HttpResponse
from spider.store.sessions import DevEnvSessionStore

# ---- confirmation ----------------------------------------------------------

C = Confirmer("s3cret")
ARGS = ("user", "shop.test", "add_to_cart", {"sku": "A", "qty": 1})


def test_canonical_params() -> None:
    assert canonical_params({"b": 1, "a": "x", "_confirmation_id": "c"}) == '{"a":"x","b":1}'


def test_confirmation_round_trip() -> None:
    conf = C.issue(*ARGS, summary="s", now=1000)
    assert conf.expires_at == "1970-01-01T00:18:40Z"
    assert C.check(conf.id, *ARGS, now=1100) is None


def test_confirmation_expired() -> None:
    conf = C.issue(*ARGS, summary="s", now=1000)
    assert "expired" in (C.check(conf.id, *ARGS, now=1121) or "")


@pytest.mark.parametrize(
    "other",
    [
        ("other-user", "shop.test", "add_to_cart", {"sku": "A", "qty": 1}),
        ("user", "evil.test", "add_to_cart", {"sku": "A", "qty": 1}),
        ("user", "shop.test", "remove", {"sku": "A", "qty": 1}),
        ("user", "shop.test", "add_to_cart", {"sku": "A", "qty": 2}),
    ],
)
def test_confirmation_bound_to_call(other: tuple) -> None:  # type: ignore[type-arg]
    conf = C.issue(*ARGS, summary="s", now=1000)
    assert "does not match" in (C.check(conf.id, *other, now=1001) or "")


def test_confirmation_other_secret_and_garbage() -> None:
    conf = C.issue(*ARGS, summary="s", now=1000)
    assert Confirmer("different").check(conf.id, *ARGS, now=1001) is not None
    assert C.check("garbage", *ARGS) == "malformed confirmation id"
    assert C.check("c_!!!", *ARGS) == "malformed confirmation id"


# ---- signals ---------------------------------------------------------------


def resp(
    status: int = 200, text: str = "", url: str = "https://s.test/x", hops: list | None = None
) -> HttpResponse:  # type: ignore[type-arg]
    return HttpResponse(status=status, headers={}, text=text, final_url=url, hops=hops or [])


def test_signals() -> None:
    assert match_signal(["status:999"], resp(999)) == "status:999"
    assert match_signal(["status:403"], resp(200)) is None
    assert match_signal(["body_contains:captcha"], resp(text="<div captcha>")) == "body_contains:captcha"
    redirected = resp(url="https://s.test/home", hops=[(302, "https://s.test/checkpoint/challenge")])
    assert match_signal(["redirect:/checkpoint"], redirected) == "redirect:/checkpoint"
    assert match_signal(["redirect:/login"], resp(url="https://s.test/login?next=/")) == "redirect:/login"
    assert match_signal(["redirect:/login"], resp(url="https://s.test/home")) is None


def test_content_types() -> None:
    assert content_type_ok("json", "application/json; charset=utf-8")
    assert content_type_ok("json", "application/vnd.linkedin.normalized+json")
    assert not content_type_ok("json", "text/html")
    assert content_type_ok("html", "text/html; charset=utf-8")
    assert content_type_ok("text", "text/plain")
    assert not content_type_ok("text", "text/html")
    assert content_type_ok("any", None)


# ---- extraction ------------------------------------------------------------


def ex(**kw: str) -> Extract:
    return Extract.model_validate(kw)


VIEW = ResponseView(
    text=(
        '<meta name="csrf" content="tok1">'
        '<script id="__NEXT_DATA__" type="x">{"props":{"pageProps":{"n":1}}}</script>'
    ),
    headers={"x-total": "42", "content-type": "text/html"},
    cookies={"flow": "abc"},
)


def test_regex_extract() -> None:
    assert run_extract(
        ex(**{"from": "body", "type": "regex", "expr": 'name="csrf" content="([^"]+)"'}), VIEW
    ) == ["tok1"]


def test_regex_parse_json_then() -> None:
    e = ex(
        **{
            "from": "body",
            "type": "regex",
            "expr": '<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            "parse": "json",
            "then": "$.props.pageProps",
        }
    )
    assert run_extract(e, VIEW) == [{"n": 1}]


def test_header_and_cookie_extract() -> None:
    assert run_extract(ex(**{"from": "headers", "type": "jsonpath", "expr": "$['x-total']"}), VIEW) == ["42"]
    assert run_extract(ex(**{"from": "headers", "type": "regex", "expr": r"x-total: (\d+)"}), VIEW) == ["42"]
    assert run_extract(ex(**{"from": "cookies", "type": "jsonpath", "expr": "$.flow"}), VIEW) == ["abc"]


def test_jsonpath_filter_and_non_json_body() -> None:
    view = ResponseView('{"e":[{"type":"company","id":1},{"type":"person","id":2}]}', {}, {})
    e = ex(**{"from": "body", "type": "jsonpath", "expr": '$.e[?(@.type=="company")].id'})
    assert run_extract(e, view) == [1]
    with pytest.raises(ExtractError):
        run_extract(e, VIEW)


def test_project() -> None:
    items = [{"t": {"x": "a"}, "u": 1}, {"u": 2}, "raw"]
    assert project(items, {"title": "$.t.x", "u": "$.u"}) == [
        {"title": "a", "u": 1},
        {"title": None, "u": 2},
        {"title": None, "u": None},
    ]
    assert project(items, {}) == items


# ---- misc ------------------------------------------------------------------


def test_redact_headers() -> None:
    out = redact_headers({"csrf-token": "x", "Authorization": "Bearer y", "Cookie": "a=b", "accept": "json"})
    assert out == {
        "csrf-token": "[redacted]",
        "Authorization": "[redacted]",
        "Cookie": "[redacted]",
        "accept": "json",
    }


def test_dev_env_sessions() -> None:
    store = DevEnvSessionStore('{"shop.test": {"shop": {"sid": "v"}}}')
    assert store.get("anyone", "shop.test", "shop") == {"sid": "v"}
    assert store.get("anyone", "shop.test", "other") is None
    assert DevEnvSessionStore("").get("u", "d", "p") is None


def test_regex_extract_resolves_streamed_rows() -> None:
    """One result spread over rows that point to each other (seen on LinkedIn's web app)."""
    body = "\n".join(
        [
            '0:["$","ul",null,{"children":["$L3","$L4"]}]',
            '1:I["chunks/card.js",["card"],"Card"]',
            '2:{"name":"Acme Labs","followers":"1,200 followers"}',
            '3:["$","$L1",null,{"info":"$2","link":"/company/acme/","price":"$$5","self":"$3"}]',
            '4:["$","$L1",null,{"info":{"name":"Brightline"},"link":"$5:props:href"}]',
            '5:["$","a",null,{"href":"/company/brightline/"}]',
        ]
    )
    view = ResponseView(body, {"content-type": "application/octet-stream"}, {})
    rows_expr = r'(?m)^[0-9a-f]+:(\["\$","\$L1".*\])$'
    e = ex(
        **{
            "from": "body",
            "type": "regex",
            "expr": rows_expr,
            "parse": "json",
            "resolve": "rows",
            "then": "$[3]",
        }
    )
    acme, bright = run_extract(e, view)
    assert acme["info"] == {"name": "Acme Labs", "followers": "1,200 followers"}  # "$2" filled in
    assert acme["price"] == "$$5"  # escaped text is left alone
    assert acme["self"][3]["self"] == "$3"  # a row is not filled in inside itself
    assert bright["link"] == "/company/brightline/"  # "$5:props:href" follows the path into row 5
    assert project([acme, bright], {"name": "$.info.name"}) == [{"name": "Acme Labs"}, {"name": "Brightline"}]
    with pytest.raises(ValueError, match="resolve"):
        ex(**{"from": "body", "type": "regex", "expr": "(x)", "resolve": "rows"})  # needs parse json
