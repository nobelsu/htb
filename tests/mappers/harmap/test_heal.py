"""Step 9: heal = replay every recording in a fresh browser, re-map the whole site, save the new card."""

import json
import shutil
import subprocess
import sys

import pytest
from synthetic import ts

from spider.mappers.harmap.card.validate import RuntimeExecutor
from spider.mappers.harmap.pipeline import map_domain, save_card
from spider.mappers.harmap.record.replay import new_marker, script_from_actions

START = "http://127.0.0.1:9/"


def _meta(**extra):
    return {
        "start_url": START,
        "markers": {"query": "zqxv8841"},
        "real_inputs": {},
        "goal_marks": [],
        **extra,
    }


def test_script_from_actions_rebuilds_the_run():
    actions = [
        {"t": ts(0.1), "type": "navigate", "url": START},
        {"t": ts(0.5), "type": "navigate", "url": "http://127.0.0.1:9/search"},
        {"t": ts(1.0), "type": "input", "selector": "#q", "value": "zqxv8841", "url": "x"},
        {"t": ts(1.2), "type": "click", "selector": "button", "text": "Search", "url": "x"},
        {"t": ts(1.4), "type": "navigate", "url": "http://127.0.0.1:9/results?q=zqxv8841"},  # caused by click
        {"t": ts(3.0), "type": "key", "key": "Enter", "selector": "#q", "url": "x"},
    ]
    steps, warnings = script_from_actions(actions, _meta(goal_marks=[ts(2.0), ts(4.0)]))
    assert steps == [
        {"type": "navigate", "url": "/search"},
        {"type": "input", "selector": "#q", "value": "{{params.query}}"},
        {"type": "click", "selector": "button"},
        {"type": "mark"},
        {"type": "key", "selector": "#q", "key": "Enter"},
        {"type": "mark"},
    ]
    assert warnings == []


def test_script_skips_redacted_secrets_and_keeps_partial_values():
    actions = [
        {"t": ts(1), "type": "input", "selector": "input[name=password]", "value": "[redacted]", "url": "x"},
        {"t": ts(2), "type": "input", "selector": "#q", "value": "about zqxv8841!", "url": "x"},
    ]
    steps, warnings = script_from_actions(actions, _meta())
    assert steps == [{"type": "input", "selector": "#q", "value": "about {{params.query}}!"}]
    assert "secret input" in warnings[0]


def test_new_markers_are_fresh():
    a, b = new_marker(), new_marker()
    assert a != b and len(a) == 8 and a[:4].isalpha() and a[4:].isdigit()


# --- break the site, then heal it -----------------------------------------------------------


@pytest.fixture
def site_copy(fixture_captures, tmp_path):
    root = tmp_path / "captures" / "127.0.0.1"
    runs = {"search": ("m1", "m2", "real", "page2"), "add_note": ("m1", "m2", "real")}
    for task, task_runs in runs.items():
        for run in task_runs:
            shutil.copytree(fixture_captures(task, run), root / task / run)
    yield root
    fixture_captures.site.move_search(False)  # the site is shared by the whole test session


def _search(card, cookies, page=1):
    return RuntimeExecutor().run_action(
        card, "search", {"query": "software", "page": page}, session_cookies=cookies
    )


@pytest.mark.browser
def test_site_change_breaks_the_card_and_heal_fixes_it(site_copy, fixture_captures, tmp_path):
    db = tmp_path / "runtime.db"
    before = map_domain(site_copy)
    assert before.validation.ok
    assert save_card(before.card, db, note="initial map") == 1
    cookies = before.tasks[0].runs["m1"].state_cookies
    old_markers = json.loads((site_copy / "search" / "m1" / "meta.json").read_text())["markers"]
    assert _search(before.card, cookies)["status"] == "SUCCESS"

    fixture_captures.site.move_search(True)  # "site redesign": search API moves to /api/v2/search
    assert _search(before.card, cookies)["status"] == "PAGE_CHANGED"

    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "spider.mappers.harmap",
            "heal",
            str(site_copy),
            "--live",
            "--save",
            "--db",
            str(db),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env={**__import__("os").environ, "MAPPER_NO_LLM": "1"},
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "VALID" in out.stdout and "saved 127.0.0.1 v2" in out.stdout

    healed = json.loads((site_copy / "card.json").read_text())
    search = next(a for a in healed["actions"] if a["name"] == "search")
    assert search["steps"][-1]["request"]["url"] == "{{base_url}}/api/v2/search"
    assert search["output"]["next_params"]["until"] == "steps.search.total"  # pagination survived
    assert {a["name"] for a in healed["actions"]} == {"search", "add_note"}  # whole site re-mapped
    pages = [len(_search(healed, cookies, p)["data"]) for p in (1, 2, 3)]
    assert pages == [10, 10, 3]

    new_markers = json.loads((site_copy / "search" / "m1" / "meta.json").read_text())["markers"]
    assert new_markers.keys() == old_markers.keys() and new_markers != old_markers  # fresh markers
    assert (site_copy.parent / "127.0.0.1.prev" / "search" / "m1" / "meta.json").exists()  # old kept

    from spider.store.cards import Registry

    reg = Registry(db)
    try:
        active = reg.get("127.0.0.1")
        assert active.version == 2 and active.active
        assert active.card.action("add_note") is not None  # write action replaced too, no hold-back
    finally:
        reg.close()
