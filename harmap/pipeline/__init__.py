"""Mapper pipeline M1–M8: capture bundles → card + mapping report (docs/harmap-design.md, "Pipeline")."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from harmap import checks
from harmap.llm import LLM
from harmap.pipeline import pagination
from harmap.pipeline.m0_recon import Recon, recon
from harmap.pipeline.m3_goal import (
    Goal,
    GoalNotFound,
    counterpart,
    endpoint_key,
    find_goal,
    find_read_goal,
)
from harmap.pipeline.m4_provenance import Provenance, trace
from harmap.pipeline.m5_steps import StepPlan, plan_steps
from harmap.pipeline.m6_output import OutputPlan, expect_for, find_result_list, plan_output
from harmap.pipeline.m7_annotate import Annotation, annotate
from harmap.pipeline.m8_assemble import MappedAction, assemble
from harmap.pipeline.runs import Bundle, load_task
from harmap.validate import Executor, RuntimeExecutor, Validation, live_validate, static_validate

CARD_FILE = "card.json"
REPORT_FILE = "mapping_report.md"
NO_OUTPUT = {"from": None, "fields": {}, "limit": None, "next_params": None}


class MapError(RuntimeError):
    pass


@dataclass
class TaskResult:
    task: str
    runs: dict[str, Bundle]
    goal: Goal
    prov: Provenance
    plans: list[StepPlan]
    output: OutputPlan
    annotation: Annotation
    mapped: MappedAction
    real_goal: Goal | None = None
    goal_picked_by: str = "earliest (no LLM)"
    warnings: list[str] = field(default_factory=list)


def _matching_goal(bundle: Bundle | None, like: Goal, like_bundle: Bundle, kind: str) -> Goal | None:
    """The same endpoint's goal request in another run (m2, real, page2)."""
    if bundle is None:
        return None
    key = endpoint_key(like.entry, like_bundle.values)
    try:
        g = find_goal(bundle, kind)
        if endpoint_key(g.entry, bundle.values) == key:
            return g
        for c in g.candidates:
            if endpoint_key(c, bundle.values) == key:
                return Goal(c, "same endpoint as m1's goal", g.candidates, False, g.typed_at, g.mark)
    except GoalNotFound:
        pass
    entry = counterpart(bundle, like.entry, like_bundle.values)
    return Goal(entry, "same endpoint as m1's goal") if entry is not None else None


def _summary(e) -> str:
    keys = sorted(e.response_json) if isinstance(e.response_json, dict) else type(e.response_json).__name__
    query = "&".join(sorted(k.split("=")[0] for k in e.raw_query.split("&") if k))
    return f"{e.method} {e.path}?{query} → {keys}"


def _pagination(page2: Bundle, goal: Goal, m1: Bundle, real_goal: Goal | None):
    """Page 1 vs page 2 of the goal endpoint: both from the page2 run (same session and query) when it
    has both, else page 1 from the real run. Returns (Pagination | None, warnings)."""
    key = endpoint_key(goal.entry, m1.values)
    same = [e for e in page2.entries if endpoint_key(e, page2.values) == key]
    try:
        last = find_read_goal(page2, mark_index=-1).entry
        if endpoint_key(last, page2.values) != key:
            last = same[-1] if same else None
    except GoalNotFound:
        last = same[-1] if same else None
    if last is None:
        return None, ["page2 run: the goal endpoint was not requested"]
    first = next((e for e in same if e is not last and e.started < last.started), None)
    if first is None and real_goal is not None:
        first = real_goal.entry
    if first is None:
        return None, ["page2 run: no page-1 request to compare with"]
    items_path = find_result_list(goal.entry.response_json, first.response_json)
    # Bundle.fields() only normalizes the entry it is given, so page2's bundle serves both pages.
    return pagination.infer(page2, first, page2, last, items_path)


