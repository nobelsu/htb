"""Step 6: a full card for the fixture site from real captures (m1, m2, real) + mapping_report.md."""

import json
import subprocess
import sys

import pytest

from mapper.pipeline import map_domain, write_outputs


@pytest.fixture(scope="module")
def domain_dir(fixture_captures):
    for run in ("m1", "m2", "real"):
        fixture_captures("search", run)
    return fixture_captures.root / "127.0.0.1"


@pytest.fixture(scope="module")
def mapped(domain_dir):
    return map_domain(domain_dir, task="search", live=True)


@pytest.mark.browser
def test_card_passes_every_save_time_check(mapped):
    v = mapped.validation
    assert v.ok, v.errors
    assert v.ran == ["schema.validate_card", "tools/check_card.py", "mapper.checks (secrets)"]
    assert not v.warnings


@pytest.mark.browser
def test_card_runs_live_through_the_executor(mapped):
    [live] = mapped.validation.live
    assert live.status == "SUCCESS" and live.items == 10, live


@pytest.mark.browser
def test_card_site_fields(mapped, fixture_captures):
    card = mapped.card
    assert card["schema_version"] == "0.4"
    assert (card["domain"], card["tool_prefix"]) == ("127.0.0.1", "127_0_0_1")
    assert card["base_url"] == fixture_captures.site.url
    assert card["blocked_signals"] == ["status:403"]
    assert card["provenance"]["source"] == "network_capture"
    assert card["defaults"]["auth_provider"] == "127_0_0_1"
    [provider] = card["connect_providers"]
    assert provider["id"] == "127_0_0_1"
    assert provider["session_check"]["url"] == "{{base_url}}/api/session"  # placeholder, see Open items
    assert any("HIGH PRIORITY (Nobel)" in n for n in mapped.notes)


@pytest.mark.browser
def test_card_action(mapped):
    [action] = mapped.card["actions"]
    assert action["name"] == "search" and action["kind"] == "read"
    assert (action["spends"], action["idempotent"], action["requires_confirmation"]) == (False, True, False)
    assert action["params"]["query"]["required"] is True
    assert action["examples"] == [{"params": {"query": "software"}, "min_items": 1}]
    assert [s["id"] for s in action["steps"]] == ["session", "search"]
    session, search = action["steps"]
    assert session["expect"] == {"status": [200], "content_type": "json", "paths": ["$.token"]}
    assert search["expect"] == {"status": [200], "content_type": "json", "paths": ["$.elements"]}
    assert search["extract"]["items"]["expr"] == "$.elements[*]"
    assert action["output"] == {
        "from": "steps.search.items",
        "fields": {
            "id": "$.id",
            "name": "$.name",
            "industry": "$.industry",
            "url": "$.url",
            "followers": "$.followers",
            "logo_url": "$.logo.url",
            "logo_width": "$.logo.width",
        },
        "limit": 10,
        "next_params": None,
    }


@pytest.mark.browser
def test_report_shows_every_goal_field_with_provenance(mapped, tmp_path):
    card_path, report_path = write_outputs(mapped, tmp_path)
    assert json.loads(card_path.read_text()) == mapped.card
    report = report_path.read_text()
    rows = {
        "`query.q` | param": "`{{params.query}}`",
        "`query.start` | constant": "`0`",
        "`header.accept` | constant": "`application/json`",
        "`header.x-token` | dependency": "`{{steps.session.token}}`",
        "`header.csrf-token` | session_cookie": "`{{session.cookies.JSESSIONID \\| strip_quotes}}`",
        "`cookie.sid` | session_cookie": "(cookie jar)",
    }
    goal_section = report.split("(goal):", 1)[1].split("### Output", 1)[0]
    for start, template in rows.items():
        line = next((ln for ln in goal_section.splitlines() if ln.startswith(f"| {start}")), None)
        assert line is not None and template in line, (start, line)
    assert "confirmed on m2" in report
    assert "HIGH PRIORITY (Nobel)" in report
    assert "**VALID**" in report and "SUCCESS, 10 item(s)" in report


@pytest.mark.browser
def test_no_live_session_value_in_card_or_report(mapped, tmp_path, domain_dir):
    card_path, report_path = write_outputs(mapped, tmp_path)
    text = card_path.read_text() + report_path.read_text()
    state = json.loads((domain_dir / "search" / "m1" / "state.json").read_text())
    for c in state["cookies"]:
        assert c["value"] not in text and c["value"].strip('"') not in text, c["name"]
    assert mapped.secrets and not any(s in text for s in mapped.secrets)


@pytest.mark.browser
def test_cli_map_writes_card_and_saves_to_registry(domain_dir, tmp_path):
    db = tmp_path / "runtime.db"
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "mapper",
            "map",
            str(domain_dir),
            "--task",
            "search",
            "--out",
            str(tmp_path),
            "--save",
            "--db",
            str(db),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env={**__import__("os").environ, "MAPPER_NO_LLM": "1"},
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "VALID" in out.stdout and "saved 127.0.0.1 v1" in out.stdout
    from runtime.registry import Registry

    reg = Registry(db)
    try:
        stored = reg.get("127.0.0.1")
        assert stored is not None and stored.card.action("search") is not None
    finally:
        reg.close()
    v = subprocess.run(
        [sys.executable, "-m", "mapper", "validate", str(tmp_path / "card.json")],
        capture_output=True,
        text=True,
    )
    assert v.returncode == 0 and "VALID" in v.stdout
