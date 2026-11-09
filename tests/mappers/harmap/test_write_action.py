"""Step 8: the fixture's "Save note" form POST → a write action with a csrf regex extract,
requires_confirmation: true and examples: []. Proven end to end through the runtime's executor."""

import asyncio
import json
import shutil

import pytest

from spider.mappers.harmap.pipeline import map_domain, write_outputs

# characters that break a form body unless they are encoded
TRICKY = "Tom & Jerry = 100% fun +1"


@pytest.fixture(scope="module")
def domain_dir(fixture_captures, tmp_path_factory):
    root = tmp_path_factory.mktemp("step8") / "127.0.0.1"
    for task, runs in {"add_note": ("m1", "m2", "real"), "search": ("m1", "m2", "real", "page2")}.items():
        for run in runs:
            shutil.copytree(fixture_captures(task, run), root / task / run)
    return root


@pytest.fixture(scope="module")
def note(domain_dir):
    return map_domain(domain_dir, task="add_note")


def _action(result, name):
    return next(a for a in result.card["actions"] if a["name"] == name)


@pytest.mark.browser
def test_write_action_card(note):
    assert note.validation.ok, note.validation.errors
    a = _action(note, "add_note")
    assert (a["kind"], a["spends"], a["idempotent"], a["requires_confirmation"]) == (
        "write",
        False,
        False,
        True,
    )
    assert a["examples"] == []
    assert a["output"] == {"from": None, "fields": {}, "limit": None, "next_params": None}
    assert a["params"]["text"]["required"] is True
    assert "ask the user" in a["description"]


@pytest.mark.browser
def test_csrf_regex_extract_from_the_compose_page(note):
    compose, post = _action(note, "add_note")["steps"]
    assert compose["id"] == "compose" and compose["request"]["method"] == "GET"
    assert compose["extract"] == {
        "csrf": {"from": "body", "type": "regex", "expr": 'name="csrf" content="([^"]+)"'}
    }
    assert compose["expect"] == {"status": [200], "content_type": "html", "paths": []}
    assert post["request"]["method"] == "POST"
    assert (
        post["request"]["body"] == "csrf={{steps.compose.csrf | urlencode}}&text={{params.text | urlencode}}"
    )
    assert post["request"]["headers"]["content-type"].startswith("application/x-www-form-urlencoded")
    assert post["expect"]["status"] == [201]


@pytest.mark.browser
def test_report_shows_csrf_dependency_and_text_param(note, tmp_path):
    _, report_path = write_outputs(note, tmp_path)
    goal = report_path.read_text().split("(goal):", 1)[1]
    csrf = next(ln for ln in goal.splitlines() if ln.startswith("| `body.csrf`"))
    text = next(ln for ln in goal.splitlines() if ln.startswith("| `body.text`"))
    assert "dependency" in csrf and "[redacted]" in csrf and "regex" in csrf and "confirmed on m2" in csrf
    assert "param" in text and "`urlencode`" in text


@pytest.mark.browser
def test_note_is_saved_through_the_executor_after_confirmation(note, domain_dir, fixture_captures):
    from spider.card import load_card
    from spider.run.confirm import Confirmer
    from spider.run.executor import Executor
    from spider.store.cards import StoredCard
    from spider.store.sessions import InMemorySessionStore

    card = load_card(note.card)
    cookies = json.loads((domain_dir / "add_note" / "m1" / "state.json").read_text())["cookies"]
    session = {c["name"]: c["value"] for c in cookies}
    store = InMemorySessionStore()
    store.put("default", card.domain, card.defaults.auth_provider, session)
    executor = Executor(store, Confirmer("test-secret"))
    stored = StoredCard(card.domain, 1, card, True, "2026-11-07T00:00:00Z", None)

    def run(args):
        return asyncio.run(executor.run(stored, "add_note", args)).model_dump(mode="json")

    first = run({"text": TRICKY})
    assert first["status"] == "NEEDS_CONFIRMATION"  # nothing sent yet
    second = run({"text": TRICKY, "_confirmation_id": first["confirmation"]["id"]})
    assert second["status"] == "SUCCESS", second

    site = fixture_captures.site
    sess = site.state.sessions[session["sid"]]
    assert sess.notes[-1]["text"] == TRICKY  # arrived exactly, special characters intact


@pytest.mark.browser
def test_whole_site_card_has_both_actions(domain_dir):
    both = map_domain(domain_dir, live=True)
    assert both.validation.ok, both.validation.errors
    assert sorted(a["name"] for a in both.card["actions"]) == ["add_note", "search"]
    assert [r.action for r in both.validation.live] == ["search"]  # writes never run live
    assert all(r.ok for r in both.validation.live)
    assert _action(both, "search")["output"]["next_params"]["until"] == "steps.search.total"
