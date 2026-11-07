"""The explorer: an LLM picks a site's main tasks, does each one live in a recorded browser, and the
other runs replay what it did.

1. **Survey** (one browser session): snapshots of the start page and a few same-site pages it links to.
   The LLM picks the site's tasks from them, or describes the ones the user named.
2. **Live run** per task: the step-by-step LLM driver (agent.py) does the task with the example input
   while the capture tool records it as the ``real`` run, marking the goal right after the action that
   performs the task. A failed attempt is thrown away and redone from the start page, with the reason
   passed on (up to 3 attempts).
3. **Replays**: the ``real`` run's recorded actions (heal's replay) are re-run with fresh markers for
   ``m1`` and ``m2``, and with the next-page click added for ``page2``.
4. **Check**: the mapper's goal finder (M3) must find a goal in m1; then ``map`` builds the card.

Steps 1-3 only drive the browser and write bundles; nothing in them reads network traffic.

Safety: the survey never follows log-out/delete-style links and the driver refuses them; write tasks
(follow, post, send, add to cart...) are skipped unless ``include_writes`` is set, because exploring
one really performs it, about four times.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from mapper.bundle import iso_now
from mapper.capture import driver
from mapper.capture.replay import new_marker
from mapper.domains import hostname, registrable_domain, same_site
from mapper.explore.agent import AgentFailed, AgentRun, drive
from mapper.explore.planner import Attempt, PlanError, Planner, TaskPlan, TaskSpec
from mapper.explore.snapshot import as_text, page_snapshot
from mapper.llm import LLM

EXPLORE_FILE = "explore.json"
UNSAFE_LINK = re.compile(
    r"log\s*-?out|sign\s*-?out|log\s*off|delete|remove|deactivate|unsubscribe|close account", re.I
)
RUN_LABELS = {
    "m1": "replaying it with a made-up word",
    "m2": "replaying it with a second made-up word",
    "page2": "replaying it with the example input, then opening page 2",
}


def _say(msg: str) -> None:
    """Progress for the person watching: the same task runs several times on purpose."""
    print(f"[explore] {msg}", file=sys.stderr, flush=True)


def _first_line(e: Exception) -> str:
    return f"{type(e).__name__}: {(str(e).strip().splitlines() or [''])[0][:200]}"


@dataclass
class TaskOutcome:
    plan: TaskPlan
    status: str  # recorded | skipped | failed
    reason: str = ""
    runs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            **self.plan.spec.as_dict(),
            "status": self.status,
            "reason": self.reason,
            "runs": self.runs,
            "script": self.plan.script,
            "next_selector": self.plan.next_selector,
            "attempts": [{"actions": a.actions, "error": a.error} for a in self.plan.attempts],
        }


@dataclass
class ExploreResult:
    start_url: str
    domain_dir: Path
    pages: list[str]
    tasks: list[TaskOutcome]

    @property
    def recorded(self) -> list[TaskOutcome]:
        return [t for t in self.tasks if t.status == "recorded"]


# --- phase 1: survey ------------------------------------------------------------------


def _goto(page, tracker, url: str) -> None:
    page.goto(url, wait_until="load")
    tracker.wait_quiet(page)


def survey(page, tracker, start_url: str, extra_pages: int = 4) -> tuple[str, list[str]]:
    """Snapshots of the start page and a few same-site pages it links to (never log-out links)."""
    _say(f"looking around the site, starting at {start_url}")
    _goto(page, tracker, start_url)
    first = page_snapshot(page)
    texts, visited = [f"== Page {urlsplit(first['url']).path} ==\n{as_text(first)}"], [first["url"]]
    host = hostname(start_url)
    candidates = []
    for link in first["links"]:
        url = urljoin(first["url"], link["href"]).split("#")[0]
        if not url.startswith("http") or not same_site(hostname(url), host):
            continue
        if UNSAFE_LINK.search(link["text"] or "") or UNSAFE_LINK.search(link["href"]):
            continue
        if url not in visited and url not in candidates:
            candidates.append(url)
    for url in candidates[:extra_pages]:
        try:
            _goto(page, tracker, url)
        except Exception:  # a page that won't load is just skipped
            continue
        snap = page_snapshot(page)
        _say(f"looking at {urlsplit(snap['url']).path}")
        visited.append(snap["url"])
        texts.append(f"== Page {urlsplit(snap['url']).path} ==\n{as_text(snap)}")
    return "\n\n".join(texts), visited


# --- phase 2: the live run ------------------------------------------------------------------------


@dataclass
class Capture:
    """How every run of this exploration is recorded."""

    start_url: str
    state: Path | None
    out: Path
    headless: bool
    step_timeout_ms: int

    def config(self, spec: TaskSpec, run: str, **driver_kw):
        from mapper.capture.record import CaptureConfig

        return CaptureConfig(
            start_url=urljoin(self.start_url, spec.start_path),
            task=spec.name,
            run=run,
            kind=spec.kind,
            description=spec.description,
            state=self.state,
            out=self.out,
            headless=self.headless,
            force=True,
            **driver_kw,
        )


def live_run(
    planner: Planner, spec: TaskSpec, cap: Capture, max_attempts: int
) -> tuple[AgentRun | None, list[Attempt]]:
    """Claude does the task, recorded as the ``real`` run. Returns its run, or None after max_attempts."""
    from mapper.capture.record import record

    attempts: list[Attempt] = []
    feedback: list[str] = []
    url = urljoin(cap.start_url, spec.start_path)
    for n in range(1, max_attempts + 1):
        _say(
            f"{spec.name}: Claude is doing the task live (attempt {n} of {max_attempts}), recorded as 'real'"
        )
        done: dict[str, AgentRun] = {}

        def agent(page, tracker, mark, done=done) -> None:
            page.set_default_timeout(cap.step_timeout_ms)
            done["run"] = drive(
                page, tracker, mark, planner=planner, spec=spec, start_url=url, feedback=feedback
            )

        try:
            record(cap.config(spec, "real", real_inputs=dict(spec.inputs), agent=agent))
        except AgentFailed as e:
            actions, error = (e.run.actions if e.run else []), str(e)
        except Exception as e:  # the browser, the LLM call or the recording itself failed
            actions, error = [], _first_line(e)
        else:
            run = done["run"]
            attempts.append(Attempt(run.actions, None))
            _say(f"{spec.name}: done in {len(run.actions)} actions; final check: {run.verdict}")
            return run, attempts
        attempts.append(Attempt(actions, error))
        feedback.append(f"attempt {n}: {error}")
        _say(f"{spec.name}: attempt {n} failed: {error}")
    return None, attempts


# --- phase 3: the replays ----------------------------------------------------------------------------


def replay_runs(spec: TaskSpec, run: AgentRun, cap: Capture, task_dir: Path) -> tuple[list[dict], list[str]]:
    """Re-run the live run's recorded actions for m1, m2 (fresh markers) and page2. Returns its steps."""
    from mapper.capture.record import record
    from mapper.capture.replay import plan_bundle

    plan = plan_bundle(task_dir / "real")
    steps = plan.steps
    untyped = [k for k in spec.inputs if "{{params." + k + "}}" not in json.dumps(steps)]
    if untyped:
        raise RuntimeError(
            f"the recorded actions never type the input(s) {untyped}, so they can't be replayed"
        )
    runs: dict[str, dict] = {
        "m1": {"markers": {k: new_marker() for k in spec.inputs}, "script": steps},
        "m2": {"markers": {k: new_marker() for k in spec.inputs}, "script": steps},
    }
    if run.next_selector:
        page2 = [*steps, {"type": "click", "selector": run.next_selector}, {"type": "mark"}]
        runs["page2"] = {"real_inputs": dict(spec.inputs), "script": page2}
    for name, kw in runs.items():
        words = ", ".join((kw.get("markers") or kw.get("real_inputs") or {}).values())
        _say(f"{spec.name}: recording '{name}', {RUN_LABELS[name]}" + (f" ({words})" if words else ""))
        record(cap.config(spec, name, **kw))
    return steps, ["real", *runs]


