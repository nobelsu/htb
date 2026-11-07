"""Explorer: an LLM (scripted here) picks the tasks and does each one live, step by step; replays follow."""

import json
from types import SimpleNamespace

import pytest

from mapper.capture.driver import AFTER_MARK_MS
from mapper.explore.agent import accessibility_tree, drive
from mapper.explore.explorer import EXPLORE_FILE, explore
from mapper.explore.planner import PlanError, Planner, TaskSpec
from mapper.llm import LLM
from mapper.pipeline import map_domain


def act(action, role=None, name=None, text=None, key=None, url=None, performs=False):
    return {
        "action": action,
        "role": role,
        "name": name,
        "text": text,
        "key": key,
        "url": url,
        "performs_task": performs,
        "reason": "test",
    }


SEARCH_TASK = {
    "name": "search_companies",
    "description": "Search companies by keyword",
    "kind": "read",
    "start_path": "/search",
    "inputs": [{"name": "query", "example": "software"}],
    "paginates": True,
}
NOTE_TASK = {
    "name": "add_note",
    "description": "Save a note",
    "kind": "write",
    "start_path": "/compose",
    "inputs": [{"name": "text", "example": "Call Acme about the Q3 renewal"}],
    "paginates": False,
}
IMPOSSIBLE_TASK = {
    "name": "view_notifications",
    "description": "View notifications",
    "kind": "read",
    "start_path": "/",
    "inputs": [],
    "paginates": False,
}
GOOD_SEARCH = [
    act("type", "textbox", "Search companies", text="software"),
    act("click", "button", "Search", performs=True),
    act("done"),
]
GOOD_NOTE = [
    act("type", "textbox", "Write a note", text="Call Acme about the Q3 renewal"),
    act("click", "button", "Save note", performs=True),
    act("done"),
]
OK = {"ok": True, "reason": "results are shown", "next_role": None, "next_name": None}


class Brain:
    """Stands in for the LLM: answers each call by which schema it asks for, from per-task queues."""

    def __init__(self, tasks, actions, verdicts):
        self.tasks = tasks
        self.actions = {k: list(v) for k, v in actions.items()}  # task description -> next actions
        self.verdicts = {k: list(v) for k, v in verdicts.items()}  # task description -> check answers
        self.prompts: list[tuple[str, str]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    @staticmethod
    def _task(queues, prompt):
        return next(d for d in queues if f"Task: {d} (" in prompt)

    def create(self, **kw):
        schema = kw["output_config"]["format"]["schema"]["properties"]
        prompt = kw["messages"][0]["content"]
        if "tasks" in schema:
            kind, payload = "discover", {"tasks": self.tasks}
        elif "action" in schema:
            kind, payload = "action", self.actions[self._task(self.actions, prompt)].pop(0)
        else:
            kind, payload = "check", self.verdicts[self._task(self.verdicts, prompt)].pop(0)
        self.prompts.append((kind, prompt))
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(payload))]
        )


def _llm(brain):
    return LLM(client=brain, enabled=True)


# --- planner and driver checks (no browser) -----------------------------------------------------


def test_task_specs_are_sanitised():
    spec = Planner._spec({**SEARCH_TASK, "name": "Search Companies!", "start_path": "search"})
    assert (spec.name, spec.start_path, spec.inputs) == ("search_companies", "/search", {"query": "software"})
    with pytest.raises(PlanError, match="bad input name"):
        Planner._spec({**SEARCH_TASK, "inputs": [{"name": "page", "example": "2"}]})  # reserved for paging


def test_explorer_needs_an_llm():
    with pytest.raises(PlanError, match="needs an LLM"):
        Planner(LLM(enabled=False)).discover("site")


def test_typed_field_contents_never_reach_the_llm():
    tree = "\n".join(
        [
            '- textbox "Search companies": my secret query',
            "- searchbox: hello",
            '- button "Search"',
            '- textbox "Empty"',
        ]
    )
    page = SimpleNamespace(locator=lambda sel: SimpleNamespace(aria_snapshot=lambda: tree))
    assert (
        accessibility_tree(page)
        == '- textbox "Search companies"\n- searchbox\n- button "Search"\n- textbox "Empty"'
    )


class FakeElement:
    def __init__(self, calls, name):
        self.calls, self.name = calls, name

    def count(self):
        return 1

    def nth(self, i):
        return self

    def is_visible(self):
        return True

    def click(self):
        self.calls.append(("click", self.name))

    def fill(self, text):
        self.calls.append(("fill", text))


def test_the_mark_comes_the_instant_the_flagged_action_is_done():
    calls = []
    page = SimpleNamespace(
        url="https://example.com/search",
        locator=lambda sel: SimpleNamespace(aria_snapshot=lambda: '- textbox "q"\n- button "Search"'),
        get_by_role=lambda role, name=None, exact=True: FakeElement(calls, name),
    )
    tracker = SimpleNamespace(wait_quiet=lambda page, timeout_ms=10_000: calls.append(("quiet", timeout_ms)))
    spec = TaskSpec("search", "Search companies by keyword", "read", "/search", {"query": "software"})
    brain = Brain([], {spec.description: GOOD_SEARCH}, {spec.description: [OK]})
    run = drive(
        page,
        tracker,
        lambda: calls.append(("MARK",)),
        planner=Planner(_llm(brain)),
        spec=spec,
        start_url="https://example.com/search",
        feedback=[],
    )
    assert run.marked and [a["outcome"] for a in run.actions] == ["ok", "ok, goal marked"]
    i = calls.index(("click", "Search"))
    assert calls[i + 1 :] == [("MARK",), ("quiet", AFTER_MARK_MS)]  # no wait, no LLM call in between


