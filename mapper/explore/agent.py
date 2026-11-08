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
from urllib.parse import urlsplit, quote, quote_plus, urljoin

from mapper.capture.driver import AFTER_MARK_MS, TYPING_PAUSE_MS, settle
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


# The visible thing a person clicks for a control the site hides (a styled checkbox inside its pill):
# its label, else the nearest visible wrapper.
STAND_IN_JS = """el => {
  const shown = (n) => {
    const r = n.getBoundingClientRect(), s = getComputedStyle(n);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  };
  for (const l of el.labels || []) if (shown(l)) return l;
  for (let n = el.parentElement, i = 0; n && i < 3; n = n.parentElement, i++) if (shown(n)) return n;
  return null;
}"""
MAX_SAME_FAILURE = 3  # the same failing action this many times ends the attempt


class AgentFailed(Exception):
    """This attempt did not do the task; the message goes back to Claude on the next attempt."""

    def __init__(self, message: str, run: AgentRun | None = None) -> None:
        super().__init__(message)
        self.run = run


class SignedOut(Exception):
    """The site sent us to a sign-in page: the session has expired or the site signed it out (often its
    anti-bot reaction). Every task stops: going on would only look more like a bot."""


class EmptyResult(AgentFailed):
    """Done right, but the site found nothing for the example input: try a different example."""


class StepError(Exception):
    """One action could not be carried out; Claude is told and picks another."""


# For a "picked" input: the same spot in two other items of the list the chosen item sits in, so the
# replays choose differently and the mapper can see which part of the request is the choice.
OTHER_ITEMS_JS = """el => {
  const sel = window.__mapperSelectorFor;
  let item = el;
  const path = [];
  while (item && item.parentElement) {
    const same = Array.from(item.parentElement.children).filter((c) => c.tagName === item.tagName);
    if (same.length >= 3) break;
    path.unshift(Array.from(item.parentElement.children).indexOf(item));
    item = item.parentElement;
  }
  if (!item || !item.parentElement || !sel) return [];
  const same = Array.from(item.parentElement.children).filter((c) => c.tagName === item.tagName);
  const i = same.indexOf(item), out = [];
  for (let k = 1; k < same.length && out.length < 2; k++) {
    let node = same[(i + k) % same.length];
    for (const idx of path) node = node && node.children[idx];
    const s = node && sel(node);
    if (s && document.querySelectorAll(s).length === 1) out.push(s);
  }
  return out;
}"""
SCREEN_TEXT = re.compile(r'^\s*- [\w-]+(?: "((?:[^"\\]|\\.)*)")?(?: \[[^\]]*\])*(?:: (.+))?$', re.M)


@dataclass
class AgentRun:
    actions: list[dict] = field(default_factory=list)  # what Claude did, with the outcome of each
    marked: bool = False
    next_selector: str | None = None
    verdict: str = ""
    picks: dict[str, dict] = field(default_factory=dict)  # input → {"at": iso time, "others": [selectors]}
    screen: list[str] = field(default_factory=list)  # text on the final page (for reads without input)
    urls: list[str] = field(default_factory=list)  # pages visited, to check inputs given in the address


def accessibility_tree(page, limit: int = MAX_TREE_CHARS) -> str:
    tree = FIELD_VALUE.sub(r"\1", page.locator("body").aria_snapshot())
    return tree if len(tree) <= limit else tree[:limit] + "\n[... rest of the page cut ...]"


def _find(page, role: str | None, name: str | None, for_click: bool = False):
    if not role:
        raise StepError("no role given: name the element by the role and name shown in the tree")
    hidden = None
    for exact in (True, False):
        loc = page.get_by_role(role, name=name, exact=exact) if name else page.get_by_role(role)
        for i in range(min(loc.count(), 10)):
            if loc.nth(i).is_visible():
                return loc.nth(i)
            hidden = hidden or loc.nth(i)
    if hidden is not None and for_click:
        stand_in = hidden.evaluate_handle(STAND_IN_JS).as_element()
        if stand_in is not None:
            return stand_in  # a person clicks the visible label or pill, not the hidden input
    if hidden is not None:
        raise StepError(f"the {role} named {name!r} is hidden on the page and has no visible label to click")
    raise StepError(f"no visible {role} named {name!r} on the page")


