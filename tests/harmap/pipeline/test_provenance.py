import json
import re
from urllib.parse import quote

import pytest
from synthetic import action, har_entry, make_bundle

from spider.harmap.helpers import jsonpaths
from spider.harmap.pipeline.goal import counterpart, find_read_goal
from spider.harmap.pipeline.provenance import build_regex, trace
from spider.harmap.pipeline.steps import plan_steps

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


# --- picked items and fixed values -----------------------------------------------------------

PEOPLE = [  # (id, first name, url slug, member number): the list a person clicks one of
    ("ACoAAxk1Q9", "Ada", "ada-lovelace-7", "736419872"),
    ("ACoAAbq7Z2", "Grace", "grace-hopper-2", "558120443"),
    ("ACoAAmw4R8", "Alan", "alan-turing-5", "901337425"),
]


def _people_page() -> str:
    rows = "".join(
        f'<li data-id="{pid}" data-member="{num}"><a href="/in/{slug}">{name}</a>'
        f'<button data-id="{pid}" data-key="connect-{slug}">Connect</button></li>'
        for pid, name, slug, num in PEOPLE
    )
    return f"<html><body><ul>{rows}</ul><script>var fail='app.ui.FailToast';</script></body></html>"


def _connect(i: int, picked: int) -> list[dict]:
    pid, name, slug, num = PEOPLE[picked]
    body = {
        "personId": pid,
        "firstName": name,
        "profileUrl": f"{SITE}/in/{slug}",
        "buttonKey": f"connect-{slug}",
        "invitee": {"memberId": num},
        "state": f"state:invitation:member:{num}",
        "onFail": "app.ui.FailToast" if i == 0 else "app.ui.FailBanner",
    }
    return [
        har_entry(
            "GET", f"{SITE}/network/", at=0, mime="text/html", body=_people_page(), resource_type="document"
        ),
        har_entry(
            "POST", f"{SITE}/api/connect", at=5, post=json.dumps(body), post_mime="application/json", body={}
        ),
    ]


def _picked_runs():
    m1 = make_bundle("m1", _connect(0, 0), kind="write")
    m2 = make_bundle("m2", _connect(1, 1), kind="write")
    for b in (m1, m2):
        b.meta["inputs_via"] = {"person_id": "picked"}
    g1 = next(e for e in m1.entries if e.method == "POST")
    g2 = next(e for e in m2.entries if e.method == "POST")
    return m1, trace(m1, g1, m2, g2)


def test_every_detail_of_a_picked_item_becomes_an_input():
    """A click sends the person's id, name, url, and their slug and number again inside other values.
    None may stay as recorded (it would act on the recorded person every time)."""
    _, prov = _picked_runs()
    labs = {lab.address: lab for lab in prov.goal.labels}
    pid = labs["body.$.personId"]
    assert (pid.provenance, pid.params, pid.template) == ("param", ("person_id",), "{{params.person_id}}")
    assert labs["body.$.firstName"].params == ("first_name",)
    url, key = labs["body.$.profileUrl"], labs["body.$.buttonKey"]
    assert url.params == key.params  # one slug, two places: one input
    slug = url.params[0]
    assert url.template == f"{SITE}/in/{{{{params.{slug}}}}}"
    assert key.template == f"connect-{{{{params.{slug}}}}}"
    member, state = labs["body.$.invitee.memberId"], labs["body.$.state"]
    assert member.provenance == "param" and member.params == state.params  # the number, alone and in a urn
    assert state.template.startswith("state:invitation:member:{{params.")
    assert len({pid.params, labs["body.$.firstName"].params, url.params, member.params}) == 4


def test_a_code_name_that_differs_between_runs_is_not_an_input():
    _, prov = _picked_runs()
    lab = prov.goal.label("body.$.onFail")
    assert lab.provenance == "unknown" and not lab.params


def test_an_llm_namer_names_the_inputs_one_item_carries():
    m1 = make_bundle("m1", _connect(0, 0), kind="write")
    m2 = make_bundle("m2", _connect(1, 1), kind="write")
    for b in (m1, m2):
        b.meta["inputs_via"] = {"person_id": "picked"}
    seen = []

    def namer(groups):
        seen.extend(groups)
        return [f"detail_{i}" for i in range(len(groups))]

    g1 = next(e for e in m1.entries if e.method == "POST")
    g2 = next(e for e in m2.entries if e.method == "POST")
    prov = trace(m1, g1, m2, g2, namer=namer)
    assert len(seen) == 4 and {g["m1"] for g in seen} == {"ACoAAxk1Q9", "Ada", "ada-lovelace-7", "736419872"}
    assert {g["m2"] for g in seen} == {"ACoAAbq7Z2", "Grace", "grace-hopper-2", "558120443"}
    assert {p for lab in prov.goal.labels for p in lab.params} == {f"detail_{i}" for i in range(4)}


