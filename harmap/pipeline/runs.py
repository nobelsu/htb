"""Loading capture bundles into memory: one ``Bundle`` per run, grouped per task."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from harmap.bundle import ACTIONS_FILE, HAR_FILE, META_FILE, RUNS, STATE_FILE, parse_iso
from harmap.pipeline.m1_load import Entry, filter_entries, load_har
from harmap.pipeline.m2_normalize import Field, normalize


@dataclass
class Bundle:
    """One capture run. ``state_cookies`` holds live session values: never print or log them."""

    meta: dict
    entries: list[Entry]  # M1-kept, time-ordered
    dropped: list[tuple[Entry, str]] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    state_cookies: dict[str, str] = field(default_factory=dict, repr=False)
    path: Path | None = None
    _fields: dict[int, list[Field]] = field(default_factory=dict, repr=False)

    @property
    def run(self) -> str:
        return self.meta.get("run", "")

    @property
    def task(self) -> str:
        return self.meta.get("task", "")

    @property
    def kind_hint(self) -> str:
        return self.meta.get("kind_hint", "read")

    @property
    def values(self) -> dict[str, str]:
        """param name → the input typed in this run (marker for m1/m2, real input otherwise)."""
        return dict(self.meta.get("markers") or {}) or dict(self.meta.get("real_inputs") or {})

    @property
    def goal_marks(self) -> list[datetime]:
        return [parse_iso(t) for t in self.meta.get("goal_marks") or []]

    @property
    def started_at(self) -> datetime | None:
        t = self.meta.get("started_at")
        return parse_iso(t) if t else None

    def fields(self, entry: Entry) -> list[Field]:
        key = id(entry)
        if key not in self._fields:
            self._fields[key] = normalize(entry)
        return self._fields[key]

    def field_map(self, entry: Entry) -> dict[str, Field]:
        return {f.address: f for f in self.fields(entry)}

    def ui_events(self, *types: str) -> list[tuple[datetime, dict]]:
        out = []
        for a in self.actions:
            if (not types or a.get("type") in types) and a.get("t"):
                out.append((parse_iso(a["t"]), a))
        return sorted(out, key=lambda x: x[0])


def load_bundle(path: str | Path) -> Bundle:
    path = Path(path)
    meta = json.loads((path / META_FILE).read_text())
    result = filter_entries(load_har(path / HAR_FILE), meta["domain"])
    actions = []
    if (path / ACTIONS_FILE).exists():
        actions = [
            json.loads(line) for line in (path / ACTIONS_FILE).read_text().splitlines() if line.strip()
        ]
    cookies: dict[str, str] = {}
    if (path / STATE_FILE).exists():
        state = json.loads((path / STATE_FILE).read_text())
        cookies = {c["name"]: c["value"] for c in state.get("cookies", [])}
    return Bundle(meta, result.kept, result.dropped, actions, cookies, path)


def load_task(task_dir: str | Path) -> dict[str, Bundle]:
    """All runs captured for one task: ``{"m1": Bundle, "m2": ..., "real": ..., "page2": ...}``."""
    task_dir = Path(task_dir)
    return {run: load_bundle(task_dir / run) for run in RUNS if (task_dir / run / META_FILE).exists()}
