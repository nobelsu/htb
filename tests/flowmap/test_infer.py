"""flowmap.infer on synthetic captures: no browser. One end-to-end test on the bookshop needs Chromium."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlencode

import pytest

from flowmap.capture import CookieRecord, Exchange, Run, secret_hash
from flowmap.flows import Click, Fill, Flow, Goto, Input, LocatorCall, Press, Select
from flowmap.infer import InferError, infer, registrable, same_site, splice, visible_match
from schema.validate import validate_card

SITE = "https://shop.test"
M = ["qzvx4821", "bkrt0937"]  # markers of marker runs 0 and 1
REAL = ["blue mug", "desk lamp"]


def flow(**over: Any) -> Flow:
    data: dict[str, Any] = {
        "domain": "shop.test",
        "action": "search",
        "inputs": {"query": Input(examples=REAL)},
        "steps": [
            Goto(url=SITE + "/"),
            Fill(target="#q", value="{{query}}"),
            Press(target="#q", key="Enter"),
        ],
        "results_selector": ".r",
    }
    data.update(over)
    return Flow(**data)


class Builder:
    """Builds one run's exchanges in order."""

    def __init__(self, kind: str, index: int, values: dict[str, str], markers: dict[str, str] | None = None):
        self.run = Run(
            run_id=f"s-{kind}{index}",
            session="s",
            domain="shop.test",
            action="search",
            kind=kind,  # type: ignore[arg-type]
            index=index,
            values=values,
            markers=markers or {},
            started_at="t",
        )

    def add(
        self,
        url: str,
        *,
        step: int = 2,
        method: str = "GET",
        body: Any = None,
        status: int = 200,
        ctype: str = "application/json",
        headers: dict[str, str] | None = None,
        post: str | None = None,
        req_ctype: str | None = None,
        set_cookie: str | None = None,
        phase: str = "flow",
        rtype: str = "fetch",
    ) -> Exchange:
        req = list((headers or {}).items())
        if req_ctype:
            req.append(("content-type", req_ctype))
        resp = [("content-type", ctype)]
        if set_cookie:
            resp.append(("set-cookie", set_cookie))
        ex = Exchange(
            id=len(self.run.exchanges),
            step=step,
            phase=phase,
            resource_type=rtype,
            method=method,  # type: ignore[arg-type]
            url=url,
            request_headers=req,
            post_data=post,
            status=status,
            response_headers=resp,
            body=body if isinstance(body, str) or body is None else json.dumps(body),
            started_ms=len(self.run.exchanges) * 10.0,
        )
        self.run.exchanges.append(ex)
        return ex


def results(names: list[str], total: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "results": [{"name": n, "id": f"id-{i}", "price": 10 + i} for i, n in enumerate(names)]
    }
    if total is not None:
        out["total"] = total
    return out


def runs_with(make: Any, *, marker_runs: int = 2, real_runs: int = 2) -> list[Run]:
    """``make(builder, value, i, kind)`` adds a run's exchanges."""
    out = []
    for i in range(marker_runs):
        b = Builder("marker", i, {"query": M[i]}, {"query": M[i]})
        make(b, M[i], i, "marker")
        out.append(b.run)
    for i in range(real_runs):
        b = Builder("real", i, {"query": REAL[i]})
        b.run.visible = [f"{REAL[i].title()} {n}" for n in ("one", "two", "three")]
        make(b, REAL[i], i, "real")
        out.append(b.run)
    return out


def names(value: str, kind: str) -> list[str]:
    return [] if kind == "marker" else [f"{value.title()} {n}" for n in ("one", "two", "three")]


def search_step(inferred: Any) -> dict[str, Any]:
    return inferred.steps[-1]


# ---------- helpers ----------