def _input_forms(values: dict[str, str]) -> list[str]:
    forms = set()
    for v in values.values():
        if v:
            forms |= {v.lower(), quote(v, safe="").lower(), quote_plus(v).lower()}
    return [f for f in forms if len(f) >= 2]


def _perform(page, tracker, act: dict, start_url: str, inputs: dict[str, str]) -> None:
    kind, role, name = act["action"], act.get("role"), act.get("name")
    if (name and UNSAFE.search(name)) or (kind == "navigate" and UNSAFE.search(act.get("url") or "")):
        target = f"{role} {name!r}" if name else repr(act.get("url"))
        raise StepError(f"{target} is not allowed (it could end the session or delete something)")
    if kind == "click":
        _find(page, role, name, for_click=True).click()
    elif kind == "type":
        _find(page, role, name).fill(act.get("text") or "")
        tracker.wait_for_action(page, TYPING_PAUSE_MS)  # suggestion lookups finish before Enter
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


def _note_pick(page, run: AgentRun, act: dict) -> None:
    """Before clicking the chosen item: remember when, and how to click two other items instead."""
    from mapper.bundle import iso_now

    el = _find(page, act.get("role"), act.get("name"), for_click=True)
    others = el.evaluate(OTHER_ITEMS_JS) or []
    if len(others) < 2:
        raise StepError(
            f"{act.get('role')} {act.get('name')!r} is not one of several similar items in a list: "
            "pick an item from a list with at least three of them"
        )
    run.picks[act["picks"]] = {"at": iso_now(), "others": others}


def _screen_text(tree: str) -> list[str]:
    """The names and texts shown on a page, from its accessibility tree (no field contents)."""
    out = []
    for m in SCREEN_TEXT.finditer(tree):
        for text in m.groups():
            text = (text or "").strip().strip('"')
            if 2 <= len(text) <= 200 and text not in out and not text.startswith("/"):
                out.append(text)
    return out


def _describe(act: dict) -> str:
    target = f'{act.get("role")} "{act.get("name")}"' if act.get("role") else ""
    extra = {"type": f' text "{act.get("text")}"', "press": f" key {act.get('key')}"}.get(act["action"], "")
    extra += f" url {act.get('url')}" if act["action"] == "navigate" else ""
    extra += f" (picks {act['picks']})" if act.get("picks") else ""
    flag = " [flagged as performing the task]" if act.get("performs_task") else ""
    return f"{act['action']} {target}{extra}".strip() + flag


SIGN_IN_PATH = re.compile(r"/(log-?in|sign-?in|auth(wall)?|uas/login|session)(/|$|\?)", re.I)


def signed_out(start_url: str, landed_url: str, page) -> bool:
    """``page`` is a sign-in page (or a "confirm your password" one) instead of the signed-in page asked for."""
    if urlsplit(landed_url).path == urlsplit(start_url).path and not SIGN_IN_PATH.search(urlsplit(landed_url).path):
        return False
    try:
        has_password = page.locator("input[type=password]").count() > 0
    except Exception:
        has_password = False
    return bool(SIGN_IN_PATH.search(urlsplit(landed_url).path)) or has_password


