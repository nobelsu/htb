import json
import re
from urllib.parse import quote

import pytest
from synthetic import action, har_entry, make_bundle

from harmap import jsonpaths
from harmap.pipeline.m3_goal import counterpart, find_read_goal
from harmap.pipeline.m4_provenance import build_regex, trace
from harmap.pipeline.m5_steps import plan_steps

SITE = "https://www.example.com"
# Markers with a space and a double quote, so raw / percent / JSON / Rest.li forms all differ.
A, B = 'zq xv"8841', 'kj tr"5527'
EPOCH_MS = "1793977200000"  # 2026-11-07T15:00:00Z, the synthetic capture time


def _runs(build):
    """Build m1/m2 bundles from ``build(marker, run_index) -> entries``; return (m1, m2, provenance)."""
    m1 = make_bundle("m1", build(A, 0), values={"query": A}, state_cookies={"JSESSIONID": '"ajax:42424242"'})
    m2 = make_bundle("m2", build(B, 1), values={"query": B}, state_cookies={"JSESSIONID": '"ajax:42424242"'})
    g1, g2 = find_read_goal(m1), find_read_goal(m2)
    return m1, m2, trace(m1, g1.entry, m2, g2.entry)


def _goal_label(prov, address):
    lab = prov.goal.label(address)
    assert lab is not None, [x.address for x in prov.goal.labels]
    return lab


LIST = {"elements": [], "total": 0}


# --- marker encodings → filters ------------------------------------------------------------


def test_marker_raw_form_in_header():
    _, _, prov = _runs(
        lambda m, i: [har_entry("GET", f"{SITE}/api/search", headers={"x-search": m}, body=LIST)]
    )
    lab = _goal_label(prov, "header.x-search")
    assert (lab.provenance, lab.encoding, lab.filter) == ("param", "raw", None)
    assert lab.template == "{{params.query}}"


def test_marker_percent_encoded_in_query_has_no_filter():
    _, _, prov = _runs(lambda m, i: [har_entry("GET", f"{SITE}/api/search?q={quote(m)}&start=0", body=LIST)])
    lab = _goal_label(prov, "query.q")
    assert (lab.provenance, lab.encoding, lab.filter) == ("param", "percent", None)
    assert lab.template == "{{params.query}}"  # the executor percent-encodes query values


def test_marker_percent_encoded_in_path_gets_urlencode():
    m1, _, prov = _runs(lambda m, i: [har_entry("GET", f"{SITE}/company/{quote(m)}/people", body=LIST)])
    lab = _goal_label(prov, "url.path[1]")
    assert (lab.provenance, lab.encoding, lab.filter) == ("param", "percent", "urlencode")
    [step] = plan_steps(prov, m1)
    assert step.request["url"] == "{{base_url}}/company/{{params.query | urlencode}}/people"


def test_marker_json_escaped_in_body_has_no_filter():
    def build(m, i):
        body = json.dumps({"variables": {"keywords": m, "count": 10, "exact": False}})
        return [har_entry("POST", f"{SITE}/graphql", post=body, post_mime="application/json", body=LIST)]

    m1, _, prov = _runs(build)
    lab = _goal_label(prov, "body.$.variables.keywords")
    assert (lab.provenance, lab.encoding, lab.filter) == ("param", "json", None)
    [step] = plan_steps(prov, m1)
    # whole-value placeholder (typed value kept); literals keep their JSON types
    assert step.request["body"] == {
        "variables": {"keywords": "{{params.query}}", "count": 10, "exact": False}
    }


def test_marker_restli_escaped_gets_escape_restli():
    def build(m, i):
        variables = f"(keywords:{quote(m)},origin:GLOBAL_SEARCH_HEADER,start:0)"
        return [
            har_entry(
                "GET", f"{SITE}/voyager/api/graphql?variables={variables}&queryId=voyager.x1", body=LIST
            )
        ]

    m1, _, prov = _runs(build)
    lab = _goal_label(prov, "query.variables.$.keywords")
    assert (lab.provenance, lab.encoding, lab.filter) == ("param", "restli", "escape_restli")
    [step] = plan_steps(prov, m1)
    assert step.request["query"] == {
        "variables": "(keywords:{{params.query | escape_restli}},origin:GLOBAL_SEARCH_HEADER,start:0)",
        "queryId": "voyager.x1",
    }


