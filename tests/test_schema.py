import copy
import json
from pathlib import Path
from typing import Any

import pytest

from schema import Card, Envelope, Meta, ResultCode, validate_card
from schema.params import coerce_params, input_schema

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = json.loads((ROOT / "action_map.example.json").read_text())


@pytest.fixture
def card() -> dict[str, Any]:
    return copy.deepcopy(EXAMPLE)


def action(card: dict[str, Any], name: str) -> dict[str, Any]:
    return next(a for a in card["actions"] if a["name"] == name)


def assert_fails(card: dict[str, Any], *fragments: str) -> None:
    report = validate_card(card)
    assert not report.ok, "expected the card to be rejected"
    text = "\n".join(report.errors)
    for frag in fragments:
        assert frag in text, f"{frag!r} not in errors:\n{text}"


def test_example_card_passes() -> None:
    report = validate_card(EXAMPLE)
    assert report.ok, report.errors
    assert report.warnings == []
    assert isinstance(report.card, Card)


def test_example_round_trips() -> None:
    card = Card.model_validate(EXAMPLE)
    assert Card.model_validate(card.to_json_dict()) == card


def test_unknown_filter(card: dict[str, Any]) -> None:
    action(card, "get_company_people")["steps"][0]["request"]["query"]["universalName"] = (
        "{{params.company | lowercase}}"
    )
    assert_fails(card, "get_company_people.steps.company", "unknown filter 'lowercase'")


def test_bad_offset_filter(card: dict[str, Any]) -> None:
    q = action(card, "search_companies")["steps"][0]["request"]["query"]
    q["variables"] = "(start:{{params.page | offset:ten}})"
    assert_fails(card, "bad offset filter 'offset:ten'")


def test_unknown_namespace(card: dict[str, Any]) -> None:
    action(card, "get_company_people")["steps"][0]["request"]["url"] = "{{env.HOST}}/x"
    assert_fails(card, "unknown namespace 'env'")


def test_forward_step_reference(card: dict[str, Any]) -> None:
    steps = action(card, "get_company_people")["steps"]
    steps[0]["request"]["query"]["universalName"] = "{{steps.people.items}}"
    assert_fails(card, "{{steps.people.items}} is not an earlier step's extract key")


def test_self_step_reference(card: dict[str, Any]) -> None:
    steps = action(card, "get_company_people")["steps"]
    steps[0]["request"]["query"]["universalName"] = "{{steps.company.id}}"
    assert_fails(card, "{{steps.company.id}} is not an earlier step's extract key")


def test_limit_not_equal_page_size(card: dict[str, Any]) -> None:
    action(card, "search_companies")["output"]["limit"] = 25
    assert_fails(card, "search_companies: output.limit (25) must equal next_params.page_size (10)")


def test_next_params_unknown_param(card: dict[str, Any]) -> None:
    action(card, "search_companies")["output"]["next_params"]["param"] = "pg"
    assert_fails(card, "next_params.param 'pg' is not a param")


def test_underscore_param(card: dict[str, Any]) -> None:
    action(card, "search_companies")["params"]["_debug"] = {"type": "boolean", "required": False}
    assert_fails(card, "search_companies.params._debug: param names starting with '_' are reserved")


@pytest.mark.parametrize("signal", ["status:40", "redirect:login", "blocked", "body_contains:"])
def test_bad_blocked_signal(card: dict[str, Any], signal: str) -> None:
    card["blocked_signals"].append(signal)
    assert_fails(card, f"blocked_signals: bad signal {signal!r}")


def test_bad_logged_out_signal(card: dict[str, Any]) -> None:
    card["connect_providers"][0]["logged_out_signals"].append("cookie:li_at")
    assert_fails(card, "connect_providers.linkedin.logged_out_signals: bad signal 'cookie:li_at'")


def test_session_ref_without_auth(card: dict[str, Any]) -> None:
    card["defaults"]["auth_provider"] = None
    assert_fails(card, "{{session.cookies.JSESSIONID | strip_quotes}} used where auth_provider is null")


def test_unknown_auth_provider(card: dict[str, Any]) -> None:
    action(card, "search_companies")["auth_provider"] = "github"
    assert_fails(card, "search_companies: auth_provider 'github' matches no connect_providers[].id")


