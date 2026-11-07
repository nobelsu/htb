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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from mapper.bundle import iso_now
from mapper.capture import driver
from mapper.capture.replay import new_marker
from mapper.domains import hostname, registrable_domain, same_site
from mapper.explore.agent import AgentFailed, AgentRun, EmptyResult, drive
from mapper.explore.planner import Attempt, PlanError, Planner, TaskPlan, TaskSpec
from mapper.explore.snapshot import as_text, page_snapshot
from mapper.llm import LLM

EXPLORE_FILE = "explore.json"
# The explorer's own Claude calls (pick tasks, each action, the final check); the mapper's steps keep theirs.
EXPLORER_MODEL, EXPLORER_EFFORT = "claude-opus-5-5", "high"
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
    driver.settle(page)


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
    max_browsers: int = 8
    _browsers: threading.BoundedSemaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._browsers = threading.BoundedSemaphore(max(1, self.max_browsers))

    def record(self, cfg):
        """Record one run, never with more than ``max_browsers`` browsers open at once."""
        from mapper.capture.record import record

        with self._browsers:
            return record(cfg)

    def config(self, spec: TaskSpec, run: str, start_url: str | None = None, **driver_kw):
        from mapper.capture.record import CaptureConfig

        return CaptureConfig(
            start_url=start_url or urljoin(self.start_url, spec.start_path),
            task=spec.name,
            run=run,
            kind=spec.kind,
            description=spec.description,
            state=self.state,
            out=self.out,
            headless=self.headless,
            force=True,
            step_timeout_ms=self.step_timeout_ms,
            **driver_kw,
        )


MAX_EXAMPLES = 3  # different example inputs to try when the site finds nothing (not driving failures)


def run_task(
    planner: Planner, spec: TaskSpec, cap: Capture, task_dir: Path, max_attempts: int
) -> tuple[TaskPlan, list[str]]:
    """Live run (Claude, recorded as ``real``), then the replays. Returns the plan and the runs recorded
    (none if it never worked; ``plan.attempts`` then says why).

    A failed try counts toward ``max_attempts``: Claude's run failed, or it worked but replaying it with
    made-up words broke (a step that only exists when there are results). A try done right where the
    site found nothing for the example (EmptyResult) does not count: the example is swapped for a new
    one, up to MAX_EXAMPLES. ``spec.inputs`` ends up holding the example that worked.
    """
    plan = TaskPlan(spec)
    feedback: list[str] = []
    tried = [dict(spec.inputs)]
    failures = 0
    url = urljoin(cap.start_url, spec.start_path)
    while failures < max_attempts:
        n = len(plan.attempts) + 1
        shutil.rmtree(task_dir, ignore_errors=True)  # every try starts clean
        shown = f" with {spec.inputs}" if spec.inputs else ""
        _say(f"{spec.name}: Claude is doing the task live{shown} (try {n}), recorded as 'real'")
        done: dict[str, AgentRun] = {}

        def agent(page, tracker, mark, done=done) -> None:
            page.set_default_timeout(cap.step_timeout_ms)
            done["run"] = drive(
                page, tracker, mark, planner=planner, spec=spec, start_url=url, feedback=feedback
            )

        try:
            cap.record(cap.config(spec, "real", real_inputs=dict(spec.inputs), agent=agent))
        except EmptyResult as e:
            plan.attempts.append(Attempt(e.run.actions if e.run else [], str(e)))
            _say(f"{spec.name}: try {n} worked, but the site found nothing for {spec.inputs}")
            if len(tried) >= MAX_EXAMPLES:
                break
            try:
                spec.inputs.update(planner.new_example(spec, tried))
            except PlanError:
                break
            tried.append(dict(spec.inputs))
            feedback.append(
                f"try {n}: the site found nothing for {tried[-2]}; the example is now {spec.inputs}"
            )
            continue
        except AgentFailed as e:
            actions, error = (e.run.actions if e.run else []), str(e)
        except Exception as e:  # the browser, the LLM call or the recording itself failed
            actions, error = [], _first_line(e)
        else:
            run = done["run"]
            _say(f"{spec.name}: done in {len(run.actions)} actions; final check: {run.verdict}")
            try:
                plan.script, runs = replay_runs(spec, run, cap, task_dir)
            except ReplayFailed as e:
                actions, error = run.actions, f"the live run worked, but {e}"
            except Exception as e:
                actions, error = run.actions, f"the live run worked, but the replays failed: {_first_line(e)}"
            else:
                plan.attempts.append(Attempt(run.actions, None))
                plan.next_selector = run.next_selector
                return plan, runs
        failures += 1
        plan.attempts.append(Attempt(actions, error))
        feedback.append(f"try {n}: {error}")
        _say(f"{spec.name}: try {n} failed ({failures} of {max_attempts} allowed): {error}")
    shutil.rmtree(task_dir, ignore_errors=True)  # nothing usable to map
    return plan, []


