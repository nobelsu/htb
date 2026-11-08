from typing import Any

import pytest

from runtime.template import (
    Context,
    MissingCookie,
    MissingParam,
    StepValueError,
    TemplateError,
    build_request,
    encode_query,
    merge_headers,
    resolve,
    resolve_body,
)
from schema.card import Request


def ctx(**params: Any) -> Context:
    return Context(
        base_url="https://site.test",
        params=params,
        cookies={"JSESSIONID": '"ajax:123"', "sid": "abc"},
        steps={("company", "id"): ["urn:li:1"], ("search", "items"): [1, 2], ("x", "none"): []},
    )


# ---- filters ---------------------------------------------------------------


def test_escape_restli_palantir() -> None:
    assert resolve("{{params.q | escape_restli}}", ctx(q="Palantir, Inc.")) == "Palantir%2C%20Inc."


def test_escape_restli_all_specials() -> None:
    assert resolve("{{params.q | escape_restli}}", ctx(q="a%(b):c,'d e")) == "a%25%28b%29%3Ac%2C%27d%20e"


def test_escape_restli_leaves_other_chars() -> None:
    assert resolve("{{params.q | escape_restli}}", ctx(q="a&b/ü")) == "a&b/ü"


def test_offset_page_three() -> None:
    assert resolve("{{params.page | offset:10}}", ctx(page=3)) == 20


def test_offset_page_one() -> None:
    assert resolve("{{params.page | offset:10}}", ctx(page=1)) == 0


def test_offset_from_string() -> None:
    assert resolve("{{params.page | offset:25}}", ctx(page="2")) == 25


def test_urlencode() -> None:
    assert (
        resolve("{{params.id | urlencode}}", ctx(id="urn:li:fsd company/1?x"))
        == "urn%3Ali%3Afsd%20company%2F1%3Fx"
    )


def test_strip_quotes() -> None:
    assert resolve("{{session.cookies.JSESSIONID | strip_quotes}}", ctx()) == "ajax:123"


def test_strip_quotes_without_quotes() -> None:
    assert resolve("{{session.cookies.sid | strip_quotes}}", ctx()) == "abc"


def test_strip_quotes_only_one_pair() -> None:
    assert resolve("{{params.v | strip_quotes}}", ctx(v='""x""')) == '"x"'


def test_int_filter() -> None:
    assert resolve("{{params.v | int}}", ctx(v=" 42 ")) == 42
    assert resolve("{{params.v | int}}", ctx(v=7.0)) == 7


def test_int_filter_bad_input() -> None:
    with pytest.raises(TemplateError, match="cannot convert 'abc' to int"):
        resolve("{{params.v | int}}", ctx(v="abc"))


def test_filter_chain() -> None:
    assert resolve("{{params.v | strip_quotes | int}}", ctx(v='"12"')) == 12


# ---- resolution ------------------------------------------------------------


def test_text_substitution() -> None:
    assert (
        resolve("{{base_url}}/c/{{params.id}}/p{{params.n}}", ctx(id="a", n=2)) == "https://site.test/c/a/p2"
    )


def test_whole_placeholder_keeps_type() -> None:
    assert resolve("{{params.n}}", ctx(n=5)) == 5
    assert resolve("{{params.b}}", ctx(b=False)) is False


def test_bool_in_text() -> None:
    assert resolve("flag={{params.b}}", ctx(b=True)) == "flag=true"


def test_step_value_single() -> None:
    assert resolve("{{steps.company.id}}", ctx()) == "urn:li:1"


def test_step_value_many_and_zero() -> None:
    with pytest.raises(StepValueError) as many:
        resolve("{{steps.search.items}}", ctx())
    assert many.value.count == 2
    with pytest.raises(StepValueError) as zero:
        resolve("{{steps.x.none}}", ctx())
    assert zero.value.count == 0


def test_missing_cookie() -> None:
    with pytest.raises(MissingCookie, match="li_at"):
        resolve("{{session.cookies.li_at}}", ctx())


def test_missing_param() -> None:
    with pytest.raises(MissingParam):
        resolve("{{params.nope}}", ctx())


# ---- typed bodies ----------------------------------------------------------


def test_typed_body_values() -> None:
    body = {
        "urn": "{{params.id}}",
        "count": "{{params.n}}",
        "label": "n={{params.n}}",
        "following": True,
        "limit": 10,
        "nested": {"page": "{{params.page | int}}", "tags": ["{{params.id}}", 3]},
    }
    out = resolve_body(body, ctx(id="u1", n=4, page="2"))
    assert out == {
        "urn": "u1",
        "count": 4,
        "label": "n=4",
        "following": True,
        "limit": 10,
        "nested": {"page": 2, "tags": ["u1", 3]},
    }


def test_optional_params_dropped_from_body() -> None:
    body = {"q": "{{params.q}}", "sort": "{{params.sort}}", "meta": {"note": "x {{params.note}}"}, "keep": 1}
    assert resolve_body(body, ctx(q="a")) == {"q": "a", "meta": {}, "keep": 1}


# ---- query encoding --------------------------------------------------------


def test_query_values_percent_encoded() -> None:
    q = {"keywords": "{{params.q}}", "page": "{{params.page}}", "literal": "a b&c=d"}
    assert encode_query(q, ctx(q="Palantir, Inc.", page=2)) == (
        "keywords=Palantir%2C%20Inc.&page=2&literal=a%20b%26c%3Dd"
    )


def test_query_restli_exception() -> None:
    q = {"variables": "(keywords:{{params.q | escape_restli}},start:{{params.page | offset:10}})"}
    assert encode_query(q, ctx(q="Palantir, Inc.", page=3)) == (
        "variables=(keywords:Palantir%2C%20Inc.,start:20)"
    )


def test_query_restli_still_encodes_ampersand() -> None:
    q = {"variables": "(keywords:{{params.q | escape_restli}})"}
    assert encode_query(q, ctx(q="R&D")) == "variables=(keywords:R%26D)"


def test_query_drops_optional() -> None:
    assert encode_query({"q": "{{params.q}}", "sort": "{{params.sort}}"}, ctx(q="x")) == "q=x"


def test_query_non_string_literal() -> None:
    assert encode_query({"count": 10, "flag": True}, ctx()) == "count=10&flag=true"


# ---- requests --------------------------------------------------------------


def test_build_request() -> None:
    req = Request(
        method="post",
        url="{{base_url}}/follow/{{params.id | urlencode}}",
        query={"q": "{{params.q}}", "opt": "{{params.opt}}"},
        headers={},
        body={"urn": "{{params.id}}", "following": True},
    )
    headers = merge_headers(
        {"Accept": "application/json", "csrf-token": "{{session.cookies.JSESSIONID | strip_quotes}}"},
        {"accept": "text/html"},
        {"X-Opt": "{{params.opt}}"},
    )
    built = build_request(req, headers, ctx(id="urn:li:1", q="a b"))
    assert built.method == "POST"
    assert built.url == "https://site.test/follow/urn%3Ali%3A1?q=a%20b"
    assert built.headers == {"accept": "text/html", "csrf-token": "ajax:123"}
    assert built.json_body == {"urn": "urn:li:1", "following": True}


def test_url_with_existing_query_and_missing_optional() -> None:
    req = Request(method="GET", url="{{base_url}}/s?x=1&tag={{params.tag}}", query={"q": "z"})
    assert build_request(req, {}, ctx()).url == "https://site.test/s?x=1&tag=&q=z"


def test_no_body() -> None:
    built = build_request(Request(method="GET", url="{{base_url}}/"), {}, ctx())
    assert not built.has_body and built.json_body is None
