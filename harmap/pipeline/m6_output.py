"""M6 extractors + output: ``expect`` for every step, and the action's ``output``.

* ``expect``: the captured status and content type, and ``paths`` = the structural parent of each
  extracted JSONPath (``$.elements`` for ``$.elements[0].id``), so a missing item is ``EMPTY`` while a
  missing structure is ``PAGE_CHANGED``.
* ``output``: the list in the goal response that grew with results (m1's marker search is empty, the
  real run's is not) becomes an ``items`` extract on the goal step; 3–8 item fields are picked by the
  LLM, or deterministically: all scalar leaves of the first item, capped at 8.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from harmap import jsonpaths
from harmap.llm import LLM
from harmap.pipeline.m1_load import Entry
from harmap.pipeline.m5_steps import StepPlan, snake

MAX_FIELDS = 8


def structural_parent(path: str) -> str:
    """``$.a.items[0].id`` → ``$.a.items``; ``$.items[*]`` → ``$.items``; ``$.token`` → ``$.token``."""
    m = re.search(r"\[(\d+|\*)\]", path)
    return path[: m.start()] if m else path


def expect_for(plan: StepPlan) -> dict:
    entry = plan.draft.entry
    status = [entry.status] if 200 <= entry.status < 400 else [200]
    content_type = {"json": "json", "html": "html", "text": "text"}.get(entry.response_kind, "any")
    paths: list[str] = []
    doc = entry.response_json
    for ex in plan.extract.values():
        if ex["from"] == "body" and ex["type"] == "jsonpath":
            parent = structural_parent(ex["expr"])
            if parent != "$" and parent not in paths and doc is not None and jsonpaths.find(parent, doc):
                paths.append(parent)
    return {"status": status, "content_type": content_type, "paths": paths}


def _lists(doc: Any) -> dict[str, list]:
    return {p: v for p, v in jsonpaths.walk(doc) if isinstance(v, list)}


def find_result_list(empty_doc: Any, full_doc: Any) -> str | None:
    """JSONPath of the list that grew most from the marker run to the real run."""
    before = _lists(empty_doc) if empty_doc is not None else {}
    best: tuple[int, int, int, str] | None = None
    for path, items in _lists(full_doc).items():
        growth = len(items) - len(before.get(path, []))
        if growth <= 0:
            continue
        objects = sum(isinstance(i, dict) for i in items)
        key = (growth, objects, -len(path), path)
        if best is None or key > best:
            best = key
    return best[3] if best else None


def scalar_leaves(item: Any) -> list[str]:
    return [p for p, v in jsonpaths.flatten(item) if not isinstance(v, (dict, list))]


def field_names(paths: list[str]) -> dict[str, str]:
    """``["$.name", "$.logo.url"]`` → ``{"name": "$.name", "logo_url": "$.logo.url"}``."""
    out: dict[str, str] = {}
    for p in paths:
        base = (
            snake(re.sub(r"\[\d+\]", "", p).replace("$", "").replace("['", ".").replace("']", "")) or "value"
        )
        name, n = base, 2
        while name in out:
            name, n = f"{base}_{n}", n + 1
        out[name] = p
    return out


@dataclass
class OutputPlan:
    output: dict
    items_path: str | None = None
    sample: Any = None
    source: str = "fallback"  # "llm" | "fallback" | "none"
    warnings: list[str] = field(default_factory=list)


def plan_output(goal: StepPlan, m1_goal: Entry, real_goal: Entry | None, task: str, llm: LLM) -> OutputPlan:
    """Adds the ``items`` extract to ``goal`` and returns the action's ``output``."""
    full = (real_goal or m1_goal).response_json
    if full is None:
        return OutputPlan(
            {"from": None, "fields": {}, "limit": None, "next_params": None},
            source="none",
            warnings=["goal response is not JSON: no output extracted"],
        )
    items_path = find_result_list(m1_goal.response_json, full)
    if items_path is None:
        return OutputPlan(
            {"from": None, "fields": {}, "limit": None, "next_params": None},
            source="none",
            warnings=["no result list grew between the marker run and the real run"],
        )
    goal.extract["items"] = {"from": "body", "type": "jsonpath", "expr": f"{items_path}[*]"}
    items = jsonpaths.find(items_path, full)[0]
    sample = items[0] if items else None

    fields: dict[str, str] = {}
    source = "none"
    if isinstance(sample, dict):
        candidates = scalar_leaves(sample)
        picked = llm.pick_output_fields(task, sample, candidates)
        source = "llm" if picked else "fallback"
        fields = field_names(picked or candidates[:MAX_FIELDS])
    output = {"from": f"steps.{goal.id}.items", "fields": fields, "limit": len(items), "next_params": None}
    return OutputPlan(output, items_path, sample, source)