def test_marker_partial_value_templates_the_rest_literally():
    _, _, prov = _runs(
        lambda m, i: [har_entry("GET", f"{SITE}/api/search?q={quote('name:' + m + ' inc')}", body=LIST)]
    )
    assert _goal_label(prov, "query.q").template == "name:{{params.query}} inc"


def test_marker_only_in_m1_is_not_a_param():
    def build(m, i):
        q = quote(A)  # m2 also sends marker A: an echo, not the typed input
        return [har_entry("GET", f"{SITE}/api/search?q={quote(m)}&echo={q}", body=LIST)]

    _, _, prov = _runs(build)
    lab = _goal_label(prov, "query.echo")
    assert lab.provenance != "param"
    assert any("marker B" in n for n in lab.notes)


# --- session cookies ---------------------------------------------------------------------------


def test_session_cookie_header_with_strip_quotes_and_cookie_fields_left_to_jar():
    def build(m, i):
        return [
            har_entry(
                "GET",
                f"{SITE}/api/search?q={quote(m)}",
                headers={"csrf-token": "ajax:42424242"},
                cookies={"JSESSIONID": '"ajax:42424242"', "li_at": "AQEDAT-secret-value"},
                body=LIST,
            )
        ]

    m1, _, prov = _runs(build)
    csrf = _goal_label(prov, "header.csrf-token")
    assert (csrf.provenance, csrf.cookie, csrf.strip_quotes) == ("session_cookie", "JSESSIONID", True)
    for name in ("JSESSIONID", "li_at"):
        lab = _goal_label(prov, f"cookie.{name}")
        assert lab.provenance == "session_cookie" and "jar" in lab.notes[0]
    assert prov.uses_session
    [step] = plan_steps(prov, m1)
    assert step.request["headers"] == {"csrf-token": "{{session.cookies.JSESSIONID | strip_quotes}}"}
    assert "cookie" not in step.request["headers"]


# --- dependencies ------------------------------------------------------------------------------


def _token_flow(m, i):
    token = ["tok_a1b2c3d4e5", "tok_f6g7h8i9j0"][i]
    return [
        har_entry("GET", f"{SITE}/api/session", at=1, body={"token": token, "ttl": 300}),
        har_entry("GET", f"{SITE}/api/search?q={quote(m)}", at=2, headers={"x-token": token}, body=LIST),
    ]


def test_json_dependency_confirmed_on_m2_becomes_a_step():
    m1, _, prov = _runs(_token_flow)
    lab = _goal_label(prov, "header.x-token")
    assert lab.provenance == "dependency"
    assert lab.source.kind == "json" and lab.source.confirmed
    assert lab.source.extract == {"from": "body", "type": "jsonpath", "expr": "$.token"}
    steps = plan_steps(prov, m1)
    assert [s.id for s in steps] == ["session", "search"]
    assert steps[0].extract == {"token": {"from": "body", "type": "jsonpath", "expr": "$.token"}}
    assert steps[1].request["headers"]["x-token"] == "{{steps.session.token}}"


def test_html_dependency_regex_uses_stable_context():
    tokens = ["Zm9vYmFyMTIzNDU2", "cXV4cXV1eDc4OTAx"]

    def build(m, i):
        page = (
            f'<html><head><meta name="nonce" content="n{i}{i}{i}">'
            f'<meta name="csrf" content="{tokens[i]}"></head><body>hi</body></html>'
        )
        return [
            har_entry("GET", f"{SITE}/compose", at=1, mime="text/html", body=page, resource_type="document"),
            har_entry(
                "POST",
                f"{SITE}/api/search",
                at=2,
                post=f"csrf={tokens[i]}&q={quote(m)}",
                post_mime="application/x-www-form-urlencoded",
                body=LIST,
            ),
        ]

    m1, _, prov = _runs(build)
    lab = _goal_label(prov, "body.csrf")
    assert lab.provenance == "dependency" and lab.source.kind == "html" and lab.source.confirmed
    expr = lab.source.extract["expr"]
    assert lab.source.extract["type"] == "regex"
    assert re.findall(expr, m1.entries[0].response_body) == [tokens[0]]
    assert expr.startswith('name="csrf" content="([^"]+)"')