def map_task(task_dir: str | Path, llm: LLM | None = None) -> TaskResult:
    """M3–M7 for one task (one card action)."""
    llm = llm or LLM()
    task_dir = Path(task_dir)
    runs = load_task(task_dir)
    if "m1" not in runs:
        raise MapError(f"{task_dir}: no m1 run")
    m1, m2, real = runs["m1"], runs.get("m2"), runs.get("real")
    task, kind = m1.task or task_dir.name, m1.kind_hint
    warnings: list[str] = []
    if m2 is None:
        warnings.append("no m2 run: params and dependencies are not cross-checked")

    goal = find_goal(m1, kind)
    picked_by = "earliest (no LLM)"
    if goal.tie:
        idx = llm.pick_goal(task, [_summary(c) for c in goal.candidates])
        if idx is not None:
            goal.entry, picked_by = goal.candidates[idx], "the LLM"
        warnings.append(f"goal tie between {len(goal.candidates)} endpoints, picked by {picked_by}")
    m2_goal = _matching_goal(m2, goal, m1, kind)
    real_goal = _matching_goal(real, goal, m1, kind)

    prov = trace(m1, goal.entry, m2, m2_goal.entry if m2_goal else None)
    is_read = goal.entry.method in ("GET", "HEAD")
    pag = None
    if is_read and runs.get("page2") is not None:
        pag, pag_warnings = _pagination(runs["page2"], goal, m1, real_goal)
        warnings += pag_warnings
        if pag is not None and not pagination.apply(prov, pag):
            warnings.append(f"page2 run: paging field {pag.address} is not on m1's goal request; ignored")
            pag = None
    plans = plan_steps(prov, m1)
    goal_plan = next(p for p in plans if p.draft.is_goal)
    if is_read:
        output = plan_output(goal_plan, goal.entry, real_goal.entry if real_goal else None, task, llm)
    else:
        output = OutputPlan(dict(NO_OUTPUT), source="none")
    if pag is not None and output.output["from"] is not None:
        next_params: dict = {"param": pagination.PAGE_PARAM, "increment": 1, "page_size": pag.page_size}
        if pag.until_path:
            goal_plan.extract["total"] = {"from": "body", "type": "jsonpath", "expr": pag.until_path}
            next_params["until"] = f"steps.{goal_plan.id}.total"
        else:
            warnings.append("page2 run: no total-count field found; next page = a full page came back")
        output.output["limit"] = pag.page_size
        output.output["next_params"] = next_params
    for p in plans:
        p.expect = expect_for(p)
        warnings += p.warnings
    warnings += output.warnings + prov.warnings

    params = [p for p in m1.values if any(p in lab.params for d in prov.steps for lab in d.labels)]
    for p in m1.values:
        if p not in params:
            warnings.append(f"input {p!r} was typed but never reached a request")
    real_inputs = real.values if real is not None else {}
    ann = annotate(
        task=task,
        task_description=m1.meta.get("task_description", ""),
        kind_hint=kind,
        goal=goal.entry,
        params=params,
        real_inputs=real_inputs,
        output_fields=list(output.output["fields"]),
        has_unknowns=bool(prov.unknowns),
        llm=llm,
    )
    warnings += ann.warnings
    fields = dict(ann.fields)
    if pag is not None:
        fields["params"][pagination.PAGE_PARAM] = dict(pagination.PAGE_PARAM_SPEC)
    examples = fields.pop("examples")
    action = {
        "name": task,
        **{
            k: fields.pop(k) for k in ("description", "kind", "spends", "idempotent", "requires_confirmation")
        },
        "params": fields.pop("params"),
        "steps": [p.as_card() for p in plans],
        "output": output.output,
        "verify": None,
        "examples": examples,
        **fields,  # tier, when set
    }
    entries = [e for b in runs.values() for e in b.entries]
    mapped = MappedAction(action, plans, prov.uses_session, m1.meta.get("start_url", ""), entries)
    return TaskResult(task, runs, goal, prov, plans, output, ann, mapped, real_goal, picked_by, warnings)


@dataclass
class DomainResult:
    card: dict
    tasks: list[TaskResult]
    validation: Validation
    notes: list[str]
    secrets: set[str] = field(repr=False)
    llm: LLM
    captures_dir: Path
    live_requested: bool = False
    recon: Recon | None = None


def task_dirs(captures_dir: Path, task: str | None = None) -> list[Path]:
    dirs = sorted(d for d in captures_dir.iterdir() if d.is_dir() and (d / "m1" / "meta.json").exists())
    if task is not None:
        dirs = [d for d in dirs if d.name == task]
        if not dirs:
            raise MapError(f"{captures_dir}: no captured task {task!r}")
    if not dirs:
        raise MapError(f"{captures_dir}: no task with an m1 run")
    return dirs


def map_domain(
    captures_dir: str | Path,
    *,
    task: str | None = None,
    llm: LLM | None = None,
    live: bool = False,
    executor: Executor | None = None,
) -> DomainResult:
    llm = llm or LLM()
    captures_dir = Path(captures_dir)
    tasks = [map_task(d, llm) for d in task_dirs(captures_dir, task)]
    first = tasks[0]
    domain = first.runs["m1"].meta["domain"]
    base_url = first.goal.entry.base_url
    for tr in tasks[1:]:
        if tr.goal.entry.base_url != base_url:
            tr.warnings.append(f"goal is on {tr.goal.entry.base_url}, card base_url is {base_url}")
    card, notes = assemble(domain, base_url, [t.mapped for t in tasks])

    cookies: dict[str, str] = {}
    headers: list[tuple[str, str]] = []
    for tr in tasks:
        for b in tr.runs.values():
            cookies.update(b.state_cookies)
        for p in tr.plans:
            headers += p.draft.entry.request_headers
    secrets = checks.secret_values(cookies, headers)
    validation = static_validate(card, secrets)
    if live:
        session = dict(first.runs["m1"].state_cookies)
        validation.live = live_validate(card, executor or RuntimeExecutor(), session)
    every_entry = [
        e for tr in tasks for b in tr.runs.values() for e in (*b.entries, *(d for d, _ in b.dropped))
    ]
    site = recon(every_entry)
    return DomainResult(card, tasks, validation, notes, secrets, llm, captures_dir, live, site)