def test_bad_jsonpath(card: dict[str, Any]) -> None:
    action(card, "search_companies")["output"]["fields"]["name"] = "$.title[["
    assert_fails(card, "search_companies.output.fields.name: bad jsonpath")


def test_bad_regex(card: dict[str, Any]) -> None:
    action(card, "get_company_people")["steps"][0]["extract"]["id"] = {
        "from": "body",
        "type": "regex",
        "expr": "id=(\\d+",
    }
    assert_fails(card, "get_company_people.steps.company.extract.id: bad regex")


def test_read_without_examples(card: dict[str, Any]) -> None:
    action(card, "search_companies")["examples"] = []
    assert_fails(card, "search_companies: read action needs at least one example")


def test_example_missing_required(card: dict[str, Any]) -> None:
    action(card, "search_companies")["examples"][0]["params"] = {"page": 2}
    assert_fails(card, "search_companies.examples[0]: query: required")


def test_duplicate_action_name(card: dict[str, Any]) -> None:
    card["actions"].append(copy.deepcopy(action(card, "search_companies")))
    assert_fails(card, "duplicate action name 'search_companies'")


def test_required_with_default(card: dict[str, Any]) -> None:
    action(card, "search_companies")["params"]["query"]["default"] = "x"
    assert_fails(card, "search_companies.params.query: param is both required and given a default")


def test_bad_tool_prefix(card: dict[str, Any]) -> None:
    card["tool_prefix"] = "Linked-In"
    assert_fails(card, "tool_prefix 'Linked-In' must match")


def test_unknown_field_rejected(card: dict[str, Any]) -> None:
    action(card, "search_companies")["cache_ttl"] = 5
    assert_fails(card, "actions.0.cache_ttl: Extra inputs are not permitted")


def test_write_without_confirmation_is_a_warning(card: dict[str, Any]) -> None:
    action(card, "follow_company")["requires_confirmation"] = False
    report = validate_card(card)
    assert report.ok, report.errors
    assert report.warnings == ["follow_company: write action without requires_confirmation"]


def test_effective_inheritance() -> None:
    card = Card.model_validate(EXAMPLE)
    search = card.action("search_companies")
    assert search is not None
    assert search.effective_auth_provider(card.defaults) == "linkedin"
    assert search.effective_cache_ttl_s(card.defaults) == 300
    assert search.effective_tier(card.defaults) == "browser"
    follow = card.action("follow_company")
    assert follow is not None and follow.write_step_index() == 0


def test_explicit_null_auth_overrides_default() -> None:
    data = copy.deepcopy(EXAMPLE)
    action(data, "follow_company")["auth_provider"] = None
    card = Card.model_validate(data)
    follow = card.action("follow_company")
    assert follow is not None and follow.effective_auth_provider(card.defaults) is None


def test_coerce_params() -> None:
    card = Card.model_validate(EXAMPLE)
    specs = card.actions[0].params
    assert coerce_params(specs, {"query": "x"}) == ({"query": "x", "page": 1}, {})
    _, errs = coerce_params(specs, {"page": "2", "extra": 1, "_x": 1})
    assert errs == {"query": "required", "page": "must be an integer", "extra": "unknown param", "_x": "reserved name"}
    _, errs = coerce_params(specs, {"query": "x", "page": True})
    assert errs == {"page": "must be an integer"}


def test_input_schema() -> None:
    card = Card.model_validate(EXAMPLE)
    schema = input_schema(card.actions[0].params, requires_confirmation=False)
    assert schema["required"] == ["query"]
    assert schema["properties"]["page"] == {"type": "integer", "description": "1-based page number", "default": 1}
    follow = input_schema(card.actions[2].params, requires_confirmation=True)
    assert "_confirmation_id" in follow["properties"]


def test_envelope_shape() -> None:
    env = Envelope(status=ResultCode.SUCCESS, data=[], meta=Meta(ms=1, tier="http", run_id="r_1"))
    dumped = env.model_dump(mode="json")
    assert list(dumped) == ["status", "data", "next_params", "confirmation", "heal", "meta", "error"]
    assert dumped["heal"] is None
    assert len(ResultCode) == 12