def check_task(task_dir: Path, kind: str) -> str | None:
    from mapper.pipeline.m3_goal import GoalNotFound, find_goal
    from mapper.pipeline.runs import load_bundle

    try:
        find_goal(load_bundle(task_dir / "m1"), kind)
    except GoalNotFound as e:
        return str(e)
    return None


def explore(
    start_url: str,
    *,
    state: str | Path | None = None,
    out: str | Path = "captures",
    wanted: list[str] | None = None,
    max_tasks: int = 6,
    include_writes: bool = False,
    llm: LLM | None = None,
    headless: bool = True,
    max_attempts: int = 3,
    step_timeout_ms: int = 8000,
    extra_pages: int = 4,
) -> ExploreResult:
    from playwright.sync_api import sync_playwright

    from mapper.capture.record import _logger_js

    planner = Planner(llm or LLM())
    out = Path(out)
    state = Path(state) if state else None
    domain_dir = out / registrable_domain(hostname(start_url))
    cap = Capture(start_url, state, out, headless, step_timeout_ms)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=str(state) if state else None)
        try:
            context.add_init_script(_logger_js())
            tracker = driver.NetworkTracker(context)
            page = context.new_page()
            page.set_default_timeout(step_timeout_ms)
            site, pages = survey(page, tracker, start_url, extra_pages)
        finally:
            context.close()
            browser.close()

    specs = planner.discover(site, max_tasks, wanted)
    _say(f"the AI picked {len(specs)} task(s): {', '.join(s.name for s in specs)}")
    outcomes = []
    for spec in specs:
        plan = TaskPlan(spec)
        if spec.kind == "write" and not include_writes:
            reason = "write task: exploring it really performs it; pass --include-writes"
            _say(f"{spec.name}: skipped ({reason})")
            outcomes.append(TaskOutcome(plan, "skipped", reason))
            continue
        task_dir = domain_dir / spec.name
        shutil.rmtree(task_dir, ignore_errors=True)  # no leftover runs from an earlier exploration
        run, plan.attempts = live_run(planner, spec, cap, max_attempts)
        if run is None:
            shutil.rmtree(task_dir, ignore_errors=True)  # a failed live run leaves nothing to map
            outcomes.append(TaskOutcome(plan, "failed", plan.attempts[-1].error or "not done"))
            continue
        plan.next_selector = run.next_selector
        try:
            plan.script, runs = replay_runs(spec, run, cap, task_dir)
        except Exception as e:
            shutil.rmtree(task_dir, ignore_errors=True)
            outcomes.append(TaskOutcome(plan, "failed", f"replay failed: {_first_line(e)}"))
            continue
        problem = check_task(task_dir, spec.kind)
        outcomes.append(TaskOutcome(plan, "failed" if problem else "recorded", problem or "", runs))

    result = ExploreResult(start_url, domain_dir, pages, outcomes)
    domain_dir.mkdir(parents=True, exist_ok=True)
    (domain_dir / EXPLORE_FILE).write_text(
        json.dumps(
            {
                "start_url": start_url,
                "explored_at": iso_now(),
                "pages": pages,
                "tasks": [o.as_dict() for o in outcomes],
                "llm_calls": [
                    {"purpose": c.purpose, "ok": c.ok, "detail": c.detail} for c in planner.llm.calls
                ],
            },
            indent=2,
        )
        + "\n"
    )
    return result


