"""The explorer: an LLM decides a site's main tasks and writes a script for each; the browser runs them.

1. **Survey + plan** (one browser session): snapshot the start page and a few main pages, let the LLM pick
   the site's tasks, then for each task let it write a click/type script and dry-run it. A failed dry run
   goes back to the LLM with the error and a snapshot of the page where it failed (up to 3 attempts). For
   paginated reads the LLM also picks the next-page control.
2. **Record**: the capture tool records m1/m2 (fresh markers), real (the example input) and page2 per task.
3. **Check**: each recording must contain a goal request (M3); then ``map`` turns it all into a card.

Safety: the survey never follows log-out/delete-style links, and write tasks (follow, post, send, add to
cart...) are skipped unless ``include_writes`` is set, because exploring one really performs it,
several times.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from mapper.bundle import iso_now
from mapper.capture import driver
from mapper.capture.replay import new_marker
from mapper.domains import hostname, registrable_domain, same_site
from mapper.explore.planner import Attempt, PlanError, Planner, TaskPlan, TaskSpec
from mapper.explore.snapshot import as_text, page_snapshot
from mapper.llm import LLM
from mapper.pipeline.m3_goal import value_forms

EXPLORE_FILE = "explore.json"
UNSAFE_LINK = re.compile(
    r"log\s*-?out|sign\s*-?out|log\s*off|delete|remove|deactivate|unsubscribe|close account", re.I
)
MAX_FEEDBACK_CHARS = 4000


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
            "attempts": [{"script": a.script, "error": a.error} for a in self.plan.attempts],
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


# --- phase 1: survey and plan ------------------------------------------------------------------


def _goto(page, tracker, url: str) -> None:
    page.goto(url, wait_until="load")
    tracker.wait_quiet(page)


def survey(page, tracker, start_url: str, extra_pages: int = 4) -> tuple[str, list[str]]:
    """Snapshots of the start page and a few same-site pages it links to (never log-out links)."""
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
        visited.append(snap["url"])
        texts.append(f"== Page {urlsplit(snap['url']).path} ==\n{as_text(snap)}")
    return "\n\n".join(texts), visited


def dry_run(page, tracker, url: str, steps: list[dict], spec: TaskSpec) -> str | None:
    """Run the script once (no recording). Returns an error message, or None if it worked."""
    seen: list[tuple[str, str, str, str]] = []

    def on_request(req) -> None:
        seen.append((req.method, req.url, req.post_data or "", req.resource_type))

    page.on("request", on_request)
    try:
        _goto(page, tracker, url)
        driver.run_script(
            page, steps, start_url=url, params=spec.inputs, tracker=tracker, on_mark=lambda: None
        )
    except Exception as e:
        first_line = (str(e).strip().splitlines() or [type(e).__name__])[0]
        return f"a step failed: {type(e).__name__}: {first_line[:300]}"
    finally:
        page.remove_listener("request", on_request)
    host = hostname(url)
    calls = [s for s in seen if s[3] in ("fetch", "xhr", "document") and same_site(hostname(s[1]), host)]
    if spec.inputs:
        forms = {f for v in spec.inputs.values() for f in value_forms(v).values() if f}
        if not any(f in s[1] or f in s[2] for s in calls for f in forms):
            return "the script ran, but no request to the site carried the typed input (did it submit?)"
    if spec.kind == "write" and not any(s[0] not in ("GET", "HEAD") for s in calls):
        return "the script ran, but it sent no POST/PUT/DELETE, so the change was never submitted"
    return None


def plan_task(planner: Planner, page, tracker, start_url: str, spec: TaskSpec, max_attempts: int) -> TaskPlan:
    plan = TaskPlan(spec)
    url = urljoin(start_url, spec.start_path)
    feedback: list[str] = []
    for n in range(1, max_attempts + 1):
        _goto(page, tracker, url)
        try:
            steps = planner.write_script(spec, as_text(page_snapshot(page)), feedback)
        except PlanError as e:
            plan.attempts.append(Attempt([], f"invalid script: {e}"))
            feedback.append(f"attempt {n}: invalid script: {e}")
            continue
        error = dry_run(page, tracker, url, steps, spec)
        plan.attempts.append(Attempt(steps, error))
        if error is None:
            plan.script = steps
            if spec.paginates and spec.kind == "read":
                plan.next_selector = _next_control(planner, page, spec)
            return plan
        where = as_text(page_snapshot(page))[:MAX_FEEDBACK_CHARS]
        feedback.append(f"attempt {n} ({json.dumps(steps)}): {error}\nThe page at that point:\n{where}")
    return plan


def _next_control(planner: Planner, page, spec: TaskSpec) -> str | None:
    try:
        sel = planner.find_next(spec, as_text(page_snapshot(page)))
    except PlanError:
        return None
    if sel and page.query_selector(sel) is not None:
        return sel
    return None


# --- phase 2 and 3: record and check -----------------------------------------------------------


def record_task(plan: TaskPlan, start_url: str, state: Path | None, out: Path, headless: bool) -> list[str]:
    from mapper.capture.record import CaptureConfig, record

    spec, steps = plan.spec, plan.script or []
    url = urljoin(start_url, spec.start_path)
    runs: dict[str, tuple[dict, dict, list[dict]]] = {
        "m1": ({k: new_marker() for k in spec.inputs}, {}, steps),
        "m2": ({k: new_marker() for k in spec.inputs}, {}, steps),
        "real": ({}, dict(spec.inputs), steps),
    }
    if plan.next_selector:
        page2 = [
            *steps,
            {"type": "click", "selector": plan.next_selector},
            {"type": "wait", "ms": 300},
            {"type": "mark"},
        ]
        runs["page2"] = ({}, dict(spec.inputs), page2)
    for run, (markers, real, script) in runs.items():
        record(
            CaptureConfig(
                start_url=url,
                task=spec.name,
                run=run,
                markers=markers,
                real_inputs=real,
                kind=spec.kind,
                description=spec.description,
                state=state,
                out=out,
                headless=headless,
                script=script,
                force=True,
            )
        )
    return list(runs)


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

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=str(state) if state else None)
        try:
            context.add_init_script(_logger_js())
            tracker = driver.NetworkTracker(context)
            page = context.new_page()
            page.set_default_timeout(step_timeout_ms)
            site, pages = survey(page, tracker, start_url, extra_pages)
            specs = planner.discover(site, max_tasks, wanted)
            outcomes = []
            for spec in specs:
                if spec.kind == "write" and not include_writes:
                    reason = "write task: exploring it really performs it; pass --include-writes"
                    outcomes.append(TaskOutcome(TaskPlan(spec), "skipped", reason))
                    continue
                plan = plan_task(planner, page, tracker, start_url, spec, max_attempts)
                if plan.script is None:
                    outcomes.append(
                        TaskOutcome(plan, "failed", plan.attempts[-1].error or "no working script")
                    )
                else:
                    outcomes.append(TaskOutcome(plan, "planned"))
        finally:
            context.close()
            browser.close()

    for o in outcomes:
        if o.status != "planned":
            continue
        try:
            o.runs = record_task(o.plan, start_url, state, out, headless)
        except Exception as e:
            o.status, o.reason = "failed", f"recording failed: {type(e).__name__}: {str(e)[:200]}"
            continue
        problem = check_task(domain_dir / o.plan.spec.name, o.plan.spec.kind)
        o.status, o.reason = ("failed", problem) if problem else ("recorded", "")

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
