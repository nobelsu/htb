import json

from synthetic import har_entry

from harmap import jsonpaths
from harmap.pipeline.m1_load import entry_from_har
from harmap.pipeline.m2_normalize import leaf_fields, normalize

SITE = "https://www.example.com"


def _fields(raw: dict) -> dict:
    return {f.address: f for f in normalize(entry_from_har(0, raw))}


def test_path_segments_decoded_with_raw_kept():
    f = _fields(har_entry("GET", f"{SITE}/company/zq%20xv%2F88/about"))
    assert f["url.path[0]"].value == "company"
    assert f["url.path[1]"].value == "zq xv/88"
    assert f["url.path[1]"].raw == "zq%20xv%2F88"
    assert f["url.path[1]"].context == "path"
    assert f["url.path[2]"].value == "about"


def test_query_values_decoded_and_repeats_indexed():
    f = _fields(har_entry("GET", f"{SITE}/api/search?q=zq%20xv%2288(41)&start=10&tag=a&tag=b&plus=a+b"))
    assert f["query.q"].value == 'zq xv"88(41)'
    assert f["query.q"].raw == "zq%20xv%2288(41)"
    assert f["query.q"].context == "query"
    assert f["query.start"].value == "10"
    assert f["query.tag[0]"].value == "a" and f["query.tag[1]"].value == "b"
    assert f["query.plus"].value == "a b"


def test_headers_lowercased_and_transport_headers_skipped():
    f = _fields(
        har_entry(
            "GET",
            f"{SITE}/api",
            headers={
                "Host": "www.example.com",
                "Content-Length": "0",
                "Connection": "keep-alive",
                ":authority": "www.example.com",
                "X-Token": "t0k3n",
                "csrf-token": "ajax:1",
            },
            cookies={"sid": "abc"},
        )
    )
    headers = {a for a in f if a.startswith("header.")}
    assert headers == {"header.x-token", "header.csrf-token"}
    assert f["header.x-token"].value == "t0k3n"


def test_cookies_keep_quotes():
    f = _fields(har_entry("GET", f"{SITE}/api", cookies={"sid": "abc", "JSESSIONID": '"ajax:123"'}))
    assert f["cookie.sid"].value == "abc"
    assert f["cookie.JSESSIONID"].value == '"ajax:123"'
    assert f["cookie.JSESSIONID"].context == "cookie"


def test_cookies_fall_back_to_cookie_header():
    raw = har_entry("GET", f"{SITE}/api", headers={"Cookie": 'a=1; JSESSIONID="ajax:9"'})
    raw["request"]["cookies"] = []
    f = _fields(raw)
    assert f["cookie.a"].value == "1" and f["cookie.JSESSIONID"].value == '"ajax:9"'


def test_json_body_flattened_to_jsonpath_leaves_with_types():
    body = {
        "operationName": "Search",
        "variables": {"keywords": "zqxv8841", "start": 0, "exact": False, "filters": [{"k": "v"}]},
        "where": None,
    }
    f = _fields(har_entry("POST", f"{SITE}/graphql", post=json.dumps(body), post_mime="application/json"))
    assert f["body.$.variables.keywords"].value == "zqxv8841"
    assert f["body.$.variables.start"].value == 0
    assert f["body.$.variables.exact"].value is False
    assert f["body.$.variables.filters[0].k"].value == "v"
    assert f["body.$['where']"].value is None
    assert f["body.$.variables.keywords"].context == "json"
    for addr in f:
        assert jsonpaths.compiles(addr.removeprefix("body."))


def test_form_body_fields():
    f = _fields(
        har_entry(
            "POST",
            f"{SITE}/api/notes",
            post="csrf=abc%2B1&text=hello+zqxv8841",
            post_mime="application/x-www-form-urlencoded",
        )
    )
    assert f["body.csrf"].value == "abc+1"
    assert f["body.text"].value == "hello zqxv8841"
    assert f["body.text"].context == "form"


def test_other_text_body_is_one_field():
    f = _fields(har_entry("POST", f"{SITE}/x", post="raw<xml/>", post_mime="application/xml"))
    assert f["body"].value == "raw<xml/>" and f["body"].context == "text"


def test_restli_query_value_split_into_subfields():
    raw_value = "(start:0,query:(keywords:zq%20xv%2C%28a%29,flagshipSearchIntent:SEARCH_SRP))"
    f = _fields(har_entry("GET", f"{SITE}/voyager/api/graphql?variables={raw_value}&queryId=voyager.abc"))
    parent = f["query.variables"]
    assert parent.structure == "restli" and parent.raw == raw_value
    kw = f["query.variables.$.query.keywords"]
    assert kw.value == "zq xv,(a)"
    assert kw.raw == "zq%20xv%2C%28a%29"
    assert kw.context == "restli" and kw.parent == "query.variables"
    assert f["query.variables.$.start"].value == "0"
    assert f["query.queryId"].structure is None


def test_graphql_json_query_value_split_into_subfields():
    f = _fields(
        har_entry("GET", f"{SITE}/graphql?variables=%7B%22keywords%22%3A%22zqxv8841%22%2C%22n%22%3A5%7D")
    )
    assert f["query.variables"].structure == "json"
    assert f["query.variables.$.keywords"].value == "zqxv8841"
    assert f["query.variables.$.n"].value == 5
    assert f["query.variables.$.keywords"].context == "json"


def test_leaf_fields_replace_structured_parents():
    entry = entry_from_har(0, har_entry("GET", f"{SITE}/x?variables=(a:1,b:2)&c=3"))
    addrs = [f.address for f in leaf_fields(normalize(entry))]
    assert addrs == ["url.path[0]", "query.variables.$.a", "query.variables.$.b", "query.c"]