def test_registrable_and_same_site() -> None:
    assert registrable("www.api.example.com") == "example.com"
    assert registrable("a.foo.co.uk") == "foo.co.uk"
    assert registrable("127.0.0.1") == "127.0.0.1"
    assert same_site("https://api.shop.test/x", {"shop.test"})
    assert not same_site("https://analytics.other.test/x", {"shop.test"})
    assert not same_site("http://localhost:1/x", {"127.0.0.1"})


def test_visible_match_partial() -> None:
    assert visible_match("Desk lamp", "  desk   LAMP ")
    assert visible_match(12.5, "Price: £12.50")  # the visible text holds the value
    assert visible_match("Desk lamp with a long name", "Desk lamp with")  # the page cut it short
    assert not visible_match("ab", "Cab rank")  # too short to count as contained
    assert not visible_match("", "x")


def test_splice() -> None:
    assert splice("(keywords:abc,start:0)", [("abc", "{{q}}")]) == "(keywords:{{q}},start:0)"
    assert splice("a-a", [("a", "X")]) == "X-X"


# ---------- encodings and filters ----------


def test_query_value_with_a_space() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        b.add(SITE + "/", step=0, rtype="document", ctype="text/html", body="<html>")
        b.add(SITE + "/api/search?" + urlencode({"q": v, "lang": "en"}), body=results(names(v, kind)))

    inf = infer(flow(), runs_with(make))
    req = search_step(inf)["request"]
    assert req["url"] == "{{base_url}}/api/search"
    assert req["query"] == {"q": "{{params.query}}", "lang": "en"}
    assert inf.base_url == SITE and inf.kind == "read" and inf.confidence == 1.0


def test_path_value_gets_urlencode() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        b.add(f"{SITE}/api/search/{quote(v)}/items", body=results(names(v, kind)))

    req = search_step(infer(flow(), runs_with(make)))["request"]
    assert req["url"] == "{{base_url}}/api/search/{{params.query | urlencode}}/items"


def test_json_body_value() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        post = json.dumps({"query": {"text": v, "limit": 20}, "flags": [True]})
        b.add(
            SITE + "/graphql",
            method="POST",
            post=post,
            req_ctype="application/json",
            body=results(names(v, kind)),
        )

    inf = infer(flow(kind="read"), runs_with(make))
    req = search_step(inf)["request"]
    assert req["body"] == {"query": {"text": "{{params.query}}", "limit": 20}, "flags": [True]}
    assert req["headers"] == {"content-type": "application/json"}
    assert inf.kind == "read"  # a POST search: the flow says read
    assert any("treated as a read" in r for r in inf.reasons)


def test_form_body_gets_urlencode() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        post = urlencode({"q": v, "type": "all"})
        b.add(
            SITE + "/search",
            method="POST",
            post=post,
            req_ctype="application/x-www-form-urlencoded",
            body=results(names(v, kind)),
        )

    req = search_step(infer(flow(kind="read"), runs_with(make)))["request"]
    assert req["body"] == "q={{params.query | urlencode}}&type=all"


def test_restli_value_gets_escape_restli() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        variables = f"(keywords:{v},start:0,origin:GLOBAL)"
        b.add(
            SITE + "/voyager/api/graphql?" + urlencode({"variables": variables, "queryId": "abc.123"}),
            body={"data": {"search": results(names(v, kind))}},
        )

    inf = infer(flow(), runs_with(make))
    query = search_step(inf)["request"]["query"]
    assert query["variables"] == "(keywords:{{params.query | escape_restli}},start:0,origin:GLOBAL)"
    assert inf.changed_signals == ["body_contains:PersistedQueryNotFound"]


# ---------- picking the request ----------


def test_ignores_typeahead_third_party_and_page_load() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        b.add(SITE + "/", step=0, rtype="document", ctype="text/html", body="<html>")
        b.add(SITE + "/api/trending", step=0, body=results(names(v, kind)))  # same data, before submit
        b.add(SITE + "/api/suggest?q=" + v[:3], step=1, body={"s": [v[:3]]})  # typed so far
        b.add("https://analytics.other.test/collect?term=" + quote(v), body={"ok": 1})
        b.add(SITE + "/api/search?q=" + quote(v), body=results(names(v, kind)))

    inf = infer(flow(), runs_with(make))
    assert search_step(inf)["request"]["url"] == "{{base_url}}/api/search"