def main(args: argparse.Namespace) -> int:
    from mapper import pipeline

    started = time.monotonic()
    try:
        result = explore(
            args.start_url,
            state=args.state,
            out=args.out,
            wanted=args.tasks or None,
            max_tasks=args.max_tasks,
            include_writes=args.include_writes,
            headless=not args.headed,
        )
    except PlanError as e:
        print(f"mapper explore: {e}", file=sys.stderr)
        return 2
    for o in result.tasks:
        s = o.plan.spec
        extra = f" — {o.reason}" if o.reason else ""
        print(f"{o.status:9} {s.name} ({s.kind}): {s.description}{extra}")
    print(f"explored in {time.monotonic() - started:.0f}s; details: {result.domain_dir / EXPLORE_FILE}")
    if not result.recorded:
        print("nothing recorded, so nothing to map", file=sys.stderr)
        return 1
    mapped = pipeline.map_domain(result.domain_dir, live=args.live)
    card_path, report_path = pipeline.write_outputs(mapped, result.domain_dir)
    v = mapped.validation
    print(f"card: {card_path}\nreport: {report_path}")
    print("VALID" if v.ok else f"INVALID ({len(v.errors)} error(s), see report)")
    if args.save and v.ok:
        import os

        db = args.db or os.environ.get("REGISTRY_DB", "./runtime.db")
        version = pipeline.save_card(mapped.card, db, note=f"mapper explore {args.start_url}")
        print(f"saved {mapped.card['domain']} v{version} to {db}")
    return 0 if v.ok else 1
