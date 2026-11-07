"""Record one capture run into a bundle.

Headed (default): a human drives the browser. In the terminal, press Enter right after the click
that performs the task (goal mark); type ``done`` to finish. ``--headless`` runs a scripted driver
(see ``driver.py``) instead; that is what the tests use.

Bundle (``captures/<domain>/<task>/<run>/``): ``traffic.har``, ``state.json`` (SENSITIVE),
``actions.jsonl``, ``meta.json``. HAR and state contain live cookies: written with mode 0600 and
never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from mapper import BUNDLE_VERSION, __version__
from mapper.bundle import (
    ACTIONS_FILE,
    HAR_FILE,
    MARKER_RUNS,
    META_FILE,
    REAL_RUNS,
    STATE_FILE,
    bundle_dir,
    iso_now,
)
from mapper.capture import driver
from mapper.domains import hostname, registrable_domain

# The mapper only reads a site's data requests and HTML pages (M1 drops everything else). Capture
# still lets the browser load everything, exactly as for a person; only the saved file is trimmed:
# these entries keep their URL, status and headers but not their body. Scripts alone were 57% of a
# LinkedIn recording.
NO_BODY = frozenset({"image", "media", "font", "script", "stylesheet", "manifest", "texttrack"})


def slim_har(path: Path) -> None:
    """Drop the bodies of static files (code, styles, images, video, fonts) from a saved recording."""
    har = json.loads(path.read_text(encoding="utf-8"))
    for entry in har.get("log", {}).get("entries", []):
        content = (entry.get("response") or {}).get("content") or {}
        if entry.get("_resourceType") in NO_BODY and "text" in content:
            del content["text"]
            content.pop("encoding", None)
            content["comment"] = "body not saved: the mapper does not read static files"
    path.write_text(json.dumps(har), encoding="utf-8")


HUMAN_HELP = (
    "Drive the browser to perform the task.\n"
    "  Enter        = goal mark (press it right after the click that does the task)\n"
    "  done + Enter = finish the run\n"
)


class CaptureError(RuntimeError):
    pass


@dataclass
class CaptureConfig:
    start_url: str
    task: str
    run: str
    markers: dict[str, str] = field(default_factory=dict)
    real_inputs: dict[str, str] = field(default_factory=dict)
    kind: str = "read"
    description: str = ""
    state: Path | None = None
    out: Path = Path("captures")
    headless: bool = False
    script: str | Path | list[dict] | None = None
    # A live driver, e.g. the explorer's LLM: called as agent(page, tracker, mark) once the start page has
    # loaded, it moves the browser and calls mark() right after the action that performs the task.
    agent: Callable[[Any, driver.NetworkTracker, Callable[[], None]], None] | None = None
    step_timeout_ms: int | None = None  # Playwright's per-action timeout; None keeps its default (30 s)
    force: bool = False

    @property
    def domain(self) -> str:
        return registrable_domain(hostname(self.start_url))

    @property
    def params(self) -> dict[str, str]:
        return self.markers if self.run in MARKER_RUNS else self.real_inputs

    def check(self) -> None:
        if not hostname(self.start_url):
            raise CaptureError(f"start_url has no host: {self.start_url!r}")
        if self.run in MARKER_RUNS and self.real_inputs:
            raise CaptureError(f"run {self.run} takes --marker values, not --input")
        if self.run in REAL_RUNS and self.markers:
            raise CaptureError(f"run {self.run} takes --input values, not --marker")
        if self.headless and self.script is None and self.agent is None:
            raise CaptureError("--headless needs a --script for the scripted driver")
        if self.script is not None and self.agent is not None:
            raise CaptureError("a capture has one driver: a script or an agent, not both")
        if self.state is not None and not Path(self.state).is_file():
            raise CaptureError(f"--state file not found: {self.state}")


class ActionLog:
    """Collects UI events (from the injected logger and from Python) into actions.jsonl."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.events: list[dict] = []
        self._fh = path.open("a", encoding="utf-8")

    def add(self, event: dict) -> None:
        self.events.append(event)
        self._fh.write(json.dumps(event) + "\n")  # streamed so a crash keeps what we had
        self._fh.flush()

    def finish(self) -> None:
        self._fh.close()
        ordered = sorted(self.events, key=lambda e: e.get("t", ""))
        self.path.write_text("".join(json.dumps(e) + "\n" for e in ordered), encoding="utf-8")


def _logger_js() -> str:
    return resources.files("mapper.capture").joinpath("actions_logger.js").read_text(encoding="utf-8")


def _attach_navigation_log(page, log: ActionLog) -> None:
    def on_nav(frame) -> None:
        if frame == page.main_frame and frame.url not in ("", "about:blank"):
            log.add({"t": iso_now(), "type": "navigate", "url": frame.url})

    page.on("framenavigated", on_nav)


