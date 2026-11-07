"""M6 extractors + output: ``expect`` for every step, and the action's ``output``.

* ``expect``: the captured status and content type, and ``paths`` = the structural parent of each
  extracted JSONPath (``$.elements`` for ``$.elements[0].id``), so a missing item is ``EMPTY`` while a
  missing structure is ``PAGE_CHANGED``.
* ``output``: the list in the goal response that grew with results (m1's marker search is empty, the
  real run's is not) becomes an ``items`` extract on the goal step; 3–8 item fields are picked by the
  LLM, or deterministically: all scalar leaves of the first item, capped at 8.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from mapper import jsonpaths
from mapper.llm import LLM
from mapper.pipeline.m1_load import Entry
from mapper.pipeline.m5_steps import StepPlan, snake

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
    if (real_goal or m1_goal).response_kind == "stream":
        return plan_stream_output(goal, m1_goal, real_goal, task, llm)
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


# --- streamed answers (data rows, one per line; see m1_load) -----------------------------------------
# The results are the rows that appear in the real run's answer and not in the marker run's, sharing one
# layout. A result is often spread over several rows that point to each other, so rows are compared after
# their pointers are filled in (runtime.extract.resolve_rows, the same code the executor runs for
# ``resolve: "rows"``). The card picks the result rows by the text they all start with.

ROW_LINE = re.compile(r"(?m)^([0-9a-f]{1,8}):(\[.*\]|\{.*\})$")
TOKENISH = re.compile(r"^[A-Za-z0-9+/=_.:-]{16,}$")  # ids, hashes, tracking tokens: nothing for a person
URLISH = re.compile(r"^(https?://|/)\S+$")
MAX_PREFIX = 300
MAX_CANDIDATES = 40


def _rows(entry: Entry | None) -> list[tuple[str, str, Any]]:
    """``(row id, row text, row value)`` for each JSON row of a streamed answer."""
    out = []
    for m in ROW_LINE.finditer((entry.response_body if entry else None) or ""):
        try:
            out.append((m.group(1), m.group(2), json.loads(m.group(2))))
        except ValueError:
            continue
    return out


def _shape(v: Any, depth: int = 4) -> str:
    if depth == 0:
        return "."
    if isinstance(v, dict):
        return "{" + ",".join(f"{k}:{_shape(v[k], depth - 1)}" for k in sorted(v)) + "}"
    if isinstance(v, list):
        return "[" + ",".join(_shape(x, depth - 1) for x in v[:6]) + "]"
    return "s" if isinstance(v, str) else type(v).__name__


def _readable(v: Any) -> bool:
    """A value a person would read: words, a number, a link (not a pointer, id or style token)."""
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if not isinstance(v, str) or not v.strip() or len(v) > 200 or v.startswith("$"):
        return False
    if URLISH.match(v):
        return True
    if TOKENISH.match(v):
        return False
    return " " in v.strip() or v[:1].isupper() or any(c.isdigit() for c in v)


def varying_fields(items: list[Any]) -> list[tuple[str, list[Any]]]:
    """Paths (relative to an item) whose readable value differs between results, in document order."""
    leaves = [dict(jsonpaths.flatten(i)) for i in items]
    counts = Counter(p for leaf in leaves for p in leaf)
    out = []
    for path, _ in jsonpaths.flatten(items[0]):
        if counts[path] < 0.6 * len(items):
            continue
        vals = [leaf[path] for leaf in leaves if path in leaf]
        if len({json.dumps(v) for v in vals}) >= 2 and all(_readable(v) for v in vals):
            out.append((path, vals))
    return out


def _common_prefix(texts: list[str]) -> str:
    prefix = texts[0]
    for t in texts[1:]:
        n = 0
        while n < min(len(prefix), len(t)) and prefix[n] == t[n]:
            n += 1
        prefix = prefix[:n]
    prefix = prefix[:MAX_PREFIX]
    return prefix[:-1] if prefix.endswith("\\") else prefix  # never end inside an escape


def _fragments(row: Any) -> list[str]:
    """JSON text pieces a row contains: ``"key":value`` for short values, and ``"key":`` alone."""
    out = []
    for path, value in jsonpaths.flatten(row):
        key = re.findall(r"\.([A-Za-z_$][\w$-]*)$|\['([^']+)'\]$", path)
        if not key:
            continue
        k = next(x for x in key[-1] if x)
        for ascii_only in (False, True):
            if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 80:
                out.append(
                    json.dumps(k) + ":" + json.dumps(value, separators=(",", ":"), ensure_ascii=ascii_only)
                )
        out.append(json.dumps(k) + ":")
    return list(dict.fromkeys(out))


def row_selector(group: list[str], others: list[str]) -> str | None:
    """A pattern matching exactly the ``group`` row texts and none of ``others``: their common start,
    plus (if that start isn't enough) a piece of text every group row contains and no other row does."""
    prefix = _common_prefix(group)
    if len(prefix) < 8:
        return None
    head = r"(?m)^[0-9a-f]{1,8}:(" + re.escape(prefix)
    rivals = [t for t in others if t.startswith(prefix)]
    if not rivals:
        return head + r".*)$"
    for piece in sorted(_fragments(json.loads(group[0])), key=len, reverse=True):
        if all(piece in t for t in group) and not any(piece in t for t in rivals):
            return head + r"(?=.*" + re.escape(piece) + r").*)$"
    return None


@dataclass
class ResultRows:
    pattern: str
    items: list[Any]  # the result rows, pointers filled in
    fields: list[tuple[str, list[Any]]]


def find_result_rows(
    empty: list[tuple[str, str, Any]], full: list[tuple[str, str, Any]]
) -> ResultRows | None:
    """The real run's result rows: the layout that grew most from the marker run, richest in readable
    values once pointers are filled in, and a pattern that matches exactly those rows."""
    from runtime.extract import resolve_rows

    by_id = {rid: v for rid, _, v in full}
    before = Counter(_shape(v) for _, _, v in empty)
    groups: dict[str, list[tuple[str, str, Any]]] = {}
    for row in full:
        groups.setdefault(_shape(row[2]), []).append(row)
    ranked = []
    for shape, rows in groups.items():
        growth = len(rows) - before.get(shape, 0)
        if growth < 2:
            continue
        items = [resolve_rows(v, by_id) for _, _, v in rows]
        fields = varying_fields(items)
        if fields:
            ranked.append(((len(fields), growth), rows, items, fields))
    ranked.sort(key=lambda r: r[0], reverse=True)
    for _, rows, items, fields in ranked:  # the richest group that a pattern can single out
        group = {id(r) for r in rows}
        others = [t for r in full if id(r) not in group for t in [r[1]]] + [t for _, t, _ in empty]
        pattern = row_selector([t for _, t, _ in rows], others)
        if pattern is not None:
            return ResultRows(pattern, items, fields)
    return None


def _fallback_names(fields: list[tuple[str, list[Any]]]) -> dict[str, str]:
    """Without the LLM: each result's link and its text, in page order."""
    out: dict[str, str] = {}
    links = [p for p, vals in fields if all(isinstance(v, str) and URLISH.match(v) for v in vals)]
    if links:
        out["link"] = links[0]
    for n, (path, _) in enumerate([f for f in fields if f[0] not in links][: MAX_FIELDS - len(out)], 1):
        out[f"text_{n}"] = path
    return out


def plan_stream_output(
    goal: StepPlan, m1_goal: Entry, real_goal: Entry | None, task: str, llm: LLM
) -> OutputPlan:
    none = {"from": None, "fields": {}, "limit": None, "next_params": None}
    found = find_result_rows(_rows(m1_goal) if real_goal else [], _rows(real_goal or m1_goal))
    if found is None:
        warning = "streamed answer: no set of result rows grew between the marker run and the real run"
        return OutputPlan(none, source="none", warnings=[warning])
    goal.extract["items"] = {
        "from": "body",
        "type": "regex",
        "expr": found.pattern,
        "parse": "json",
        "resolve": "rows",
    }
    candidates = found.fields[:MAX_CANDIDATES]
    named = llm.name_output_fields(task, [(p, vals[:3]) for p, vals in candidates])
    fields = named or _fallback_names(candidates)
    output = {
        "from": f"steps.{goal.id}.items",
        "fields": fields,
        "limit": len(found.items),
        "next_params": None,
    }
    return OutputPlan(output, None, found.items[0], "llm" if named else "fallback")
