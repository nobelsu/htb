"""Scripted driver for headless captures (tests, and replaying ``actions.jsonl`` for healing).

A script is a list of steps (or a JSONL file of them). Step types:

* ``{"type": "navigate", "url": "/search"}``: relative URLs resolve against ``start_url``.
* ``{"type": "input", "selector": "#q", "value": "{{params.query}}"}``: fills a field.
* ``{"type": "click", "selector": "button[type=submit]"}``
* ``{"type": "key", "selector": "#q", "key": "Enter"}``
* ``{"type": "wait", "ms": 200}`` and ``{"type": "wait_for", "selector": "#results li"}``
* ``{"type": "mark"}``: goal mark (what pressing Enter in the terminal does in headed mode).
* ``{"type": "done"}``: stop (optional; the end of the script also stops).

``input``, ``click``, ``key`` and ``navigate`` match the ``actions.jsonl`` event types, so a recorded
run can be fed back in. ``{{params.NAME}}`` in a value is replaced with the run's marker or real input.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import urljoin

PARAM_RE = re.compile(r"\{\{\s*params\.([A-Za-z0-9_]+)\s*\}\}")

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


class NetworkTracker:
    """Counts in-flight requests on a browser context so the driver can wait for quiet."""

    def __init__(self, context) -> None:
        self.inflight = 0
        self.last_change = time.monotonic()
        context.on("request", self._start)
        context.on("requestfinished", self._end)
        context.on("requestfailed", self._end)

    def _start(self, _req) -> None:
        self.inflight += 1
        self.last_change = time.monotonic()

    def _end(self, _req) -> None:
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
    for step in steps:
        kind = step["type"]
        if kind == "done":
            return
        if kind == "mark":
            tracker.wait_quiet(page)
            on_mark()
        elif kind in ("navigate", "goto"):
            page.goto(urljoin(start_url, step["url"]), wait_until="load")
            tracker.wait_quiet(page)
        elif kind == "input":
            page.fill(step["selector"], substitute(str(step["value"]), params))
        elif kind == "click":
            page.click(step["selector"])
            tracker.wait_quiet(page)
        elif kind == "key":
            page.press(step.get("selector") or "body", step.get("key", "Enter"))
            tracker.wait_quiet(page)
        elif kind == "wait":
            page.wait_for_timeout(int(step.get("ms", 250)))
        elif kind == "wait_for":
            page.wait_for_selector(step["selector"], timeout=int(step.get("timeout_ms", 10_000)))