# --- the fixture site, end to end -------------------------------------------------------------


@pytest.fixture
def logged_in(site, tmp_path):
    state = tmp_path / "login.state.json"
    site.login_state(state)
    return state


@pytest.mark.browser
def test_explore_drives_live_retries_and_replays(chromium, site, logged_in, tmp_path):
    never_typed = [act("click", "button", "Search", performs=True), act("done")]
    wrong_then_right = [
        act("click", "link", "Log out"),  # refused: it would end the session
        act("click", "button", "Advanced search"),  # does not exist: Claude is told and goes on
        *GOOD_SEARCH,  # attempt 2: the final check says no
        *GOOD_SEARCH,  # attempt 3: works
    ]
    wrong = {
        "ok": False,
        "reason": "only shows all results, not filtered",
        "next_role": None,
        "next_name": None,
    }
    brain = Brain(
        [SEARCH_TASK, NOTE_TASK, IMPOSSIBLE_TASK],
        {
            "Search companies by keyword": never_typed + wrong_then_right,
            "View notifications": [act("give_up")] * 3,
        },
        {
            "Search companies by keyword": [
                wrong,
                {**OK, "next_role": "button", "next_name": "Next page"},
            ],
        },
    )
    result = explore(
        site.url + "/",
        state=logged_in,
        out=tmp_path / "captures",
        llm=_llm(brain),
        step_timeout_ms=1500,
        max_attempts=3,
    )
    by_name = {o.plan.spec.name: o for o in result.tasks}
    search, note, bell = by_name["search_companies"], by_name["add_note"], by_name["view_notifications"]

    # three live attempts, each failure fed back; the third one recorded, then replayed for the others
    assert search.status == "recorded", search.reason
    errors = [a.error for a in search.plan.attempts]
    assert "never typed the input" in errors[0] and "does not show the task's result" in errors[1]
    assert errors[2] is None
    outcomes = [a["outcome"] for a in search.plan.attempts[1].actions]
    assert "not allowed" in outcomes[0] and "no visible button" in outcomes[1]
    last_action_prompt = [p for k, p in brain.prompts if k == "action" and "Search companies" in p][-1]
    assert "attempt 1: never typed" in last_action_prompt and "attempt 2:" in last_action_prompt
    assert search.plan.next_selector == "#next"
    assert search.runs == ["real", "m1", "m2", "page2"]
    assert search.plan.script[0] == {"type": "input", "selector": "#q", "value": "{{params.query}}"}
    assert {"type": "mark"} in search.plan.script
    meta = json.loads((result.domain_dir / "search_companies" / "real" / "meta.json").read_text())
    assert meta["driver"] == "agent" and len(meta["goal_marks"]) == 1
    # writes are not explored by default; an impossible task fails cleanly and leaves no recordings
    assert note.status == "skipped" and "--include-writes" in note.reason
    assert bell.status == "failed" and len(bell.plan.attempts) == 3 and "gave up" in bell.reason
    assert not (result.domain_dir / "view_notifications").exists()

    # nothing clicked "Log out", and nothing secret or typed went into a prompt's page view
    assert ("GET", "/logout") not in site.state.requests
    cookies = json.loads(logged_in.read_text())["cookies"]
    every_prompt = "\n".join(p for _, p in brain.prompts)
    for c in cookies:
        assert c["value"].strip('"') not in every_prompt
    assert '- textbox "Search companies": software' not in every_prompt

    saved = json.loads((result.domain_dir / EXPLORE_FILE).read_text())
    assert [t["status"] for t in saved["tasks"]] == ["recorded", "skipped", "failed"]
    assert len(saved["tasks"][0]["attempts"]) == 3

    mapped = map_domain(result.domain_dir, live=True)
    assert mapped.validation.ok, mapped.validation.errors
    [action] = mapped.card["actions"]
    assert action["name"] == "search_companies"
    assert action["output"]["next_params"]["until"] == "steps.search.total"
    assert mapped.validation.live[0].status == "SUCCESS"

    from mapper.pipeline import write_outputs

    _, report_path = write_outputs(mapped, tmp_path / "out")
    report = report_path.read_text()
    assert "## Explorer (tasks chosen by the LLM)" in report
    assert "| `add_note` | write | skipped |" in report


@pytest.mark.browser
def test_explore_writes_when_asked(chromium, site, logged_in, tmp_path):
    brain = Brain([NOTE_TASK], {"Save a note": GOOD_NOTE}, {"Save a note": [OK]})
    result = explore(
        site.url + "/",
        state=logged_in,
        out=tmp_path / "captures",
        wanted=["save a note"],
        include_writes=True,
        llm=_llm(brain),
        step_timeout_ms=1500,
    )
    [note] = result.tasks
    assert note.status == "recorded", note.reason
    assert note.runs == ["real", "m1", "m2"]  # not paginated: no page2
    assert "save a note" in brain.prompts[0][1]  # the user's wording reached task discovery
    mapped = map_domain(result.domain_dir)
    assert mapped.validation.ok, mapped.validation.errors
    action = mapped.card["actions"][0]
    assert (action["kind"], action["requires_confirmation"]) == ("write", True)
    assert action["steps"][-1]["request"]["body"].startswith("csrf={{steps.compose.csrf | urlencode}}")