def write_outputs(result: DomainResult, out_dir: str | Path) -> tuple[Path, Path]:
    from harmap import report

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    card_path, report_path = out_dir / CARD_FILE, out_dir / REPORT_FILE
    card_path.write_text(json.dumps(result.card, indent=2) + "\n")
    report_path.write_text(report.render(result))
    return card_path, report_path


def save_card(card: dict, db: str | Path, note: str) -> int:
    from runtime.registry import Registry

    registry = Registry(db)
    try:
        return registry.save(card, note=note)
    finally:
        registry.close()


def main(args: argparse.Namespace) -> int:
    try:
        result = map_domain(args.captures_dir, task=args.task, live=args.live)
    except (MapError, GoalNotFound) as e:
        print(f"mapper map: {e}", file=sys.stderr)
        return 2
    card_path, report_path = write_outputs(result, args.out or args.captures_dir)
    v = result.validation
    for tr in result.tasks:
        steps = " → ".join(p.id for p in tr.plans)
        print(
            f"{tr.task}: {tr.goal.entry.method} {tr.goal.entry.path}  [{steps}]  "
            f"{len(tr.warnings)} warning(s)"
        )
    for note in result.notes:
        print(f"NOTE: {note}")
    print(f"card: {card_path}\nreport: {report_path}")
    print("VALID" if v.ok else f"INVALID ({len(v.errors)} error(s), see report)")
    if args.save:
        if not v.ok:
            print("not saved: the card is invalid", file=sys.stderr)
            return 1
        db = args.db or os.environ.get("REGISTRY_DB", "./runtime.db")
        version = save_card(result.card, db, note=f"mapper {result.captures_dir}")
        print(f"saved {result.card['domain']} v{version} to {db}")
    return 0 if v.ok else 1


def heal(
    captures_dir: str | Path,
    *,
    task: str | None = None,
    state: str | Path | None = None,
    headless: bool = True,
    live: bool = False,
    executor: Executor | None = None,
) -> tuple[DomainResult, list[str]]:
    """Re-record the whole site by replaying its recordings, then re-map everything.

    Project decision (docs/harmap-design.md "Healing"): the new card replaces the old one whole, write
    actions included, with no approval hold-back. Returns (result, replay warnings).
    """
    from harmap.capture.replay import recapture

    _, replay_warnings = recapture(captures_dir, task=task, state=state, headless=headless)
    result = map_domain(captures_dir, live=live, executor=executor)
    return result, replay_warnings


def heal_main(args: argparse.Namespace) -> int:
    try:
        result, replay_warnings = heal(
            args.captures_dir, task=args.task, state=args.state, headless=not args.headed, live=args.live
        )
    except (MapError, GoalNotFound, FileNotFoundError) as e:
        print(f"mapper heal: {e}", file=sys.stderr)
        return 2
    for w in replay_warnings:
        print(f"replay warning: {w}")
    card_path, report_path = write_outputs(result, args.out or args.captures_dir)
    v = result.validation
    print(
        f"re-recorded and re-mapped {len(result.tasks)} action(s)\ncard: {card_path}\nreport: {report_path}"
    )
    print("VALID" if v.ok else f"INVALID ({len(v.errors)} error(s), see report)")
    if args.save:
        if not v.ok:
            print("not saved: the card is invalid", file=sys.stderr)
            return 1
        db = args.db or os.environ.get("REGISTRY_DB", "./runtime.db")
        version = save_card(result.card, db, note=f"mapper heal {result.captures_dir}")
        print(f"saved {result.card['domain']} v{version} to {db} (active)")
    return 0 if v.ok else 1


def recon_main(args: argparse.Namespace) -> int:
    from harmap.pipeline.runs import load_bundle

    captures_dir = Path(args.captures_dir)
    entries = []
    for meta in sorted(captures_dir.glob("*/*/meta.json")):
        b = load_bundle(meta.parent)
        entries += [*b.entries, *(d for d, _ in b.dropped)]
    if not entries:
        print(f"mapper recon: no bundles under {captures_dir}", file=sys.stderr)
        return 2
    r = recon(entries)
    for platform, evidence in r.platforms.items():
        print(f"{platform}: {'; '.join(evidence)}")
    if not r.platforms:
        print("no known platform markers")
    for t in r.json_ld_types:
        print(f"JSON-LD type: {t}")
    for emb in r.embedded:
        print(f"embedded JSON on {emb.page}: <script id={emb.script_id!r}> keys {emb.top_keys}")
        print(f"  extract: {json.dumps(emb.extract)}")
    return 0