def drive(
    page, tracker, mark, *, planner: Planner, spec: TaskSpec, start_url: str, feedback: list[str]
) -> AgentRun:
    """Do the task in ``page`` (already on the start page). Raises AgentFailed if it was not done."""
    run = AgentRun()
    history: list[str] = []
    failed: dict[tuple, int] = {}  # identical failing actions, counted
    for _ in range(MAX_STEPS):
        if signed_out(start_url, page.url, page):
            raise SignedOut(f"{page.url} is a sign-in page: the session was signed out")
        act = planner.next_action(spec, page.url, accessibility_tree(page), history, feedback)
        if act["action"] == "give_up":
            raise AgentFailed(f"Claude gave up: {act.get('reason', '')}", run)
        if act["action"] == "done":
            return _finish(page, run, planner, spec, history)
        blocker = getattr(tracker, "blocker", None)
        if act.get("performs_task") and not run.marked and blocker is not None:
            blocker.arm()  # a write's final click: what it sends is recorded but never reaches the site
        try:
            if act.get("picks") and act["action"] == "click":
                _note_pick(page, run, act)
            tracker.action()
            _perform(page, tracker, act, start_url, spec.inputs)
        except StepError as e:
            outcome = f"not done: {e}"
        except Exception as e:  # e.g. a Playwright timeout: report it and let Claude choose again
            outcome = f"failed: {type(e).__name__}: {(str(e).strip().splitlines() or [''])[0][:200]}"
        else:
            outcome = "ok"
            if act.get("performs_task") and act["action"] == "navigate":
                tracker.extend()  # the task is this page: include what it fetches right after loading
            if act.get("performs_task") and not run.marked:
                mark()  # the instant the triggering action is done, like a person pressing Enter
                run.marked = True
                outcome = "ok, goal marked"
                tracker.wait_for_action(page, AFTER_MARK_MS)  # until the task's own requests are answered
                if blocker is not None:
                    blocker.disarm()
            else:
                if act.get("performs_task"):
                    outcome = "ok, but the goal was already marked at an earlier action (not marked again)"
                if act["action"] in ("click", "press", "navigate"):
                    settle(page)
        if not outcome.startswith("ok"):
            same = tuple(act.get(k) for k in ("action", "role", "name", "text", "key", "url"))
            failed[same] = failed.get(same, 0) + 1
            if failed[same] >= MAX_SAME_FAILURE:
                run.actions.append({**act, "outcome": outcome})
                raise AgentFailed(
                    f"repeated the same failing action {MAX_SAME_FAILURE} times: {_describe(act)}", run
                )
            if failed[same] == MAX_SAME_FAILURE - 1:
                outcome += (
                    " (this exact action has now failed twice: it will not work, do something different)"
                )
        if act["action"] == "navigate":
            run.urls.append(page.url)
        run.actions.append({**act, "outcome": outcome})
        history.append(f"{len(history) + 1}. {_describe(act)} -> {outcome}")
    raise AgentFailed(f"not finished after {MAX_STEPS} actions", run)


def _finish(page, run: AgentRun, planner: Planner, spec: TaskSpec, history: list[str]) -> AgentRun:
    if not run.marked:
        raise AgentFailed("said done, but no action was flagged as performing the task (no mark)", run)
    typed = " ".join(
        a.get("text") or "" for a in run.actions if a["action"] == "type" and a["outcome"].startswith("ok")
    )
    addresses = " ".join(run.urls).lower()
    missing = [
        k
        for k, v in spec.inputs.items()
        if v not in typed and not any(f in addresses for f in _input_forms({k: v}))
    ]
    missing += [k for k in spec.picks if k not in run.picks]
    if missing:
        raise AgentFailed(
            f"never gave the input(s) {missing} as asked (typed, in the address or picked)", run
        )
    final_tree = accessibility_tree(page)
    run.screen = _screen_text(final_tree)
    verdict = planner.check_result(spec, page.url, final_tree, history)
    run.verdict = verdict["reason"]
    if not verdict["ok"] and verdict.get("empty"):
        raise EmptyResult(f"the site found nothing for {spec.inputs}: {verdict['reason']}", run)
    if not verdict["ok"]:
        raise AgentFailed(f"the final page does not show the task's result: {verdict['reason']}", run)
    if spec.paginates and verdict.get("next_role"):
        try:
            control = _find(page, verdict["next_role"], verdict.get("next_name"))
            run.next_selector = control.evaluate("el => window.__mapperSelectorFor(el)")
        except StepError:
            run.next_selector = None
    return run
