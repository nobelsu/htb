"""domains, jsonpaths and restli helpers."""

import pytest

from mapper import jsonpaths, restli
from mapper.domains import registrable_domain, same_site, tool_prefix


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("www.linkedin.com", "linkedin.com"),
        ("linkedin.com", "linkedin.com"),
        ("a.b.shop.co.uk", "shop.co.uk"),
        ("bbc.co.uk", "bbc.co.uk"),
        ("127.0.0.1", "127.0.0.1"),
        ("localhost", "localhost"),
        ("WWW.Example.COM.", "example.com"),
    ],
)
def test_registrable_domain(host, expected):
    assert registrable_domain(host) == expected


def test_same_site():
    assert same_site("api.linkedin.com", "linkedin.com")
    assert same_site("www.linkedin.com", "www.linkedin.com")
    assert not same_site("linkedin.com.evil.io", "linkedin.com")
    assert not same_site("127.0.0.2", "127.0.0.1")


@pytest.mark.parametrize(
    ("domain", "prefix"),
    [
        ("linkedin.com", "linkedin"),
        ("www.my-shop.co.uk", "my_shop"),
        ("127.0.0.1", "127_0_0_1"),
        ("averyveryverylongdomainname.com", "averyveryverylongdom"),
    ],
)
def test_tool_prefix(domain, prefix):
    assert tool_prefix(domain) == prefix


def test_jsonpath_building_compiles_and_round_trips():
    doc = {"a-b": {"where": [1, {"it's": "x"}]}, "ok": True, "empty": [], "obj": {}}
    leaves = dict(jsonpaths.flatten(doc))
    assert leaves == {
        "$['a-b']['where'][0]": 1,
        "$['a-b']['where'][1]['it\\'s']": "x",
        "$.ok": True,
        "$.empty": [],
        "$.obj": {},
    }
    for path, value in leaves.items():
        assert jsonpaths.compiles(path)
        assert jsonpaths.find(path, doc) == [value]


def test_walk_includes_containers():
    nodes = dict(jsonpaths.walk({"a": [{"b": 1}]}))
    assert set(nodes) == {"$", "$.a", "$.a[0]", "$.a[0].b"}


def test_restli_parse_linkedin_style():
    raw = (
        "(start:0,origin:GLOBAL_SEARCH_HEADER,query:(keywords:zq%20xv%2C%28a%29,"
        "queryParameters:List((key:resultType,value:List(COMPANIES))),note:''))"
    )
    assert restli.parse(raw) == {
        "start": "0",
        "origin": "GLOBAL_SEARCH_HEADER",
        "query": {
            "keywords": "zq xv,(a)",
            "queryParameters": [{"key": "resultType", "value": ["COMPANIES"]}],
            "note": "",
        },
    }
    assert restli.parse(restli.dumps(restli.parse(raw))) == restli.parse(raw)


def test_restli_escape_matches_spec_filter():
    # spec: escape_restli percent-encodes % ( ) , : ' and spaces
    assert restli.escape("a b(c),d:e'f%g") == "a%20b%28c%29%2Cd%3Ae%27f%25g"
    assert restli.escape("plain-value_1.2") == "plain-value_1.2"


def test_restli_try_parse():
    assert restli.try_parse("(a:1)") == {"a": "1"}
    assert restli.try_parse("%28a%3A1%29") == {"a": "1"}  # whole value encoded once
    assert restli.try_parse("List(1,2)") == ["1", "2"]
    assert restli.try_parse("plain") is None
    assert restli.try_parse("(unbalanced:") is None
    assert restli.try_parse("(a:1)(b:2)") is None
