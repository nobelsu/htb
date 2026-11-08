"""Explorer: an LLM (scripted here) picks the tasks and does each one live, step by step; replays follow."""

import json
import re
from types import SimpleNamespace

import pytest

from mapper.capture.driver import AFTER_MARK_MS
from mapper.explore.agent import accessibility_tree, drive
from mapper.explore.explorer import EXPLORE_FILE, explore
from mapper.explore.planner import PlanError, Planner, TaskSpec
from mapper.llm import LLM
from mapper.pipeline import map_domain


def act(action, role=None, name=None, text=None, key=None, url=None, performs=False, picks=None):
    return {
        "action": action,
        "role": role,
        "name": name,
        "text": text,
        "key": key,
        "url": url,
        "performs_task": performs,
        "picks": picks,
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


def typed(name, role, label):
    """Type whatever the prompt says to type for ``name`` (writes get made-up words from the explorer)."""

    def answer(prompt):
        value = re.search(rf"{name}: type exactly '([^']+)'", prompt).group(1)
        return act("type", role, label, text=value)

    return answer


GOOD_NOTE = [
    typed("text", "textbox", "Write a note"),
    act("click", "button", "Save note", performs=True),
    act("done"),
]
OK = {"ok": True, "empty": False, "reason": "results are shown", "next_role": None, "next_name": None}


class Brain:
    """Stands in for the LLM: answers each call by which schema it asks for, from per-task queues."""

    def __init__(self, tasks, actions, verdicts, examples=None, listed=None):
        self.tasks = tasks
        self.listed = listed or []  # answer to "plan these listed tasks"
        self.examples = list(examples or [])  # answers to "pick a new example"
        self.actions = {k: list(v) for k, v in actions.items()}  # task description -> next actions
        self.verdicts = {k: list(v) for k, v in verdicts.items()}  # task description -> check answers
        self.prompts: list[tuple[str, str]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    @staticmethod
    def _task(queues, prompt):
        return next(d for d in queues if f"Task: {d} (" in prompt or f"Task: {d}." in prompt)

    def create(self, **kw):
        schema = kw["output_config"]["format"]["schema"]["properties"]
        prompt = kw["messages"][0]["content"]
        if "tasks" in schema and "A person listed the actions" in prompt:
            kind, payload = "plan", {"tasks": self.listed}
        elif "tasks" in schema:
            kind, payload = "discover", {"tasks": self.tasks}
        elif "action" in schema:
            kind, payload = "action", self.actions[self._task(self.actions, prompt)].pop(0)
            if callable(payload):  # an action that depends on the prompt (the made-up words to type)
                payload = payload(prompt)
        elif "inputs" in schema:
            kind, payload = "example", {"inputs": self.examples.pop(0)}
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
    tracker = SimpleNamespace(
        action=lambda: None, wait_for_action=lambda page, ms: calls.append(("wait", ms))
    )
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
    assert calls[i + 1 :] == [("MARK",), ("wait", AFTER_MARK_MS)]  # no wait, no LLM call in between


def test_inputs_in_the_address_are_swapped_in_replays():
    """get_company(universal_name): the input is part of the address; replays put the made-up word there."""
    from mapper.capture.replay import script_from_actions

    actions = [
        {"t": "2026-11-08T10:00:00.000Z", "type": "navigate", "url": "https://example.com/feed/"},
        {
            "t": "2026-11-08T10:00:03.000Z",
            "type": "navigate",
            "url": "https://example.com/company/acme%20labs/",
        },
    ]
    meta = {"start_url": "https://example.com/feed/", "real_inputs": {"universal_name": "acme labs"}}
    steps, _ = script_from_actions(actions, meta)
    assert steps == [{"type": "navigate", "url": "/company/{{params.universal_name}}/"}]


def test_the_same_failing_action_three_times_ends_the_try():
    from mapper.explore.agent import AgentFailed

    nothing = SimpleNamespace(count=lambda: 0)
    page = SimpleNamespace(
        url="https://example.com/search",
        locator=lambda sel: SimpleNamespace(aria_snapshot=lambda: '- checkbox "Companies"'),
        get_by_role=lambda role, name=None, exact=True: nothing,
    )
    tracker = SimpleNamespace(action=lambda: None, wait_for_action=lambda page, timeout_ms: None)
    spec = TaskSpec("search", "Search companies by keyword", "read", "/search", {})
    brain = Brain([], {spec.description: [act("click", "checkbox", "Companies")] * 5}, {})
    with pytest.raises(AgentFailed, match="same failing action 3 times") as e:
        drive(
            page,
            tracker,
            lambda: None,
            planner=Planner(_llm(brain)),
            spec=spec,
            start_url=page.url,
            feedback=[],
        )
    outcomes = [a["outcome"] for a in e.value.run.actions]
    assert len(outcomes) == 3 and "failed twice" in outcomes[1]


@pytest.mark.browser
def test_a_hidden_checkbox_is_clicked_through_its_label(chromium):
    from playwright.sync_api import sync_playwright

    from mapper.explore.agent import _find

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                '<label class="pill" style="padding:6px;border:1px solid">'
                '<input type="checkbox" aria-label="Filter by Companies" '
                'style="position:absolute;width:0;height:0;opacity:0;margin:0">Companies</label>'
            )
            _find(page, "checkbox", "Filter by Companies", for_click=True).click()
            assert page.is_checked("input")  # clicking the visible pill ticked the hidden box
        finally:
            browser.close()


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
    wrong = {**OK, "ok": False, "reason": "only shows all results, not filtered"}
    brain = Brain(
        [SEARCH_TASK, NOTE_TASK, IMPOSSIBLE_TASK],
        {
            "Search companies by keyword": never_typed + wrong_then_right,
            "View notifications": [act("give_up")] * 3,
            "Save a note": GOOD_NOTE,
        },
        {
            "Search companies by keyword": [
                wrong,
                {**OK, "next_role": "button", "next_name": "Next page"},
            ],
            "Save a note": [OK],
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
    assert "never gave the input" in errors[0] and "does not show the task's result" in errors[1]
    assert errors[2] is None
    outcomes = [a["outcome"] for a in search.plan.attempts[1].actions]
    assert "not allowed" in outcomes[0] and "no visible button" in outcomes[1]
    last_action_prompt = [p for k, p in brain.prompts if k == "action" and "Search companies" in p][-1]
    assert "try 1: never gave" in last_action_prompt and "try 2:" in last_action_prompt
    assert search.plan.next_selector == "#next"
    assert search.runs == ["real", "m1", "m2", "page2"]
    assert search.plan.script[0] == {"type": "input", "selector": "#q", "value": "{{params.query}}"}
    assert {"type": "mark"} in search.plan.script
    meta = json.loads((result.domain_dir / "search_companies" / "real" / "meta.json").read_text())
    assert meta["driver"] == "agent" and len(meta["goal_marks"]) == 1
    # writes are not explored by default; an impossible task fails cleanly and leaves no recordings
    # writes are explored too, with the final request stopped: nothing reached the site
    assert note.status == "recorded" and note.runs == ["m1", "m2"], note.reason
    assert ("POST", "/api/notes") not in site.state.requests
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
    assert [t["status"] for t in saved["tasks"]] == ["recorded", "recorded", "failed"]
    assert len(saved["tasks"][0]["attempts"]) == 3

    mapped = map_domain(result.domain_dir, live=True)
    assert mapped.validation.ok, mapped.validation.errors
    action = next(a for a in mapped.card["actions"] if a["name"] == "search_companies")
    assert action["output"]["next_params"]["until"] == "steps.search.total"
    assert mapped.validation.live[0].status == "SUCCESS"

    from mapper.pipeline import write_outputs

    _, report_path = write_outputs(mapped, tmp_path / "out")
    report = report_path.read_text()
    assert "## Explorer (tasks chosen by the LLM)" in report
    assert "| `add_note` | write | recorded |" in report


@pytest.mark.browser
def test_writes_are_explored_with_the_final_request_blocked(chromium, site, logged_in, tmp_path):
    brain = Brain([NOTE_TASK], {"Save a note": GOOD_NOTE}, {"Save a note": [OK]})
    result = explore(
        site.url + "/",
        state=logged_in,
        out=tmp_path / "captures",
        wanted=["save a note"],
        llm=_llm(brain),
        step_timeout_ms=1500,
    )
    [note] = result.tasks
    assert note.status == "recorded", note.reason
    assert note.runs == ["m1", "m2"]  # a write: live run with made-up words, one replay, no real/page2
    assert "save a note" in brain.prompts[0][1]  # the user's wording reached task discovery
    assert ("POST", "/api/notes") not in site.state.requests  # the save never reached the site
    for run in ("m1", "m2"):
        blocked = json.loads((result.domain_dir / "add_note" / run / "blocked.json").read_text())
        assert [b["method"] for b in blocked] == ["POST"]
    live_prompt = next(p for k, p in brain.prompts if k == "action")
    assert "intercepts it" in live_prompt and "payment" in live_prompt
    mapped = map_domain(result.domain_dir)
    assert mapped.validation.ok, mapped.validation.errors
    action = mapped.card["actions"][0]
    assert (action["kind"], action["requires_confirmation"]) == ("write", True)
    assert action["steps"][-1]["request"]["body"].startswith("csrf={{steps.compose.csrf | urlencode}}")


@pytest.mark.browser
def test_an_example_that_finds_nothing_is_swapped_not_failed(chromium, site, logged_in, tmp_path):
    task = {**SEARCH_TASK, "inputs": [{"name": "query", "example": "qqqnothing"}], "paginates": False}
    empty = {**OK, "ok": False, "empty": True, "reason": "No results found"}
    brain = Brain(
        [task],
        {
            "Search companies by keyword": [
                act("type", "textbox", "Search companies", text="qqqnothing"),
                act("click", "button", "Search", performs=True),
                act("done"),
                *GOOD_SEARCH,
            ]
        },
        {"Search companies by keyword": [empty, OK]},
        examples=[[{"name": "query", "example": "software"}]],
    )
    result = explore(
        site.url + "/", state=logged_in, out=tmp_path / "captures", llm=_llm(brain), step_timeout_ms=1500
    )
    [search] = result.tasks
    assert search.status == "recorded", search.reason
    assert search.plan.spec.inputs == {"query": "software"}  # the example that worked is the one kept
    assert "found nothing" in search.plan.attempts[0].error and search.plan.attempts[1].error is None
    meta = json.loads((result.domain_dir / "search_companies" / "real" / "meta.json").read_text())
    assert meta["real_inputs"] == {"query": "software"}


@pytest.mark.browser
def test_a_replay_that_breaks_on_an_empty_page_is_a_failed_try(chromium, site, logged_in, tmp_path):
    """Seen on LinkedIn: a step that only exists when there are results can't be replayed with a
    made-up word. The try is redone with that feedback."""
    task = {**SEARCH_TASK, "paginates": False}
    needs_results = [
        act("type", "textbox", "Search companies", text="software"),
        act("click", "button", "Search"),
        act("click", "button", "Next page", performs=True),  # only there when something was found
        act("done"),
    ]
    brain = Brain(
        [task],
        {"Search companies by keyword": needs_results + GOOD_SEARCH},
        {"Search companies by keyword": [OK, OK]},
    )
    result = explore(
        site.url + "/", state=logged_in, out=tmp_path / "captures", llm=_llm(brain), step_timeout_ms=1500
    )
    [search] = result.tasks
    assert search.status == "recorded", search.reason
    first, second = search.plan.attempts
    assert "replaying it broke" in first.error and "m1: step 3 (click #next)" in first.error
    assert second.error is None and search.runs == ["real", "m1", "m2"]
    retry_prompt = [p for k, p in brain.prompts if k == "action"][-1]
    assert "set categories or filters before you type" in retry_prompt


def test_replays_start_where_claude_began():
    from mapper.explore.explorer import replay_start

    steps = [
        {"type": "navigate", "url": "/search/results/companies/"},  # passed through, not worked on
        {"type": "input", "selector": "#q", "value": "{{params.query}}"},
        {"type": "key", "selector": "#q", "key": "Enter"},
        {"type": "mark"},
    ]
    url, rest = replay_start(steps, "https://www.linkedin.com/feed/foryou/")
    assert url == "https://www.linkedin.com/search/results/companies/" and rest == steps[1:]
    assert replay_start(steps[1:], "https://example.com/feed/") == ("https://example.com/feed/", steps[1:])


# --- a person's task list -----------------------------------------------------------------------

TASK_LIST = """actions
get_me
sign-in
Fixture: who is signed in, with name and headline. Takes no parameters.

get_company
(universal_name)
sign-in
Fixture: a company by its page address, with name, industry and followers.

list_conversations
sign-in
Fixture: the member's message threads.

list_conversations_next
(cursor, count?)
sign-in
Fixture: the next page of list_conversations.

get_conversation_messages
(conversation_urn)
sign-in
Fixture: the messages in one thread.

send_message
(conversation_urn, text)
sign-in
Fixture: send a message into a thread. A REAL SEND.
"""

LISTED_PLAN = [
    {"name": "get_me", "kind": "read", "start_path": "/", "paginates": False, "inputs": []},
    {
        "name": "get_company",
        "kind": "read",
        "start_path": "/",
        "paginates": False,
        "inputs": [
            {
                "name": "universal_name",
                "given": "address",
                "example": "northwind-labs",
                "alternatives": ["granite-labs", "copperleaf-labs"],  # real pages: no made-up 404s
            }
        ],
    },
    {"name": "list_conversations", "kind": "read", "start_path": "/", "paginates": False, "inputs": []},
    {
        "name": "get_conversation_messages",
        "kind": "read",
        "start_path": "/messages",
        "paginates": False,
        "inputs": [{"name": "conversation_urn", "given": "picked", "example": None}],
    },
    {
        "name": "send_message",
        "kind": "write",
        "start_path": "/messages",
        "paginates": False,
        "inputs": [
            {"name": "conversation_urn", "given": "picked", "example": None},
            {"name": "text", "given": "typed", "example": "Hello there"},
        ],
    },
]


def test_task_lists_are_read_from_the_written_format():
    from mapper.explore.tasklist import parse_task_list, task_list_for

    tasks = parse_task_list(TASK_LIST)
    assert [t.name for t in tasks][:2] == ["get_me", "get_company"]
    nxt = next(t for t in tasks if t.name == "list_conversations_next")
    assert [(i.name, i.optional) for i in nxt.inputs] == [("cursor", False), ("count", True)]
    assert tasks[0].sign_in and tasks[0].description.startswith("Fixture: who is signed in")
    assert len(parse_task_list(task_list_for("linkedin.com").read_text())) == 47  # the bundled LinkedIn list


def test_counts_are_found_by_pattern_wherever_they_sit():
    from mapper.pipeline.m6_output import count_fields

    items = [
        {"a": {"children": ["Acme", "1,323,457 followers"]}},
        {"a": {"children": ["Beta", ["x", "· 803,806 followers"]]}},  # one level deeper: still found
        {"a": {"children": ["Gamma", "12K followers"]}},
    ]
    [(expr, values)] = count_fields(items)
    assert values == ["1,323,457 followers", "· 803,806 followers", "12K followers"]
    assert "followers" in expr and expr.startswith("$..[?(")


@pytest.mark.browser
def test_a_task_list_is_explored_and_mapped(chromium, site, logged_in, tmp_path):
    """No input, an input in the address, a picked item, and a write whose send is blocked."""
    tasks_file = tmp_path / "tasks.txt"
    tasks_file.write_text(TASK_LIST)
    pick_ada = act("click", "button", "Ada Lovelace", picks="conversation_urn")
    brain = Brain(
        [],
        {
            "Fixture: who is signed in, with name and headline. Takes no parameters.": [
                act("navigate", url="/me", performs=True),
                act("done"),
            ],
            "Fixture: a company by its page address, with name, industry and followers.": [
                act("navigate", url="/company/northwind-labs", performs=True),
                act("done"),
            ],
            "Fixture: the member's message threads.": [
                act("navigate", url="/messages", performs=True),
                act("done"),
            ],
            "Fixture: the messages in one thread.": [{**pick_ada, "performs_task": True}, act("done")],
            "Fixture: send a message into a thread. A REAL SEND.": [
                pick_ada,
                typed("text", "textbox", "Write a message"),
                act("click", "button", "Send", performs=True),
                act("done"),
            ],
        },
        {
            d: [OK]
            for d in [
                "Fixture: who is signed in, with name and headline. Takes no parameters.",
                "Fixture: a company by its page address, with name, industry and followers.",
                "Fixture: the member's message threads.",
                "Fixture: the messages in one thread.",
                "Fixture: send a message into a thread. A REAL SEND.",
            ]
        },
        listed=LISTED_PLAN,
    )
    result = explore(
        site.url + "/",
        state=logged_in,
        out=tmp_path / "captures",
        tasks_file=tasks_file,
        llm=_llm(brain),
        step_timeout_ms=3000,
    )
    status = {o.plan.spec.name: (o.status, o.reason) for o in result.tasks}
    assert all(s == "recorded" for name, (s, _) in status.items() if name != "list_conversations_next"), (
        status
    )
    assert (
        status["list_conversations_next"][0] == "skipped" and "paging" in status["list_conversations_next"][1]
    )
    assert site.state.sent == []  # exploring the send never sent anything, retries included
    assert ("POST", "/api/drafts") not in site.state.requests  # nor the draft that carries the text
    # the picked thread differs in each replay
    meta = {
        r: json.loads((result.domain_dir / "get_conversation_messages" / r / "meta.json").read_text())
        for r in ("real", "m1", "m2")
    }
    assert all(m["inputs_via"] == {"conversation_urn": "picked"} for m in meta.values())

    for run, slug in (("m1", "granite-labs"), ("m2", "copperleaf-labs")):
        m = json.loads((result.domain_dir / "get_company" / run / "meta.json").read_text())
        assert m["markers"] == {"universal_name": slug}

    mapped = map_domain(result.domain_dir, live=True)
    assert mapped.validation.ok, mapped.validation.errors
    cards = {a["name"]: a for a in mapped.card["actions"]}
    # get_me: no input; the data request matched what the page showed
    assert cards["get_me"]["steps"][-1]["request"]["url"].endswith("/api/me")
    assert {"first_name", "last_name", "headline"} <= set(cards["get_me"]["output"]["fields"])
    # get_company: the input is in the address
    company = cards["get_company"]
    assert list(company["params"]) == ["universal_name"]
    assert "{{params.universal_name" in company["steps"][-1]["request"]["url"]
    # get_conversation_messages: the picked thread is a parameter
    msgs = cards["get_conversation_messages"]
    assert list(msgs["params"]) == ["conversation_urn"]
    assert "{{params.conversation_urn" in msgs["steps"][-1]["request"]["url"]
    # send_message: a write with the picked thread and the typed text, confirmed before it runs
    send = cards["send_message"]
    assert (send["kind"], send["requires_confirmation"]) == ("write", True)
    assert set(send["params"]) == {"conversation_urn", "text"}
    assert send["examples"] == []
    live = {r.action: r.status for r in mapped.validation.live}
    assert live == dict.fromkeys(
        ["get_me", "get_company", "list_conversations", "get_conversation_messages"], "SUCCESS"
    )


@pytest.mark.browser
def test_a_signed_out_session_stops_before_any_task(chromium, site, tmp_path):
    """LinkedIn signed the session out mid-run and the explorer kept opening browsers. Signed out (the
    start page sends us to /login), it stops at once, also when the look-around is reused from disk."""
    from mapper.explore.explorer import SURVEY_FILE, PlanError

    start = site.url + "/search"
    domain_dir = tmp_path / "captures" / "127.0.0.1"
    domain_dir.mkdir(parents=True)
    (domain_dir / SURVEY_FILE).write_text(json.dumps({"start_url": start, "site": "x", "pages": [start]}))
    brain = Brain([NOTE_TASK], {}, {})
    with pytest.raises(PlanError, match="not signed in"):
        explore(start, state=None, out=tmp_path / "captures", llm=_llm(brain), step_timeout_ms=1500)
    assert brain.prompts == []  # no task was planned or started