# --- phase 3: the replays ----------------------------------------------------------------------------


class ReplayFailed(RuntimeError):
    pass


def replay_start(steps: list[dict], start_url: str) -> tuple[str, list[dict]]:
    """Replays start where the live run began working: the page loads before its first click, key or
    typing were only passed through, so they are skipped and the last of them is the start page."""
    url, i = start_url, 0
    while i < len(steps) and steps[i]["type"] in ("navigate", "goto") and "{{" not in steps[i]["url"]:
        url, i = urljoin(start_url, steps[i]["url"]), i + 1
    return url, steps[i:]


def replay_runs(spec: TaskSpec, run: AgentRun, cap: Capture, task_dir: Path) -> tuple[list[dict], list[str]]:
    """Re-run the live run's recorded actions for m1, m2 (fresh markers) and page2, all at once.
    Returns the replayed steps and the runs recorded."""
    from mapper.capture.replay import plan_bundle

    steps = plan_bundle(task_dir / "real").steps
    untyped = [k for k in spec.inputs if "{{params." + k + "}}" not in json.dumps(steps)]
    if untyped:
        raise ReplayFailed(
            f"its recorded actions never type the input(s) {untyped}, so they can't be replayed"
        )
    start, steps = replay_start(steps, urljoin(cap.start_url, spec.start_path))
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
    with ThreadPoolExecutor(max_workers=len(runs)) as pool:
        jobs = {
            name: pool.submit(cap.record, cap.config(spec, name, start_url=start, **kw))
            for name, kw in runs.items()
        }
    broken = {}
    for name, job in jobs.items():
        try:
            job.result()
        except Exception as e:
            broken[name] = str(e) if str(e) else type(e).__name__
    if broken:
        detail = "; ".join(f"{name}: {why.splitlines()[0][:200]}" for name, why in broken.items())
        hint = ""
        if set(broken) & {"m1", "m2"}:
            hint = (
                " That step only works when there are results (the made-up words find nothing): set "
                "categories or filters before you type and submit the input."
            )
        raise ReplayFailed(f"replaying it broke ({detail}).{hint}")
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
    parallel: int = 4,
    max_browsers: int = 8,
) -> ExploreResult:
    from playwright.sync_api import sync_playwright

    from mapper.capture.record import _logger_js

    planner = Planner(llm or LLM(model=EXPLORER_MODEL, effort=EXPLORER_EFFORT))
    out = Path(out)
    state = Path(state) if state else None
    domain_dir = out / registrable_domain(hostname(start_url))
    cap = Capture(start_url, state, out, headless, step_timeout_ms, max_browsers)

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

    def one(spec: TaskSpec) -> TaskOutcome:
        if spec.kind == "write" and not include_writes:
            reason = "write task: exploring it really performs it; pass --include-writes"
            _say(f"{spec.name}: skipped ({reason})")
            return TaskOutcome(TaskPlan(spec), "skipped", reason)
        try:
            plan, runs = run_task(planner, spec, cap, domain_dir / spec.name, max_attempts)
        except Exception as e:  # one task's crash must not stop the others
            return TaskOutcome(TaskPlan(spec), "failed", f"explorer error: {_first_line(e)}")
        if not runs:
            return TaskOutcome(plan, "failed", plan.attempts[-1].error or "not done")
        problem = check_task(domain_dir / spec.name, spec.kind)
        return TaskOutcome(plan, "failed" if problem else "recorded", problem or "", runs)

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        outcomes = list(pool.map(one, specs))  # tasks run side by side; results stay in task order

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
            parallel=args.parallel,
            max_browsers=args.max_browsers,
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