def test_nested_arrays() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        clusters = [{"items": [{"item": {"title": n, "urn": f"u{j}"}} for j, n in enumerate(names(v, kind))]}]
        body = {"data": {"clusters": clusters, "paging": {"count": 3}}}
        b.add(SITE + "/api/search?q=" + quote(v), body=body)

    inf = infer(flow(), runs_with(make))
    step = search_step(inf)
    assert step["extract"]["items"]["expr"] == "$.data.clusters[*].items[*]"
    assert step["expect"]["paths"] == ["$.data.clusters[*].items"]
    assert list(inf.output["fields"])[0] == "$.item.title"  # the matched field first
    assert inf.field_samples["$.item.urn"] == ["u0", "u1", "u2"]


def test_partial_visible_values_are_enough_above_60_percent() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        shown = names(v, kind)
        b.add(
            SITE + "/api/search?q=" + quote(v), body=results(shown[:2] + ["Something else"] if shown else [])
        )

    inf = infer(flow(), runs_with(make))
    assert any("matches 67%" in r for r in inf.reasons) and inf.confidence < 1

    def worse(b: Builder, v: str, i: int, kind: str) -> None:
        shown = names(v, kind)
        b.add(SITE + "/api/search?q=" + quote(v), body=results(shown[:1] + ["x1", "x2"] if shown else []))

    with pytest.raises(InferError, match="doesn't hold what the page showed"):
        infer(flow(), runs_with(worse))


def test_a_page_rendered_on_the_server_is_read_with_css() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        b.add(
            SITE + "/search?q=" + quote(v),
            rtype="document",
            ctype="text/html",
            body="<ul>" + "".join(f"<li class=r>{n}</li>" for n in names(v, kind)) + "</ul>",
        )

    out = infer(flow(), runs_with(make))  # level 2: the page's own HTML holds the results
    step = out.steps[-1]
    assert step["extract"]["items"] == {"from": "body", "type": "css", "expr": ".r"}
    assert step["expect"]["content_type"] == "html" and out.output["from"] == f"steps.{step['id']}.items"
    # A locator (not CSS) from the recording: a selector is built from the HTML instead.
    locator = [LocatorCall(method="get_by_role", args=["listitem"])]
    by_role = infer(flow(results_selector=locator), runs_with(make))
    assert by_role.steps[-1]["extract"]["items"] == {"from": "body", "type": "css", "expr": "li"}


def test_nothing_carries_the_input() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        b.add(SITE + "/api/feed", body={"x": [1]})

    with pytest.raises(InferError, match="no request carries"):
        infer(flow(), runs_with(make))


def test_two_equal_candidates_fail_loudly() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        b.add(SITE + "/api/a?q=" + quote(v), body=results(names(v, kind)))
        b.add(SITE + "/api/b?q=" + quote(v), body=results(names(v, kind)))

    with pytest.raises(InferError, match="two requests fit equally well"):
        infer(flow(), runs_with(make))


# ---------- values that change per run ----------


def test_cookie_csrf_with_strip_quotes() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        sid = secret_hash(f"ajax:{i}{kind}")
        b.add(
            SITE + "/",
            step=0,
            rtype="document",
            ctype="text/html",
            body="<html>",
            set_cookie=f'JSESSIONID="{sid}"; Path=/',
        )
        b.run.cookies = [CookieRecord(name="JSESSIONID", value=f'"{sid}"', domain="shop.test")]
        b.add(
            SITE + "/api/search?q=" + quote(v),
            headers={"csrf-token": sid, "accept": "application/json"},
            body=results(names(v, kind)),
        )

    inf = infer(flow(), runs_with(make))
    home, search = inf.steps
    assert home["request"] == {"method": "GET", "url": "{{base_url}}/"}
    assert home["extract"] == {"jsessionid": {"from": "cookies", "type": "jsonpath", "expr": "$.JSESSIONID"}}
    assert search["request"]["headers"]["csrf-token"] == "{{steps.home.jsessionid | strip_quotes}}"


