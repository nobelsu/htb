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

import json
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import quote, urljoin

PARAM_RE = re.compile(r"\{\{\s*params\.([A-Za-z0-9_]+)\s*\}\}")

TYPING_PAUSE_MS = 2000
AFTER_MARK_MS = 10_000

STEP_TYPES = {"navigate", "goto", "input", "click", "key", "wait", "wait_for", "mark", "done"}


class ScriptError(ValueError):
    pass


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


class NetworkTracker:
    """Counts in-flight requests on a browser context so the driver can wait for quiet."""

    def __init__(self, context) -> None:
        self.inflight = 0
        self.last_change = time.monotonic()
        context.on("request", self._start)
        context.on("requestfinished", self._end)
        context.on("requestfailed", self._end)

    def _start(self, req) -> None:
        if req.resource_type in NEVER_ENDING:
            return
        self.inflight += 1
        self.last_change = time.monotonic()

    def _end(self, req) -> None:
        if req.resource_type in NEVER_ENDING:
            return
        self.inflight = max(0, self.inflight - 1)
        self.last_change = time.monotonic()

    def wait_quiet(self, page, quiet_ms: int = 300, timeout_ms: int = 10_000) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            page.wait_for_timeout(50)  # also pumps Playwright events
            if self.inflight == 0 and (time.monotonic() - self.last_change) * 1000 >= quiet_ms:
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
        kind = step["type"]
        marks_next = i + 1 < len(steps) and steps[i + 1]["type"] == "mark"
        if kind == "done":
            return
        if kind == "mark":
            on_mark()  # right after the triggering action, like a person pressing Enter in the terminal
            tracker.wait_quiet(page, timeout_ms=AFTER_MARK_MS)  # let the answer finish loading
        elif kind in ("navigate", "goto"):
            url = substitute(str(step["url"]), {k: quote(v, safe="") for k, v in params.items()})
            page.goto(urljoin(start_url, url), wait_until="load")
            tracker.wait_quiet(page)
        elif kind == "input":
            page.fill(step["selector"], substitute(str(step["value"]), params))
            tracker.wait_quiet(page, timeout_ms=TYPING_PAUSE_MS)
        elif kind == "click":
            page.click(step["selector"])
            if not marks_next:
                tracker.wait_quiet(page)
        elif kind == "key":
            page.press(step.get("selector") or "body", step.get("key", "Enter"))
            if not marks_next:
                tracker.wait_quiet(page)
        elif kind == "wait":
            page.wait_for_timeout(int(step.get("ms", 250)))
        elif kind == "wait_for":
            page.wait_for_selector(step["selector"], timeout=int(step.get("timeout_ms", 10_000)))