def _fixed(i: int, with_extra: bool) -> list[dict]:
    """A page, then a prefetch of the member's own profile that repeats the page's layout token and a
    code word, then the request: the token and the word are fixed, not fetched from the profile."""
    token, word = "Lay0ut9f3ab27c", "bindableBoolean"
    home = f'<html><script>window.layout="{token}";</script><p>{word}</p></html>'
    body = {"layout": token, "q": A if i == 0 else B}
    if with_extra:
        body["extra"] = word
    return [
        har_entry("GET", f"{SITE}/home/", at=0, mime="text/html", body=home, resource_type="document"),
        har_entry(
            "POST",
            f"{SITE}/in/my-own-name-123/",
            at=1,
            body={"layout": token, "w": word},
            response_headers={"x-layout": token},
        ),
        har_entry(
            "POST", f"{SITE}/api/do", at=5, post=json.dumps(body), post_mime="application/json", body={}
        ),
    ]


def test_fixed_values_do_not_pull_in_a_prefetched_profile():
    m1 = make_bundle("m1", _fixed(0, True), values={"query": A})
    m2 = make_bundle("m2", _fixed(1, False), values={"query": B})
    g1 = next(e for e in m1.entries if e.path == "/api/do")
    g2 = next(e for e in m2.entries if e.path == "/api/do")
    prov = trace(m1, g1, m2, g2)
    layout = prov.goal.label("body.$.layout")
    assert layout.provenance == "dependency" and layout.source.entry.path == "/home/"  # the first page
    assert prov.goal.label("body.$.extra").provenance == "constant"  # m2 never sent it; not a token
    assert "/in/my-own-name-123/" not in [d.entry.path for d in prov.steps]


def test_a_field_that_is_plainly_the_listed_input_takes_the_whole_value():
    """A conversation urn starts with the member's own id (the same in both runs); the agent passes the
    whole urn it got from list_conversations, so the input is the whole value, not the changing end."""
    me = "urn:li:fsd_profile:ACoAAme0001"

    def runs(i: int) -> list[dict]:
        threads = [f"urn:li:msg_conversation:({me},2-thread{n}AAA)" for n in range(3)]
        page = "<html>" + "".join(f'<a data-urn="{t}">thread</a><b data-urn="{t}"></b>' for t in threads)
        body = {"conversationUrn": threads[i], "text": A if i == 0 else B}
        return [
            har_entry(
                "GET", f"{SITE}/messaging/", at=0, mime="text/html", body=page, resource_type="document"
            ),
            har_entry(
                "POST", f"{SITE}/api/send", at=5, post=json.dumps(body), post_mime="application/json", body={}
            ),
        ]

    m1 = make_bundle("m1", runs(0), values={"text": A}, kind="write")
    m2 = make_bundle("m2", runs(1), values={"text": B}, kind="write")
    for b in (m1, m2):
        b.meta["inputs_via"] = {"conversation_urn": "picked", "text": "typed"}
    g1 = next(e for e in m1.entries if e.method == "POST")
    g2 = next(e for e in m2.entries if e.method == "POST")
    lab = trace(m1, g1, m2, g2).goal.label("body.$.conversationUrn")
    assert (lab.params, lab.template) == (("conversation_urn",), "{{params.conversation_urn}}")


def test_a_page_read_value_that_changes_with_the_click_is_the_picked_input():
    """No field matched the click, but the request carries a value read off the page (the selected row)
    that differs between the runs: that is the clicked item, not a fixed dependency."""

    def runs(i: int) -> list[dict]:
        pid = PEOPLE[i][0]
        page = f'<html><div class="selected" data-person="{pid}">chosen</div></html>'
        return [
            har_entry("GET", f"{SITE}/pick/", at=0, mime="text/html", body=page, resource_type="document"),
            har_entry("GET", f"{SITE}/api/compose?to={pid}", at=5, body={"ok": True}),
        ]

    m1, m2 = make_bundle("m1", runs(0)), make_bundle("m2", runs(1))
    for b in (m1, m2):
        b.meta["inputs_via"] = {"recipient": "picked"}
    g1, g2 = m1.entries[-1], m2.entries[-1]
    prov = trace(m1, g1, m2, g2)
    lab = prov.goal.label("query.to")
    assert (lab.provenance, lab.params) == ("param", ("recipient",))
    assert [d.entry.path for d in prov.steps] == ["/api/compose"]  # the page is no longer fetched
