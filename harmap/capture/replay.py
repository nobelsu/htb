"""Re-record a site by replaying its saved recordings (``actions.jsonl``) in a fresh browser.

Used by healing: every run of every task is replayed headless with the scripted driver. Marker runs
(m1/m2) get fresh markers; real runs keep their real inputs. Each bundle's own ``state.json`` is reused
as the login session unless ``--state`` is given. New bundles are written to a staging folder and only
swapped in when every replay succeeded; the previous bundles are kept in ``<domain>.prev/``.
"""

from __future__ import annotations

import json
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from harmap.bundle import ACTIONS_FILE, MARKER_RUNS, META_FILE, RUNS, STATE_FILE, parse_iso

# A navigation this soon after a click/Enter was caused by it, so replaying the click replays it.
CLICK_NAV_WINDOW_S = 2.0
_LETTERS = "bcdfghjklmnpqrstvwxz"


def new_marker() -> str:
    """A fresh, unlikely-to-collide marker like ``qzvx4821``."""
    return "".join(secrets.choice(_LETTERS) for _ in range(4)) + f"{secrets.randbelow(10**4):04d}"


@dataclass
class ReplayPlan:
    task: str
    run: str
    meta: dict
    steps: list[dict]
    state: Path | None
    warnings: list[str] = field(default_factory=list)


def _relative(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def script_from_actions(actions: list[dict], meta: dict) -> tuple[list[dict], list[str]]:
    """Driver steps that redo a recorded run. Typed inputs become ``{{params.NAME}}``."""
    values = {**(meta.get("markers") or {}), **(meta.get("real_inputs") or {})}
    marks = sorted(parse_iso(t) for t in meta.get("goal_marks") or [])
    events = sorted(actions, key=lambda a: a.get("t", ""))
    start = _relative(meta.get("start_url", ""))
    steps: list[dict] = []
    warnings: list[str] = []
    last_ui = None
    seen_start = False

    def flush_marks(until) -> None:
        while marks and (until is None or marks[0] <= until):
            steps.append({"type": "mark"})
            marks.pop(0)

    for ev in events:
        t = parse_iso(ev["t"])
        flush_marks(t)
        kind = ev.get("type")
        if kind == "navigate":
            caused = last_ui is not None and (t - last_ui).total_seconds() <= CLICK_NAV_WINDOW_S
            url = _relative(ev["url"])
            if not seen_start and url == start:
                seen_start = True  # record() opens start_url itself
            elif not caused:
                steps.append({"type": "navigate", "url": url})
        elif kind == "input":
            value = str(ev.get("value", ""))
            if value == "[redacted]":
                warnings.append(f"skipped a secret input on {ev.get('selector')} (pass a logged-in --state)")
                continue
            for name, v in values.items():
                if v:
                    value = value.replace(v, "{{params." + name + "}}")
            steps.append({"type": "input", "selector": ev["selector"], "value": value})
        elif kind in ("click", "key"):
            step = {"type": kind, "selector": ev["selector"]}
            if kind == "key":
                step["key"] = ev.get("key", "Enter")
            steps.append(step)
            last_ui = t
    flush_marks(None)
    return steps, warnings


def plan_bundle(run_dir: Path) -> ReplayPlan:
    meta = json.loads((run_dir / META_FILE).read_text())
    actions_path = run_dir / ACTIONS_FILE
    actions = (
        [json.loads(ln) for ln in actions_path.read_text().splitlines() if ln.strip()]
        if actions_path.exists()
        else []
    )
    steps, warnings = script_from_actions(actions, meta)
    state = run_dir / STATE_FILE
    return ReplayPlan(meta["task"], meta["run"], meta, steps, state if state.exists() else None, warnings)


def recapture(
    domain_dir: str | Path,
    *,
    task: str | None = None,
    state: str | Path | None = None,
    headless: bool = True,
) -> tuple[list[ReplayPlan], list[str]]:
    """Replay every recording under ``captures/<domain>/`` (or one task). Returns (plans, warnings)."""
    from harmap.capture.record import CaptureConfig, record

    domain_dir = Path(domain_dir)
    domain = domain_dir.name
    tasks = sorted(d for d in domain_dir.iterdir() if d.is_dir() and (d / "m1" / META_FILE).exists())
    if task is not None:
        tasks = [d for d in tasks if d.name == task]
    plans = [plan_bundle(t / run) for t in tasks for run in RUNS if (t / run / META_FILE).exists()]
    if not plans:
        raise FileNotFoundError(f"{domain_dir}: nothing to replay")

    staging = domain_dir.parent / f".heal-{domain}"
    shutil.rmtree(staging, ignore_errors=True)
    sessions = staging / "_sessions"
    sessions.mkdir(parents=True)
    warnings: list[str] = []
    try:
        for plan in plans:
            session = Path(state) if state else None
            if session is None and plan.state is not None:
                session = sessions / f"{plan.task}-{plan.run}.state.json"
                shutil.copy(plan.state, session)
            meta = plan.meta
            markers = {k: new_marker() for k in meta.get("markers") or {}} if plan.run in MARKER_RUNS else {}
            record(
                CaptureConfig(
                    start_url=meta["start_url"],
                    task=plan.task,
                    run=plan.run,
                    markers=markers,
                    real_inputs=dict(meta.get("real_inputs") or {}),
                    kind=meta.get("kind_hint", "read"),
                    description=meta.get("task_description", ""),
                    state=session,
                    out=staging,
                    headless=headless,
                    script=plan.steps,
                    force=True,
                )
            )
            warnings += [f"{plan.task}/{plan.run}: {w}" for w in plan.warnings]
        # every replay worked: swap the new bundles in, keep the old ones as <domain>.prev/
        backup = domain_dir.parent / f"{domain}.prev"
        for t in {p.task for p in plans}:
            new = staging / domain / t
            if not new.exists():
                raise RuntimeError(f"replay of {t} wrote no bundles under {staging / domain}")
            shutil.rmtree(backup / t, ignore_errors=True)
            backup.mkdir(exist_ok=True)
            shutil.move(str(domain_dir / t), str(backup / t))
            shutil.move(str(new), str(domain_dir / t))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return plans, warnings