def test_login_cookie_uses_the_session() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        sid = secret_hash(f"li_at{i}{kind}")
        b.run.cookies = [CookieRecord(name="li_at", value=sid, domain="shop.test")]
        b.add(SITE + "/api/search?q=" + quote(v), headers={"x-token": sid}, body=results(names(v, kind)))

    inf = infer(flow(auth_provider="main"), runs_with(make))
    assert search_step(inf)["request"]["headers"]["x-token"] == "{{session.cookies.li_at}}"
    with pytest.raises(InferError, match="needs cookie 'li_at'") as e:
        infer(flow(), runs_with(make))
    assert "auth_provider" in e.value.reasons[0]


def test_two_step_token_dependency() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        token = f"tok-{kind}-{i}"
        b.add(SITE + "/api/session", step=1, body={"session": {"token": token, "ttl": 60}})
        b.add(SITE + "/api/search?" + urlencode({"q": v, "token": token}), body=results(names(v, kind)))

    inf = infer(flow(), runs_with(make))
    session, search = inf.steps
    assert session["id"] == "session"
    assert session["extract"] == {"token": {"from": "body", "type": "jsonpath", "expr": "$.session.token"}}
    assert search["request"]["query"]["token"] == "{{steps.session.token}}"


def test_csrf_from_an_html_page() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        csrf = f"c{kind}{i}x9"
        b.add(
            SITE + "/form",
            step=0,
            rtype="document",
            ctype="text/html",
            body=f'<meta name="csrf" content="{csrf}"><p>hi</p>',
        )
        b.add(SITE + "/api/search?q=" + quote(v), headers={"x-csrf": csrf}, body=results(names(v, kind)))

    form, search = infer(flow(), runs_with(make)).steps
    ex = form["extract"]["value"]
    assert ex == {"from": "body", "type": "regex", "expr": 'meta name="csrf" content="([^"\'<>&\\s]+)'}
    assert search["request"]["headers"]["x-csrf"] == "{{steps.form.value}}"


def test_cache_buster_dropped_and_unknown_header_dropped() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        url = SITE + "/api/search?" + urlencode({"q": v, "_": f"17000000{i}{len(kind)}000"})
        b.add(url, headers={"x-request-id": f"r{i}{kind}"}, body=results(names(v, kind)))

    inf = infer(flow(), runs_with(make))
    req = search_step(inf)["request"]
    assert req["query"] == {"q": "{{params.query}}"} and "headers" not in req
    assert any("cache buster" in r for r in inf.reasons)
    assert any("x-request-id" in r for r in inf.reasons) and inf.confidence == 0.9


def test_unexplained_query_value_lowers_confidence() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        b.add(
            SITE + "/api/search?" + urlencode({"q": v, "sig": f"zz{i}{kind}"}), body=results(names(v, kind))
        )

    inf = infer(flow(), runs_with(make))
    assert "sig" not in search_step(inf)["request"]["query"]
    assert inf.confidence == 0.7


# ---------- pagination ----------


def test_offset_pagination_with_total() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        shown = names(v, kind)
        b.add(
            SITE + "/api/search?" + urlencode({"q": v, "start": 0, "count": 3}),
            body={"meta": {"count": 3, "total": 40}, **results(shown)},
        )
        if kind == "real" and i == 0:
            b.run.visible_page2 = ["p2 a", "p2 b", "p2 c"]
            b.add(
                SITE + "/api/search?" + urlencode({"q": v, "start": 3, "count": 3}),
                step=3,
                phase="next_page",
                body={"meta": {"count": 3, "total": 40}, **results(b.run.visible_page2)},
            )

    inf = infer(flow(), runs_with(make))
    step = search_step(inf)
    assert step["request"]["query"] == {
        "q": "{{params.query}}",
        "start": "{{params.page | offset:3}}",
        "count": "3",
    }
    assert step["extract"]["total"] == {"from": "body", "type": "jsonpath", "expr": "$.meta.total"}
    assert inf.output["limit"] == 3
    assert inf.output["next_params"] == {
        "param": "page",
        "increment": 1,
        "page_size": 3,
        "until": "steps.search.total",
    }
    assert inf.params["page"] == {
        "type": "integer",
        "required": False,
        "default": 1,
        "description": "1-based page number",
    }


