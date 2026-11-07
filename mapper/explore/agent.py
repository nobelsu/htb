"""The LLM driver: Claude does one task in the live browser, one action at a time.

It is just another capture driver, next to a person and a script (record.py's ``agent`` hook): it moves
the browser and takes the goal mark. It never looks at network traffic and decodes nothing; the bundle
it leaves is the same as a person's.

Each turn Claude sees the page's accessibility tree (roles and names, as a screen reader would; typed
field contents are removed) and answers with one action on an element named by role and name. Claude
flags the one action that performs the task; the mark is taken the instant that action is done, with no
LLM call in between. When Claude says it is done, a separate check looks at the final page and confirms
it shows what the task asked for, and finds the next-page control for paginated tasks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from mapper.capture.driver import AFTER_MARK_MS, TYPING_PAUSE_MS
from mapper.domains import hostname, same_site
from mapper.explore.planner import Planner, TaskSpec

MAX_STEPS = 15
MAX_TREE_CHARS = 15_000
# never touched by the agent: one click on "Log out" ends the session the whole capture depends on
UNSAFE = re.compile(
    r"log\s*-?out|sign\s*-?out|log\s*off|delete|remove|deactivate|unsubscribe|close account", re.I
)
# `- textbox "Search": what was typed` → `- textbox "Search"`: field contents never go to the LLM
FIELD_VALUE = re.compile(
    r'^(\s*- (?:textbox|searchbox|combobox|spinbutton)(?: "(?:[^"\\]|\\.)*")?(?: \[[^\]]*\])*):\s.*$', re.M
)


class AgentFailed(Exception):
    """This attempt did not do the task; the message goes back to Claude on the next attempt."""

    def __init__(self, message: str, run: AgentRun | None = None) -> None:
        super().__init__(message)
        self.run = run


class StepError(Exception):
    """One action could not be carried out; Claude is told and picks another."""


@dataclass
class AgentRun:
    actions: list[dict] = field(default_factory=list)  # what Claude did, with the outcome of each
    marked: bool = False
    next_selector: str | None = None
    verdict: str = ""


def accessibility_tree(page, limit: int = MAX_TREE_CHARS) -> str:
    tree = FIELD_VALUE.sub(r"\1", page.locator("body").aria_snapshot())
    return tree if len(tree) <= limit else tree[:limit] + "\n[... rest of the page cut ...]"


def _find(page, role: str | None, name: str | None):
    if not role:
        raise StepError("no role given: name the element by the role and name shown in the tree")
    for exact in (True, False):
        loc = page.get_by_role(role, name=name, exact=exact) if name else page.get_by_role(role)
        for i in range(min(loc.count(), 10)):
            if loc.nth(i).is_visible():
                return loc.nth(i)
    raise StepError(f"no visible {role} named {name!r} on the page")


def _perform(page, tracker, act: dict, start_url: str) -> None:
    kind, role, name = act["action"], act.get("role"), act.get("name")
    if (name and UNSAFE.search(name)) or (kind == "navigate" and UNSAFE.search(act.get("url") or "")):
        target = f"{role} {name!r}" if name else repr(act.get("url"))
        raise StepError(f"{target} is not allowed (it could end the session or delete something)")
    if kind == "click":
        _find(page, role, name).click()
    elif kind == "type":
        _find(page, role, name).fill(act.get("text") or "")
        tracker.wait_quiet(page, timeout_ms=TYPING_PAUSE_MS)  # like a person glancing before Enter
    elif kind == "press":
        key = act.get("key") or "Enter"
        if role:
            _find(page, role, name).press(key)
        else:
            page.keyboard.press(key)
    elif kind == "navigate":
        url = urljoin(page.url, act.get("url") or "/")
        if not same_site(hostname(url), hostname(start_url)):
            raise StepError(f"{url} is not on this site")
        page.goto(url, wait_until="load")
    elif kind == "wait":
        page.wait_for_timeout(1500)
    else:
        raise StepError(f"unknown action {kind!r}")


def _describe(act: dict) -> str:
    target = f'{act.get("role")} "{act.get("name")}"' if act.get("role") else ""
    extra = {"type": f' text "{act.get("text")}"', "press": f" key {act.get('key')}"}.get(act["action"], "")
    extra += f" url {act.get('url')}" if act["action"] == "navigate" else ""
    flag = " [flagged as performing the task]" if act.get("performs_task") else ""
    return f"{act['action']} {target}{extra}".strip() + flag


def drive(
    page, tracker, mark, *, planner: Planner, spec: TaskSpec, start_url: str, feedback: list[str]
) -> AgentRun:
    """Do the task in ``page`` (already on the start page). Raises AgentFailed if it was not done."""
    run = AgentRun()
    history: list[str] = []
    for _ in range(MAX_STEPS):
        act = planner.next_action(spec, page.url, accessibility_tree(page), history, feedback)
        if act["action"] == "give_up":
            raise AgentFailed(f"Claude gave up: {act.get('reason', '')}", run)
        if act["action"] == "done":
            return _finish(page, run, planner, spec, history)
        try:
            _perform(page, tracker, act, start_url)
        except StepError as e:
            outcome = f"not done: {e}"
        except Exception as e:  # e.g. a Playwright timeout: report it and let Claude choose again
            outcome = f"failed: {type(e).__name__}: {(str(e).strip().splitlines() or [''])[0][:200]}"
        else:
            outcome = "ok"
            if act.get("performs_task") and not run.marked:
                mark()  # the instant the triggering action is done, like a person pressing Enter
                run.marked = True
                outcome = "ok, goal marked"
                tracker.wait_quiet(page, timeout_ms=AFTER_MARK_MS)  # let the answer load into the recording
            else:
                if act.get("performs_task"):
                    outcome = "ok, but the goal was already marked at an earlier action (not marked again)"
                if act["action"] in ("click", "press", "navigate"):
                    tracker.wait_quiet(page)
        run.actions.append({**act, "outcome": outcome})
        history.append(f"{len(history) + 1}. {_describe(act)} -> {outcome}")
    raise AgentFailed(f"not finished after {MAX_STEPS} actions", run)


def _finish(page, run: AgentRun, planner: Planner, spec: TaskSpec, history: list[str]) -> AgentRun:
    if not run.marked:
        raise AgentFailed("said done, but no action was flagged as performing the task (no mark)", run)
    typed = " ".join(
        a.get("text") or "" for a in run.actions if a["action"] == "type" and a["outcome"].startswith("ok")
    )
    missing = [k for k, v in spec.inputs.items() if v not in typed]
    if missing:
        raise AgentFailed(f"never typed the input(s) {missing} exactly as given", run)
    verdict = planner.check_result(spec, page.url, accessibility_tree(page), history)
    run.verdict = verdict["reason"]
    if not verdict["ok"]:
        raise AgentFailed(f"the final page does not show the task's result: {verdict['reason']}", run)
    if spec.paginates and verdict.get("next_role"):
        try:
            control = _find(page, verdict["next_role"], verdict.get("next_name"))
            run.next_selector = control.evaluate("el => window.__mapperSelectorFor(el)")
        except StepError:
            run.next_selector = None
    return run
