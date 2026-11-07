"""M6 output/expect, M7 annotation and the optional LLM layer (offline, with a fake client)."""

import json
from types import SimpleNamespace

import pytest
from synthetic import har_entry, make_bundle

from mapper import jsonpaths
from mapper.llm import LLM
from mapper.pipeline.m3_goal import find_read_goal
from mapper.pipeline.m4_provenance import trace
from mapper.pipeline.m5_steps import plan_steps
from mapper.pipeline.m6_output import (
    expect_for,
    field_names,
    find_result_list,
    plan_output,
    scalar_leaves,
    structural_parent,
)
from mapper.pipeline.m7_annotate import annotate

SITE = "https://www.example.com"


@pytest.mark.parametrize(
    ("path", "parent"),
    [
        ("$.elements[0].id", "$.elements"),
        ("$.data.search.elements[*].items[*].item", "$.data.search.elements"),
        ("$.token", "$.token"),
        ("$.paging.total", "$.paging.total"),
        ("$['a-b'][3]", "$['a-b']"),
    ],
)
def test_structural_parent(path, parent):
    assert structural_parent(path) == parent


def test_result_list_is_the_one_that_grew():
    empty = {"elements": [], "facets": [{"k": "a"}], "meta": {"tags": ["x"]}}
    full = {"elements": [{"id": 1}, {"id": 2}], "facets": [{"k": "a"}], "meta": {"tags": ["x", "y", "z"]}}
    # tags grew by 2 as well, but elements hold objects
    assert find_result_list(empty, full) == "$.elements"
    assert find_result_list(full, full) is None
    assert find_result_list(None, {"data": {"hits": [{"a": 1}]}}) == "$.data.hits"


def test_field_names_and_scalar_leaves():
    item = {"name": "Acme", "logo": {"url": "/l.png", "w": 64}, "tags": ["a"], "url": "/c/acme"}
    assert scalar_leaves(item) == ["$.name", "$.logo.url", "$.logo.w", "$.tags[0]", "$.url"]
    assert field_names(["$.name", "$.logo.url", "$.url", "$['a-b']"]) == {
        "name": "$.name",
        "logo_url": "$.logo.url",
        "url": "$.url",
        "a_b": "$['a-b']",
    }


def _search_plan(items_real):
    def build(q, i, items):
        return [
            har_entry("GET", f"{SITE}/api/search?q={q}", at=1, body={"total": len(items), "elements": items})
        ]

    m1 = make_bundle("m1", build("zqxv8841", 0, []), values={"query": "zqxv8841"})
    m2 = make_bundle("m2", build("kjtr5527", 1, []), values={"query": "kjtr5527"})
    real = make_bundle("real", build("acme", 2, items_real), values={"query": "acme"})
    g1 = find_read_goal(m1)
    prov = trace(m1, g1.entry, m2, find_read_goal(m2).entry)
    [plan] = plan_steps(prov, m1)
    return plan, g1.entry, real.entries[0]


def test_plan_output_fallback_caps_fields_at_eight_and_sets_expect():
    item = {f"f{i}": i for i in range(12)}
    plan, m1_goal, real_goal = _search_plan([item, item])
    out = plan_output(plan, m1_goal, real_goal, "search", LLM(enabled=False))
    assert out.source == "fallback"
    assert out.output["from"] == "steps.search.items"
    assert out.output["limit"] == 2
    assert list(out.output["fields"]) == [f"f{i}" for i in range(8)]
    assert plan.extract["items"] == {"from": "body", "type": "jsonpath", "expr": "$.elements[*]"}
    assert expect_for(plan) == {"status": [200], "content_type": "json", "paths": ["$.elements"]}


def test_plan_output_without_growth_has_no_output():
    plan, m1_goal, real_goal = _search_plan([])
    out = plan_output(plan, m1_goal, real_goal, "search", LLM(enabled=False))
    assert out.output["from"] is None and out.warnings


# --- LLM layer with a fake client ---------------------------------------------------------------


class FakeClient:
    def __init__(self, payload=None, stop_reason="end_turn", error=None):
        self.payload, self.stop_reason, self.error, self.requests = payload, stop_reason, error, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        text = json.dumps(self.payload)
        return SimpleNamespace(
            stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=text)]
        )