def test_page_number_pagination_without_total() -> None:
    def make(b: Builder, v: str, i: int, kind: str) -> None:
        b.add(SITE + "/api/search?" + urlencode({"q": v, "page": 1}), body=results(names(v, kind)))
        if kind == "real" and i == 0:
            b.run.visible_page2 = ["p2 a", "p2 b", "p2 c"]
            b.add(
                SITE + "/api/search?" + urlencode({"q": v, "page": 2}),
                step=3,
                phase="next_page",
                body=results(b.run.visible_page2),
            )

    inf = infer(flow(), runs_with(make))
    assert search_step(inf)["request"]["query"]["page"] == "{{params.page}}"
    assert inf.output["next_params"] == {"param": "page", "increment": 1, "page_size": 3}


# ---------- writes and params ----------


def test_write_detected_and_enum_param() -> None:
    f = Flow(
        domain="shop.test",
        action="add_to_cart",
        inputs={"sku": Input(examples=["A1", "B2"]), "size": Input(marker=False, examples=["s", "m"])},
        steps=[
            Goto(url=SITE + "/p/{{sku}}"),
            Select(target="#size", value="{{size}}"),
            Press(target="#add", key="Enter"),
        ],
    )
    runs = []
    for kind, idx, values in (
        ("marker", 0, {"sku": M[0], "size": "s"}),
        ("real", 0, {"sku": "A1", "size": "s"}),
        ("real", 1, {"sku": "B2", "size": "m"}),
    ):
        b = Builder(kind, idx, values, {"sku": values["sku"]} if kind == "marker" else None)
        b.add(
            SITE + "/api/cart",
            method="POST",
            step=2,
            req_ctype="application/json",
            post=json.dumps({"sku": values["sku"], "size": values["size"], "qty": 1}),
            body={"items": 1},
        )
        runs.append(b.run)
    inf = infer(f, runs)
    assert inf.kind == "write" and inf.examples == [] and inf.output == {"from": None}
    assert search_step(inf)["request"]["body"] == {
        "sku": "{{params.sku}}",
        "size": "{{params.size}}",
        "qty": 1,
    }
    assert inf.params["size"] == {"type": "enum", "required": True, "values": ["s", "m"], "example": "s"}
    assert inf.params["sku"]["type"] == "string"


# ---------- end to end on the bookshop ----------


def _card(inf: Any) -> dict[str, Any]:
    return {
        "schema_version": "0.4",
        "domain": "bookshop.test",
        "tool_prefix": "books",
        "base_url": inf.base_url,
        "provenance": {"mapped_at": "2026-11-07", "source": "test", "mapper_version": "test"},
        "actions": [
            {
                "name": inf.action,
                "description": "d",
                "kind": inf.kind,
                "spends": False,
                "idempotent": inf.kind == "read",
                "requires_confirmation": inf.kind == "write",
                "params": inf.params,
                "steps": inf.steps,
                "output": {
                    **inf.output,
                    "fields": {f"f{i}": p for i, p in enumerate(inf.output.get("fields", {}))},
                },
                "examples": inf.examples,
            }
        ],
    }


