"""Scripted driver for headless captures (tests, and replaying ``actions.jsonl`` for healing).

A script is a list of steps (or a JSONL file of them). Step types:

* ``{"type": "navigate", "url": "/search?q={{params.query}}"}``: relative URLs resolve against
  ``start_url``; ``{{params.NAME}}`` in a URL is filled in URL-encoded.
* ``{"type": "input", "selector": "#q", "value": "{{params.query}}"}``: fills a field.
* ``{"type": "click", "selector": "button[type=submit]"}``
* ``{"type": "key", "selector": "#q", "key": "Enter"}``
* ``{"type": "wait", "ms": 200}`` and ``{"type": "wait_for", "selector": "#results li"}``
* ``{"type": "mark"}``: goal mark (what pressing Enter in the terminal does in headed mode).
  It is taken the moment the step before it (the click or Enter that performs the task) is done, with
  no waiting, so the mapper's window around the goal stays tight. The run then keeps recording until
  the page goes quiet (at most ``AFTER_MARK_MS``) so the answer lands in the recording.
* ``{"type": "done"}``: stop (optional; the end of the script also stops).

After an ``input`` the driver pauses until the page settles (at most ``TYPING_PAUSE_MS``), the way a
person glances at the box before pressing Enter: suggestion lookups fired by typing then happen before
the triggering action, not inside the goal window.

``input``, ``click``, ``key`` and ``navigate`` match the ``actions.jsonl`` event types, so a recorded
run can be fed back in. ``{{params.NAME}}`` in a value is replaced with the run's marker or real input.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import quote, urljoin

PARAM_RE = re.compile(r"\{\{\s*params\.([A-Za-z0-9_]+)\s*\}\}")

SETTLE_MS = 1000  # after a click, Enter or page load: a flat pause (busy sites never go quiet)
TYPING_PAUSE_MS = 2000  # at most, after typing: until the lookups the typing started are answered
AFTER_MARK_MS = 10_000  # at most, after the mark: until the task's own requests are answered
# Requests that start this soon after an action are that action's (M3 gives the goal the same grace
# after the mark). Waits after typing and after the mark wait for those, not for background traffic.
ACTION_WINDOW_MS = 500

STEP_TYPES = {"navigate", "goto", "input", "click", "key", "wait", "wait_for", "mark", "done"}


class ScriptError(ValueError):
    pass


class StepFailed(RuntimeError):
    """A step could not be carried out; the message says which step, so it can be reported back."""


def settle(page) -> None:
    """The pause after a click, Enter or page load. Fixed, not 'until the network is quiet': busy sites
    keep prefetching and tracking in the background, which never changes what is on screen."""
    page.wait_for_timeout(SETTLE_MS)


def load_script(source: str | Path | Iterable[dict]) -> list[dict]:
    if isinstance(source, (str, Path)):
        lines = Path(source).read_text().splitlines()
        steps = [json.loads(line) for line in lines if line.strip() and not line.lstrip().startswith("#")]
    else:
        steps = [dict(s) for s in source]
    for i, step in enumerate(steps):
        if step.get("type") not in STEP_TYPES:
            raise ScriptError(f"step {i}: unknown type {step.get('type')!r}")
    return steps


def substitute(value: str, params: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name not in params:
            raise ScriptError(f"script uses {{{{params.{name}}}}} but no value was given for {name!r}")
        return params[name]

    return PARAM_RE.sub(repl, value)


# Connections a page holds open for good (live updates). They never finish, so counting them would
# keep a busy site from ever looking quiet and every wait would run to its cap.
NEVER_ENDING = frozenset({"eventsource", "websocket"})


class WriteBlocker:
    """Stops the browser from sending changes to the site while armed: around the final click of a write
    task (send, post, connect) during exploring, so nothing reaches the site, but the recording still
    holds the request it would have sent (method, address, headers, body)."""

    SAFE = ("GET", "HEAD", "OPTIONS")

    def __init__(self, context, host_is_site: Callable[[str], bool]) -> None:
        self.context, self.host_is_site = context, host_is_site
        self.blocked: list[dict] = []
        self.armed = False

    def _handle(self, route) -> None:
        req = route.request
        try:
            if req.method.upper() not in self.SAFE and self.host_is_site(req.url):
                self.blocked.append({"method": req.method.upper(), "url": req.url})
                route.abort("blockedbyclient")
            else:
                route.continue_()
        except Exception:  # the page may be gone; never fail the step that is running
            pass

    def arm(self) -> None:
        if not self.armed:
            self.context.route("**/*", self._handle)
            self.armed = True

    def disarm(self) -> None:
        if self.armed:
            with contextlib.suppress(Exception):  # the browser may already be closing
                self.context.unroute("**/*", self._handle)
            self.armed = False


class NetworkTracker:
    """Counts in-flight requests on a browser context so the driver can wait for quiet."""

    blocker: WriteBlocker | None = None  # set for write tasks: armed around the marked action

    def __init__(self, context) -> None:
        self.inflight = 0
        self.last_change = time.monotonic()
        self.started: dict[int, tuple[float, object]] = {}  # in-flight: id -> (when it started, request)
        self.action_at = time.monotonic()
        self.window_until: float | None = None  # see extend()
        context.on("request", self._start)
        context.on("requestfinished", self._end)
        context.on("requestfailed", self._end)

    def _start(self, req) -> None:
        if req.resource_type in NEVER_ENDING:
            return
        self.inflight += 1
        self.last_change = time.monotonic()
        self.started[id(req)] = (self.last_change, req)  # holding req keeps its id from being reused

    def _end(self, req) -> None:
        if req.resource_type in NEVER_ENDING:
            return
        self.inflight = max(0, self.inflight - 1)
        self.started.pop(id(req), None)
        self.last_change = time.monotonic()

    def action(self) -> None:
        """Call right before the driver acts: what starts in the next moment belongs to that action."""
        self.action_at = time.monotonic()
        self.window_until = None

    def extend(self, ms: int = ACTION_WINDOW_MS) -> None:
        """After a page load that performs the task: the data the page fetches right after loading
        belongs to the action too."""
        self.window_until = time.monotonic() + ms / 1000

    def wait_for_action(self, page, timeout_ms: int, window_ms: int = ACTION_WINDOW_MS) -> None:
        """Wait until the requests the last action started (within ``window_ms`` of it) are answered.

        Background traffic that was already running, or starts later (prefetching, tracking, bot-check
        sensors), is ignored: on busy sites it never stops. At most ``timeout_ms``."""
        since, deadline = self.action_at, time.monotonic() + timeout_ms / 1000
        end = self.window_until or since + window_ms / 1000
        while time.monotonic() < deadline:
            page.wait_for_timeout(50)  # also pumps Playwright events
            if time.monotonic() < end:
                continue  # the action's requests may not all have started yet
            if not any(since <= t <= end for t, _ in self.started.values()):
                return


def run_script(
    page,
    steps: list[dict],
    *,
    start_url: str,
    params: dict[str, str],
    tracker: NetworkTracker,
    on_mark: Callable[[], None],
) -> None:
    for i, step in enumerate(steps):
        if step["type"] == "done":
            return
        try:
            _run_step(page, step, i, steps, start_url, params, tracker, on_mark)
        except ScriptError:
            raise
        except Exception as e:
            target = step.get("selector") or step.get("url") or ""
            first = (str(e).strip().splitlines() or [type(e).__name__])[0][:200]
            raise StepFailed(f"step {i + 1} ({step['type']} {target}): {type(e).__name__}: {first}") from e


def _run_step(
    page, step: dict, i: int, steps: list[dict], start_url: str, params: dict[str, str], tracker, on_mark
) -> None:
    kind = step["type"]
    marks_next = i + 1 < len(steps) and steps[i + 1]["type"] == "mark"
    blocker = getattr(tracker, "blocker", None)
    if marks_next and blocker is not None and kind in ("click", "key", "navigate", "goto", "input"):
        blocker.arm()  # a write's final action: what it sends is recorded but never reaches the site
    if kind == "mark":
        on_mark()  # right after the triggering action, like a person pressing Enter in the terminal
        tracker.wait_for_action(page, AFTER_MARK_MS)  # until the task's own requests are answered
        if blocker is not None:
            blocker.disarm()
    elif kind in ("navigate", "goto"):
        url = substitute(str(step["url"]), {k: quote(v, safe="") for k, v in params.items()})
        tracker.action()
        page.goto(urljoin(start_url, url), wait_until="load")
        if marks_next:
            tracker.extend()  # the task is this page: include what it fetches right after loading
        else:
            settle(page)
    elif kind == "input":
        tracker.action()
        page.fill(step["selector"], substitute(str(step["value"]), params))
        tracker.wait_for_action(page, TYPING_PAUSE_MS)  # suggestion lookups finish before Enter
    elif kind == "click":
        tracker.action()
        page.click(step["selector"])
        if not marks_next:
            settle(page)
    elif kind == "key":
        tracker.action()
        page.press(step.get("selector") or "body", step.get("key", "Enter"))
        if not marks_next:
            settle(page)
    elif kind == "wait":
        page.wait_for_timeout(int(step.get("ms", 250)))
    elif kind == "wait_for":
        page.wait_for_selector(step["selector"], timeout=int(step.get("timeout_ms", 10_000)))
