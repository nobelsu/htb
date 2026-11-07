"""Static validation (schema + check_card + secrets) and the live-validation hook."""

import copy
import json
from pathlib import Path

from mapper import checks
from mapper.validate import live_validate, run_check_card, static_validate

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = json.loads((ROOT / "action_map.example.json").read_text())


def test_spec_example_passes_every_checker():
    v = static_validate(EXAMPLE)
    assert v.ok, v.errors
    assert v.ran[:2] == ["schema.validate_card", "tools/check_card.py"]


def test_invalid_card_reports_schema_and_check_card_errors():
    card = copy.deepcopy(EXAMPLE)
    card["actions"][0]["steps"][0]["request"]["query"]["variables"] = "{{params.nope | shout}}"
    v = static_validate(card)
    assert not v.ok
    assert any(e.startswith("schema:") for e in v.errors)
    assert any(e.startswith("check_card:") for e in v.errors)
    assert run_check_card(EXAMPLE) == []


def test_secret_leak_is_an_error():
    secrets = checks.secret_values({"li_at": "AQEDAT-live-session-123"}, [("csrf-token", "ajax:9876543210")])
    card = copy.deepcopy(EXAMPLE)
    card["defaults"]["headers"]["x-li"] = "AQEDAT-live-session-123"
    v = static_validate(card, secrets)
    assert any("live session value" in e for e in v.errors)
    assert static_validate(EXAMPLE, secrets).ok


def test_secret_values_ignore_short_and_non_sensitive():
    s = checks.secret_values({"a": '"x"', "JSESSIONID": '"ajax:12345678"'}, [("accept", "application/json")])
    assert s == {'"ajax:12345678"', "ajax:12345678"}


def test_secret_values_skip_settings_and_the_host_header():
    """Seen on LinkedIn: the `timezone` cookie is echoed in a tracking header, and HTTP/2's `:authority`
    (the host name) matched "auth"; both failed real cards as "live session values"."""
    s = checks.secret_values(
        {"timezone": "Europe/London", "tz2": "America/Argentina/Buenos_Aires", "li_at": "AQEDATxyzLmnQ"},
        [(":authority", "www.linkedin.com"), ("csrf-token", "ajax:9876543210")],
    )
    assert s == {"AQEDATxyzLmnQ", "ajax:9876543210"}
    random_letters = "kQzPLmnBvcXa"  # no digits, but not words: still a secret
    assert checks.secret_values({"token": random_letters}, []) == {random_letters}


class FakeExecutor:
    def __init__(self, envelopes):
        self.envelopes, self.calls = envelopes, []

    def run_action(self, card, action, params, *, session_cookies=None):
        self.calls.append((action, params, session_cookies))
        return self.envelopes[action]


def test_live_validation_runs_read_examples_only():
    fake = FakeExecutor(
        {
            "search_companies": {"status": "SUCCESS", "data": [{"name": "Palantir"}], "error": None},
            "get_company_people": {"status": "EMPTY", "data": [], "error": None},
        }
    )
    results = live_validate(EXAMPLE, fake, {"li_at": "x"})
    assert [(r.action, r.ok) for r in results] == [("search_companies", True), ("get_company_people", False)]
    assert all(c[0] != "follow_company" for c in fake.calls)  # writes never run live
    assert fake.calls[0][2] == {"li_at": "x"}