@pytest.mark.browser
async def test_bookshop_actions_infer_to_valid_cards(bookshop: str) -> None:
    pytest.importorskip("playwright.async_api")
    from flowmap.capture import capture_flow
    from tests.flowmap.conftest import book_flow, cart_flow, search_flow

    search = infer(f := search_flow(bookshop), await capture_flow(f, pause_s=0))
    assert [s["id"] for s in search.steps] == ["home", "search"]
    assert search.output["next_params"]["until"] == "steps.search.total"
    book = infer(f := book_flow(bookshop), await capture_flow(f, pause_s=0))
    assert [s["id"] for s in book.steps] == ["token", "books"]
    cart = infer(f := cart_flow(bookshop), await capture_flow(f, pause_s=0, marker_runs=1))
    assert cart.kind == "write"
    for inf in (search, book, cart):
        report = validate_card(_card(inf))
        assert report.ok, report.errors
        assert inf.confidence == 1.0, inf.reasons
        assert "127.0.0.1" not in json.dumps(inf.steps)  # the origin is {{base_url}} everywhere


def test_a_write_is_the_request_that_was_held_back() -> None:
    """LinkedIn's follow: background POSTs (presence, live updates) carry the input too, but the write is
    the one the browser held back while mapping."""

    def make(b: Builder, v: str, i: int, kind: str) -> None:
        body = '{"item": "' + v + '"}'
        b.add(SITE + "/api/presence", method="POST", post=body, req_ctype="application/json", body={})
        held = b.add(SITE + "/api/cart", method="POST", post=body, req_ctype="application/json", body={})
        held.response_headers.append(("x-flowmap-held", "1"))

    out = infer(flow(kind="write", results_selector=None), runs_with(make))
    assert out.steps[-1]["request"]["url"].endswith("/api/cart") and out.kind == "write"


def test_an_always_empty_answer_is_not_a_read() -> None:
    """A tracking ping that carries the search term but answers nothing isn't the search."""

    def make(b: Builder, v: str, i: int, kind: str) -> None:
        b.add(SITE + "/api/track?term=" + quote(v), method="POST", body="", ctype="text/plain")

    with pytest.raises(InferError, match="no request carries"):
        infer(flow(), runs_with(make))


def test_a_selector_from_the_live_page_is_rebuilt_for_the_html() -> None:
    """The site's scripts restructure the page after it loads (LinkedIn's profiles): the recorder's selector
    from the live page finds nothing in the HTML a plain request gets, so one is built from that HTML."""

    def make(b: Builder, v: str, i: int, kind: str) -> None:
        rows = "".join(
            f'<li class="hit"><a class="name" href="/p/{n}">{n}</a> <span>Premium</span></li>'
            for n in names(v, kind)
        )
        b.add(SITE + "/search?q=" + quote(v), rtype="document", ctype="text/html", body=f"<ul>{rows}</ul>")

    out = infer(flow(results_selector="div.live-card a.live-title"), runs_with(make))
    step = out.steps[-1]
    items = step["extract"]["items"]
    assert items["type"] == "css" and items["expr"] == "li.hit a.name"
    assert any("rebuilt" in r for r in out.reasons)


def test_a_held_back_tie_goes_against_background_chatter() -> None:
    """LinkedIn's follow: the live-update subscription and a beacon with a random-token path are held back
    too (they're after the click); the follow itself wins the tie, and a write's stand-in response isn't
    checked for results."""

    def make(b: Builder, v: str, i: int, kind: str) -> None:
        body = '{"item": "' + v + '"}'
        beacon = "/tapi2hqLqA7G0fpO"
        for path in ("/realtime/frontendSubscriptions", beacon, "/api/followingStates/" + quote(v)):
            ex = b.add(SITE + path, method="POST", post=body, req_ctype="application/json", body={})
            ex.response_headers.append(("x-flowmap-held", "1"))

    out = infer(flow(kind="write"), runs_with(make))  # results_selector ".r" set: not checked for a write
    assert "/api/followingStates/" in out.steps[-1]["request"]["url"] and out.kind == "write"