def test_llm_off_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert LLM().enabled is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("MAPPER_NO_LLM", "1")
    assert LLM().enabled is False


def test_llm_request_shape_and_output_fields():
    client = FakeClient({"paths": ["$.name", "$.url", "$.not_a_candidate"]})
    llm = LLM(client=client, enabled=True)
    picked = llm.pick_output_fields(
        "search", {"name": "A", "url": "/a", "id": 1}, ["$.id", "$.name", "$.url"]
    )
    assert picked == ["$.name", "$.url"]  # unknown paths dropped
    req = client.requests[0]
    assert req["model"] == "claude-sonnet-5-5"
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert llm.calls[0].ok


@pytest.mark.parametrize(
    "client",
    [FakeClient({"paths": []}, stop_reason="refusal"), FakeClient(error=RuntimeError("network down"))],
)
def test_llm_failures_fall_back(client):
    llm = LLM(client=client, enabled=True)
    assert llm.pick_output_fields("t", {"a": 1}, ["$.a"]) is None
    assert llm.calls and not llm.calls[0].ok


def test_llm_never_sees_secrets():
    client = FakeClient({"description": "Search.", "params": [{"name": "query", "description": "text"}]})
    llm = LLM(client=client, enabled=True)
    entry = make_bundle(
        "m1", [har_entry("GET", f"{SITE}/api/search", headers={"x-token": "tok_SECRET_123"})]
    ).entries[0]
    ann = annotate(
        task="search",
        task_description="",
        kind_hint="read",
        goal=entry,
        params=["query"],
        real_inputs={"query": "acme"},
        output_fields=["name"],
        has_unknowns=False,
        llm=llm,
    )
    assert ann.described_by == "llm" and ann.fields["description"] == "Search."
    assert "tok_SECRET_123" not in json.dumps(client.requests)


# --- M7 heuristics ------------------------------------------------------------------------------


def _annotate(method, path, kind_hint="read", **kw):
    entry = make_bundle(
        "m1", [har_entry(method, f"{SITE}{path}", post="a=1" if method != "GET" else None)]
    ).entries[0]
    args = dict(
        task="t",
        task_description="Do the thing",
        kind_hint=kind_hint,
        goal=entry,
        params=["query"],
        real_inputs={"query": "acme"},
        output_fields=["name", "url"],
        has_unknowns=False,
        llm=LLM(enabled=False),
    )
    args.update(kw)
    return annotate(**args)


def test_read_annotation():
    ann = _annotate("GET", "/api/search")
    f = ann.fields
    assert (f["kind"], f["spends"], f["idempotent"], f["requires_confirmation"]) == (
        "read",
        False,
        True,
        False,
    )
    assert f["description"] == "Do the thing. Returns name, url for each result. Read-only; safe to retry."
    assert f["params"]["query"] == {
        "type": "string",
        "required": True,
        "description": 'Query, as typed into the site, e.g. "acme".',
        "example": "acme",
    }
    assert f["examples"] == [{"params": {"query": "acme"}, "min_items": 1}]
    assert "tier" not in f


def test_write_annotations():
    note = _annotate("POST", "/api/notes", kind_hint="write").fields
    assert (note["kind"], note["spends"], note["idempotent"], note["requires_confirmation"]) == (
        "write",
        False,
        False,
        True,
    )
    assert note["examples"] == [] and "ask the user" in note["description"]
    assert _annotate("POST", "/api/checkout", kind_hint="write").fields["spends"] is True
    assert _annotate("PUT", "/api/profile", kind_hint="write").fields["idempotent"] is True
    assert _annotate("POST", "/voyager/api/feed/follow", kind_hint="write").fields["idempotent"] is True


def test_kind_mismatch_and_unknowns_are_reported():
    ann = _annotate("POST", "/api/x", kind_hint="read", has_unknowns=True)
    assert ann.fields["kind"] == "write" and ann.fields["tier"] == "browser"
    assert any("operator said 'read'" in w for w in ann.warnings)


def test_jsonpaths_all_compile():
    for p in ["$.elements[*]", "$.data['x-y'][0]"]:
        assert jsonpaths.compiles(p)
