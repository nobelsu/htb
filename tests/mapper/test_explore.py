"""Explorer: an LLM (scripted here) picks the tasks and writes scripts; the browser runs them."""

import json
from types import SimpleNamespace

import pytest

from mapper.explore.explorer import EXPLORE_FILE, explore
from mapper.explore.planner import PlanError, Planner, TaskSpec
from mapper.llm import LLM
from mapper.pipeline import map_domain


def _step(type_, selector=None, value=None, url=None, key=None):
    return {"type": type_, "url": url, "selector": selector, "value": value, "key": key}


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
    _step("input", "#q", "{{params.query}}"),
    _step("click", 'button[type="submit"]'),
    _step("wait_for", "#summary:not(:empty)"),
    _step("mark"),
]
WRONG_SEARCH = [_step("input", "#query", "{{params.query}}"), _step("click", "button"), _step("mark")]
GOOD_NOTE = [
    _step("input", "#text", "{{params.text}}"),
    _step("click", 'button[type="submit"]'),
    _step("wait_for", "#status:not(:empty)"),
    _step("mark"),
]


class Brain:
    """Stands in for the LLM: answers each call by which schema it asks for."""

    def __init__(self, tasks, scripts, next_selector="#next"):
        self.tasks, self.scripts, self.next_selector = (
            tasks,
            {k: list(v) for k, v in scripts.items()},
            next_selector,
        )
        self.prompts: list[tuple[str, str]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        schema = kw["output_config"]["format"]["schema"]["properties"]
        prompt = kw["messages"][0]["content"]
        if "tasks" in schema:
            kind, payload = "discover", {"tasks": self.tasks}
        elif "steps" in schema:
            kind = "script"
            name = next(n for n in self.scripts if f"name {n!r}" in prompt)
            queue = self.scripts[name]
            payload = {"steps": queue.pop(0) if len(queue) > 1 else queue[0], "notes": ""}
        else:
            kind, payload = "next", {"selector": self.next_selector, "reason": "next page button"}
        self.prompts.append((kind, prompt))
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(payload))]
        )


def _llm(brain):
    return LLM(client=brain, enabled=True)


# --- planner checks (no browser) ----------------------------------------------------------------

TASK = TaskSpec("search", "Search", "read", "/search", {"query": "software"})


def test_script_checks():
    ok = Planner.check_script(TASK, GOOD_SEARCH)
    assert ok[0] == {"type": "input", "selector": "#q", "value": "{{params.query}}"}  # nulls dropped
    for bad, why in [
        ([_step("input", "#q", "{{params.query}}")], "no goal mark"),
        ([_step("input", "#q", "hello"), _step("mark")], "never types the inputs"),
        ([_step("click"), _step("mark")], "no selector"),
        ([_step("teleport"), _step("mark")], "unknown type"),
    ]:
        with pytest.raises(PlanError, match=why):
            Planner.check_script(TASK, bad)


def test_task_specs_are_sanitised():
    spec = Planner._spec({**SEARCH_TASK, "name": "Search Companies!", "start_path": "search"})
    assert (spec.name, spec.start_path, spec.inputs) == ("search_companies", "/search", {"query": "software"})
    with pytest.raises(PlanError, match="bad input name"):
        Planner._spec({**SEARCH_TASK, "inputs": [{"name": "page", "example": "2"}]})  # reserved for paging


def test_explorer_needs_an_llm():
    with pytest.raises(PlanError, match="needs an LLM"):
        Planner(LLM(enabled=False)).discover("site")


# --- the fixture site, end to end -------------------------------------------------------------


@pytest.fixture
def logged_in(site, tmp_path):
    state = tmp_path / "login.state.json"
    site.login_state(state)
    return state


@pytest.mark.browser
def test_explore_picks_tasks_fixes_a_bad_script_and_maps_a_card(chromium, site, logged_in, tmp_path):
    brain = Brain(
        [SEARCH_TASK, NOTE_TASK, IMPOSSIBLE_TASK],
        {
            "search_companies": [WRONG_SEARCH, GOOD_SEARCH],
            "view_notifications": [[_step("click", "#bell"), _step("mark")]],
        },
    )
    result = explore(
        site.url + "/",
        state=logged_in,
        out=tmp_path / "captures",
        llm=_llm(brain),
        step_timeout_ms=1500,
        max_attempts=2,
    )
    by_name = {o.plan.spec.name: o for o in result.tasks}
    search, note, bell = by_name["search_companies"], by_name["add_note"], by_name["view_notifications"]

    # the wrong selector failed in the dry run, the error + page went back to the LLM, attempt 2 worked
    assert search.status == "recorded" and len(search.plan.attempts) == 2
    assert "#query" in search.plan.attempts[0].error or "Timeout" in search.plan.attempts[0].error
    second_prompt = [p for k, p in brain.prompts if k == "script" and "search_companies" in p][1]
    assert "attempt 1" in second_prompt and "Fields:" in second_prompt
    assert search.plan.next_selector == "#next" and search.runs == ["m1", "m2", "real", "page2"]
    # writes are not explored by default; an impossible task fails cleanly
    assert note.status == "skipped" and "--include-writes" in note.reason
    assert bell.status == "failed" and len(bell.plan.attempts) == 2

    # the survey never clicked "Log out", and nothing secret went into a prompt
    assert ("GET", "/logout") not in site.state.requests
    cookies = json.loads(logged_in.read_text())["cookies"]
    every_prompt = "\n".join(p for _, p in brain.prompts)
    for c in cookies:
        assert c["value"].strip('"') not in every_prompt

    saved = json.loads((result.domain_dir / EXPLORE_FILE).read_text())
    assert [t["status"] for t in saved["tasks"]] == ["recorded", "skipped", "failed"]

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
    brain = Brain([NOTE_TASK], {"add_note": [GOOD_NOTE]})
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
    assert "save a note" in brain.prompts[0][1]  # the user's wording reached task discovery
    mapped = map_domain(result.domain_dir)
    assert mapped.validation.ok, mapped.validation.errors
    action = mapped.card["actions"][0]
    assert (action["kind"], action["requires_confirmation"]) == ("write", True)
    assert action["steps"][-1]["request"]["body"].startswith("csrf={{steps.compose.csrf | urlencode}}")