def test_set_cookie_dependency():
    def build(m, i):
        tok = ["ck_11112222aaaa", "ck_33334444bbbb"][i]
        return [
            har_entry(
                "GET",
                f"{SITE}/",
                at=1,
                mime="text/html",
                body="<p/>",
                response_headers={"Set-Cookie": f"csrftoken={tok}; Path=/"},
            ),
            har_entry(
                "GET",
                f"{SITE}/api/search?q={quote(m)}",
                at=2,
                headers={"x-csrftoken": tok},
                cookies={"csrftoken": tok},
                body=LIST,
            ),
        ]

    _, _, prov = _runs(build)
    lab = _goal_label(prov, "header.x-csrftoken")
    assert lab.provenance == "dependency" and lab.source.kind == "cookie"
    assert lab.source.extract == {"from": "cookies", "type": "jsonpath", "expr": "$.csrftoken"}


def test_json_source_preferred_over_nearer_html():
    def build(m, i):
        tok = ["tok_a1b2c3d4e5", "tok_f6g7h8i9j0"][i]
        return [
            har_entry("GET", f"{SITE}/api/session", at=1, body={"token": tok}),
            har_entry(
                "GET",
                f"{SITE}/page",
                at=1.5,
                mime="text/html",
                body=f"<i data-t='{tok}'></i>",
                resource_type="document",
            ),
            har_entry("GET", f"{SITE}/api/search?q={quote(m)}", at=2, headers={"x-token": tok}, body=LIST),
        ]

    _, _, prov = _runs(build)
    assert _goal_label(prov, "header.x-token").source.kind == "json"


# --- constants and unknowns ----------------------------------------------------------------------


def test_constants_unknown_timestamp_and_omitted_headers():
    def build(m, i):
        return [
            har_entry(
                "GET",
                f"{SITE}/search",
                at=1,
                mime="text/html",
                body="<a href='/search'>search</a>",
                resource_type="document",
            ),
            har_entry(
                "GET",
                f"{SITE}/api/search?q={quote(m)}&start=0"
                f"&_ts={int(EPOCH_MS) + i * 1000}&sig={['ab12cd34ef', 'zz98yy76xx'][i]}",
                at=2,
                headers={
                    "accept": "application/json",
                    "user-agent": "Mozilla/5.0",
                    "sec-ch-ua": '"x"',
                    "referer": f"{SITE}/search",
                },
                body=LIST,
            ),
        ]

    m1, _, prov = _runs(build)
    assert _goal_label(prov, "url.path[1]").provenance == "constant"  # 'search' is in the HTML, but a word
    assert _goal_label(prov, "query.start").provenance == "constant"
    assert _goal_label(prov, "header.accept").provenance == "constant"
    for h in ("user-agent", "sec-ch-ua", "referer"):
        assert _goal_label(prov, f"header.{h}").provenance == "omitted"
    ts_label, sig = _goal_label(prov, "query._ts"), _goal_label(prov, "query.sig")
    assert ts_label.provenance == "unknown" and "timestamp" in ts_label.notes[0]
    assert sig.provenance == "unknown"
    assert len(prov.warnings) == 2 and all("tier 'browser'" in w for w in prov.warnings)
    [step] = plan_steps(prov, m1)  # unreachable HTML page pruned
    assert step.request["headers"] == {"accept": "application/json"}


# --- helpers -------------------------------------------------------------------------------------


def test_build_regex_extends_context_until_unique():
    body1 = '<i x="1" v="AAAA1111">' + '<i x="1" v="other999">'
    body2 = '<i x="1" v="BBBB2222">' + '<i x="1" v="other999">'
    expr, unique = build_regex(body1, "AAAA1111", body2, "BBBB2222")
    assert unique and re.findall(expr, body1) == ["AAAA1111"] and re.findall(expr, body2) == ["BBBB2222"]


@pytest.mark.parametrize("path", ["$.token", "$.data['a-b'][0].id"])
def test_dependency_paths_compile(path):
    assert jsonpaths.compiles(path)


def test_counterpart_finds_same_endpoint_in_other_run():
    m1, m2, prov = _runs(_token_flow)
    src = prov.goal.labels[0].field  # any
    assert src
    session_m1 = m1.entries[0]
    found = counterpart(m2, session_m1, m1.values, before=m2.entries[-1].started)
    assert found is m2.entries[0]


def test_actions_used_for_typed_time():
    entries = [har_entry("GET", f"{SITE}/api/search?q={quote(A)}", at=2, body=LIST)]
    b = make_bundle("m1", entries, values={"query": A}, actions=[action("input", 1, selector="#q", value=A)])
    assert find_read_goal(b).typed_at is not None