def _human_loop(context, marks: list[str]) -> None:
    lines: queue.Queue[str] = queue.Queue()

    def read_stdin() -> None:
        for line in sys.stdin:
            lines.put(line.strip())
        lines.put("done")  # EOF ends the run

    threading.Thread(target=read_stdin, daemon=True).start()
    print(HUMAN_HELP, flush=True)
    while True:
        try:
            line = lines.get_nowait()
        except queue.Empty:
            if not context.pages:
                print("browser window closed: finishing", flush=True)
                return
            try:
                context.pages[0].wait_for_timeout(100)  # pumps Playwright events
            except Exception:
                return
            continue
        if line.lower() == "done":
            return
        if line == "":
            marks.append(iso_now())
            print(f"goal mark {len(marks)} at {marks[-1]}", flush=True)
        else:
            print("press Enter for a goal mark, or type 'done' to finish", flush=True)


def _write_private(path: Path) -> None:
    if path.exists():
        os.chmod(path, 0o600)


def record(cfg: CaptureConfig) -> Path:
    """Run one capture. Returns the bundle directory."""
    cfg.check()
    from playwright.sync_api import sync_playwright  # heavy import, only when capturing

    steps = driver.load_script(cfg.script) if cfg.script is not None else None
    out = bundle_dir(cfg.out, cfg.domain, cfg.task, cfg.run)
    if out.exists():
        if not cfg.force:
            raise CaptureError(f"bundle already exists: {out} (use --force to overwrite)")
        shutil.rmtree(out)
    out.mkdir(parents=True)

    meta = {
        "bundle_version": BUNDLE_VERSION,
        "domain": cfg.domain,
        "start_url": cfg.start_url,
        "task": cfg.task,
        "task_description": cfg.description,
        "run": cfg.run,
        "markers": dict(cfg.markers) if cfg.run in MARKER_RUNS else {},
        "real_inputs": dict(cfg.real_inputs) if cfg.run in REAL_RUNS else {},
        "page": 2 if cfg.run == "page2" else 1,
        "kind_hint": cfg.kind,
        "driver": "script" if steps is not None else "agent" if cfg.agent is not None else "human",
        "goal_marks": [],
        "started_at": iso_now(),
        "ended_at": None,
        "mapper_version": __version__,
    }
    (out / META_FILE).write_text(json.dumps(meta, indent=2) + "\n")

    log = ActionLog(out / ACTIONS_FILE)
    marks: list[str] = meta["goal_marks"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=cfg.headless)
        context = browser.new_context(
            record_har_path=str(out / HAR_FILE),
            record_har_content="embed",
            record_har_mode="full",
            storage_state=str(cfg.state) if cfg.state else None,
        )
        try:
            context.expose_binding("__mapperLog", lambda _source, event: log.add(dict(event)))
            context.add_init_script(_logger_js())
            context.on("page", lambda p: _attach_navigation_log(p, log))
            tracker = driver.NetworkTracker(context)
            page = context.new_page()
            if cfg.step_timeout_ms:
                page.set_default_timeout(cfg.step_timeout_ms)
            page.goto(cfg.start_url, wait_until="load")
            driver.settle(page)

            if steps is not None:
                driver.run_script(
                    page,
                    steps,
                    start_url=cfg.start_url,
                    params=cfg.params,
                    tracker=tracker,
                    on_mark=lambda: marks.append(iso_now()),
                )
            elif cfg.agent is not None:
                cfg.agent(page, tracker, lambda: marks.append(iso_now()))
            else:
                _human_loop(context, marks)

            for p in context.pages:
                try:
                    p.evaluate("() => window.__mapperFlush && window.__mapperFlush()")
                    p.wait_for_timeout(50)
                except Exception:
                    pass
            context.storage_state(path=str(out / STATE_FILE))
        finally:
            context.close()  # flushes the HAR
            browser.close()

    log.finish()
    if (out / HAR_FILE).exists():
        slim_har(out / HAR_FILE)
    for name in (HAR_FILE, STATE_FILE):
        _write_private(out / name)
    meta["ended_at"] = iso_now()
    (out / META_FILE).write_text(json.dumps(meta, indent=2) + "\n")
    return out


def config_from_args(args: argparse.Namespace) -> CaptureConfig:
    return CaptureConfig(
        start_url=args.start_url,
        task=args.task,
        run=args.run,
        markers=dict(args.marker),
        real_inputs=dict(args.input),
        kind=args.kind,
        description=args.description,
        state=Path(args.state) if args.state else None,
        out=Path(args.out),
        headless=args.headless,
        script=args.script,
        force=args.force,
    )


def main(args: argparse.Namespace) -> int:
    try:
        cfg = config_from_args(args)
        out = record(cfg)
    except (CaptureError, driver.ScriptError) as e:
        print(f"mapper capture: {e}", file=sys.stderr)
        return 2
    meta = json.loads((out / META_FILE).read_text())
    print(f"bundle written: {out}  ({len(meta['goal_marks'])} goal mark(s))")
    return 0