def test_a_write_reads_an_id_from_the_page_its_inputs_opened() -> None:
    """LinkedIn's follow: every run followed the same company, so its id was the same in every request, but
    another company has another id. The id is read from the company page, not fixed."""

    def make(b: Builder, v: str, i: int, kind: str) -> None:
        page = '<html><div data-urn="urn:li:organization:74126343">' + v + "</div></html>"
        b.add(SITE + "/company/" + quote(v) + "/", step=1, body=page, ctype="text/html", rtype="document")
        if kind == "real":  # the follow button exists only on a real company's page
            ex = b.add(
                SITE + "/api/followingStates/urn:li:company:74126343",
                method="POST",
                post='{"following": true}',
                req_ctype="application/json",
                body={},
                headers={"referer": SITE + "/company/" + v + "/"},
            )
            ex.response_headers.append(("x-flowmap-held", "1"))

    steps = [Goto(url=SITE + "/company/{{query}}/"), Click(target="#follow")]
    out = infer(flow(kind="write", steps=steps), runs_with(make))
    url = out.steps[-1]["request"]["url"]
    assert "74126343" not in url and "{{steps." in url, url
    page = out.steps[0]
    assert "{{params.query" in page["request"]["url"] and page["extract"], page


def test_a_body_whose_shape_differs_between_runs_does_not_crash() -> None:
    """Amazon: a JSON body with an extra branch in some runs only: the map goes on (KeyError before)."""
    from flowmap.infer import _set_path

    assert _set_path({"a": {"b": 1}}, ("a", "b"), "{{params.q}}") == {"a": {"b": "{{params.q}}"}}
    assert _set_path({"a": {}}, ("entities", "x"), "{{params.q}}") == {"a": {}}
    assert _set_path({"a": [1]}, ("a", 5), "{{params.q}}") == {"a": [1]}


def test_a_request_missing_from_some_real_runs_gives_way() -> None:
    """A widget that loaded in one real run with the results in it loses to the search that's in every run."""

    def make(b: Builder, v: str, i: int, kind: str) -> None:
        b.add(SITE + "/api/search?q=" + quote(v), body=results(names(v, kind)))
        if kind == "real" and i == 0:
            b.add(SITE + "/api/widget?q=" + quote(v), body=results(names(v, kind), total=99))

    out = infer(flow(), runs_with(make))
    assert out.steps[-1]["request"]["url"].endswith("/api/search")


def stream(found: list[str]) -> str:
    """A streamed answer (React Server Components rows): the results point at their names in other rows."""
    lines = ['0:{"results":"$1"}']
    items = [{"name": f"${hex(i + 2)[2:]}", "url": f"/c/{i}"} for i in range(len(found))]
    lines.append("1:" + json.dumps(items))
    lines += [f"{hex(i + 2)[2:]}:" + json.dumps(n) for i, n in enumerate(found)]
    lines.append('9:I["chunk.js",[]]')  # a module row: not JSON, left alone
    return "\n".join(lines) + "\n"


def test_a_streamed_answer_is_read_with_resolve_rows() -> None:
    """LinkedIn's newer pages answer with streamed rows that point to each other: the rows are filled in,
    the results found in them, and the extract reads them the same way (mapper v1's resolve: "rows")."""
    from runtime.run.extract import ResponseView, run_extract
    from schema.card import Extract

    def make(b: Builder, v: str, i: int, kind: str) -> None:
        b.add(
            SITE + "/rsc/search?q=" + quote(v), body=stream(names(v, kind)), ctype="application/octet-stream"
        )

    out = infer(flow(), runs_with(make))
    items = out.steps[-1]["extract"]["items"]
    assert items["type"] == "regex" and items["resolve"] == "rows" and items["parse"] == "json"
    assert any("stream" in r for r in out.reasons)
    got = run_extract(Extract.model_validate(items), ResponseView(stream(["Desk lamp", "Mug"]), {}, {}))
    assert [g["name"] for g in got] == ["Desk lamp", "Mug"]
