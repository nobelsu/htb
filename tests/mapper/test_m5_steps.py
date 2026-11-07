"""M3 + M4 + M5 end to end on real headless captures of the fixture site (m1, m2, real)."""

import pytest
from synthetic import har_entry, make_bundle

from mapper.pipeline.m3_goal import counterpart, find_goal
from mapper.pipeline.m4_provenance import trace
from mapper.pipeline.m5_steps import plan_steps, slug_for
from mapper.pipeline.runs import load_task


@pytest.fixture(scope="module")
def search_runs(fixture_captures):
    for run in ("m1", "m2", "real"):
        fixture_captures("search", run)
    return load_task(fixture_captures.root / "127.0.0.1" / "search")


@pytest.fixture(scope="module")
def search_trace(search_runs):
    m1, m2 = search_runs["m1"], search_runs["m2"]
    g1, g2 = find_goal(m1), find_goal(m2)
    return m1, trace(m1, g1.entry, m2, g2.entry)


@pytest.mark.browser
def test_goal_is_api_search_in_every_run(search_runs):
    for run, bundle in search_runs.items():
        goal = find_goal(bundle)
        assert goal.entry.path == "/api/search", run
        assert not goal.tie
    real_goal = find_goal(search_runs["real"]).entry
    assert real_goal.response_json["total"] == 23


@pytest.mark.browser
def test_goal_fields_have_the_right_provenance(search_trace):
    _, prov = search_trace
    by_addr = {lab.address: lab for lab in prov.goal.labels}
    expected = {
        "url.path[0]": "constant",
        "url.path[1]": "constant",
        "query.q": "param",
        "query.start": "constant",
        "header.accept": "constant",
        "header.x-token": "dependency",
        "header.csrf-token": "session_cookie",
        "cookie.sid": "session_cookie",
        "cookie.JSESSIONID": "session_cookie",
    }
    for addr, provenance in expected.items():
        assert by_addr[addr].provenance == provenance, (addr, by_addr[addr])
    assert {lab.provenance for lab in by_addr.values()} <= {*expected.values(), "omitted"}
    assert by_addr["header.x-token"].source.confirmed
    assert prov.uses_session and not prov.unknowns and not prov.warnings


@pytest.mark.browser
def test_steps_for_search_with_session_token_dependency(search_trace):
    m1, prov = search_trace
    steps = [s.as_card() for s in plan_steps(prov, m1)]
    assert steps == [
        {
            "id": "session",
            "request": {
                "method": "GET",
                "url": "{{base_url}}/api/session",
                "query": {},
                "headers": {"accept": "*/*"},
                "body": None,
            },
            "extract": {"token": {"from": "body", "type": "jsonpath", "expr": "$.token"}},
        },
        {
            "id": "search",
            "request": {
                "method": "GET",
                "url": "{{base_url}}/api/search",
                "query": {"q": "{{params.query}}", "start": "0"},
                "headers": {
                    "accept": "application/json",
                    "csrf-token": "{{session.cookies.JSESSIONID | strip_quotes}}",
                    "x-token": "{{steps.session.token}}",
                },
                "body": None,
            },
            "extract": {},
        },
    ]


@pytest.mark.browser
def test_dependency_holds_on_the_real_run(search_runs, search_trace):
    """The session extract found on m1 also explains the real run's x-token."""
    _, prov = search_trace
    real = search_runs["real"]
    real_goal = find_goal(real).entry
    src = next(lab.source for lab in prov.goal.labels if lab.source)
    real_session = counterpart(real, src.entry, search_runs["m1"].values, before=real_goal.started)
    assert real_session.response_json["token"] == real_goal.header("x-token")


def test_slugs():
    site = "https://www.linkedin.com"
    cases = {
        f"{site}/api/search": "search",
        f"{site}/": "home",
        f"{site}/voyager/api/graphql?queryId=voyagerSearchDashClusters.b09": "search_dash_clusters",
        f"{site}/voyager/api/organization/companies": "companies",
        f"{site}/api/v2/12345": "step",
    }
    for url, slug in cases.items():
        b = make_bundle("m1", [har_entry("GET", url)], domain="linkedin.com")
        assert slug_for(b.entries[0]) == slug, url
