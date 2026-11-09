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

from spider.mappers.harmap.helpers import jsonpaths
from spider.mappers.harmap.infer.load import Entry
from spider.mappers.harmap.infer.steps import StepPlan, snake
from spider.mappers.harmap.llm import LLM

MAX_FIELDS = 30  # everything relevant to the action goes back to the agent


def structural_parent(path: str) -> str:
    """``$.a.items[0].id`` → ``$.a.items``; ``$.items[*]`` → ``$.items``; ``$.token`` → ``$.token``."""
    m = re.search(r"\[(\d+|\*)\]", path)
    return path[: m.start()] if m else path


def expect_for(plan: StepPlan) -> dict:
    entry = plan.draft.entry
    status = [entry.status] if 200 <= entry.status < 400 else [200]
    content_type = {"json": "json", "html": "html", "text": "text"}.get(entry.response_kind, "any")
    mime = entry.response_mime.split(";", 1)[0].strip().lower()
    if content_type == "json" and not (mime == "application/json" or mime.endswith("+json")):
        content_type = "any"  # JSON under a vendor type (application/vnd.x+json+2.1): don't insist on it
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


def find_screen_node(doc: Any, screen: list[str]) -> tuple[str, bool] | None:
    """Reads without input: the JSON list (or object) that holds the most of what the page showed.
    Returns its path and whether it is a list of results."""
    texts = {t for t in screen if len(t) >= 3}
    best: tuple[int, int, str, bool] | None = None
    for path, node in jsonpaths.walk(doc):
        if not isinstance(node, (list, dict)) or path == "$":
            continue
        held = {str(v) for _, v in jsonpaths.flatten(node) if str(v) in texts}
        is_list = isinstance(node, list) and len(node) > 1 and all(isinstance(i, dict) for i in node)
        if held and (isinstance(node, dict) or is_list):
            key = (len(held), -len(path), path, is_list)  # most held; then the most specific node
            if best is None or key[:2] > best[:2]:
                best = key
    return (best[2], best[3]) if best else None


def plan_output(
    goal: StepPlan,
    m1_goal: Entry,
    real_goal: Entry | None,
    task: str,
    llm: LLM,
    description: str = "",
    screen: list[str] | None = None,
    compare_runs: bool = True,
) -> OutputPlan:
    """Adds the ``items`` extract to ``goal`` and returns the action's ``output``. The results are what
    grew from the made-up-word run to the real run (``compare_runs``); when nothing was typed, or nothing
    grew (one object, like a company page), the part of the answer that holds what the page showed
    (``screen``, recorded by the explorer)."""
    if (real_goal or m1_goal).response_kind == "stream":
        return plan_stream_output(goal, m1_goal, real_goal, task, llm, description, screen, compare_runs)
    full = (real_goal or m1_goal).response_json
    if full is None and (real_goal or m1_goal).response_kind == "html":
        return plan_html_output(goal, real_goal or m1_goal, screen)
    if full is None:
        return OutputPlan(
            {"from": None, "fields": {}, "limit": None, "next_params": None},
            source="none",
            warnings=["goal response is not JSON: no output extracted"],
        )
    single, items_path = False, None
    why = "no result list grew between the marker run and the real run"
    if compare_runs:
        items_path = find_result_list(m1_goal.response_json, full)
    if items_path is None and screen:
        node = find_screen_node(full, screen)
        items_path, single = (node[0], not node[1]) if node else (None, False)
        why = "no part of the answer holds the text the page showed"
    if items_path is None:
        return OutputPlan(
            {"from": None, "fields": {}, "limit": None, "next_params": None}, source="none", warnings=[why]
        )
    expr = items_path if single else f"{items_path}[*]"
    goal.extract["items"] = {"from": "body", "type": "jsonpath", "expr": expr}
    found = jsonpaths.find(items_path, full)[0]
    items = [found] if single else found
    sample = items[0] if items else None

    fields: dict[str, str] = {}
    source = "none"
    if isinstance(sample, dict):
        candidates = scalar_leaves(sample)
        picked = llm.pick_output_fields(task, sample, candidates, description)
        source = "llm" if picked else "fallback"
        fields = field_names(picked or candidates[:MAX_FIELDS])
    output = {"from": f"steps.{goal.id}.items", "fields": fields, "limit": len(items), "next_params": None}
    return OutputPlan(output, items_path, sample, source)


# --- server-rendered pages (the data is only in the HTML) ---------------------------------------------
# No JSON to point into, so the result is the page's visible text, one string per element, in page order
# ("Microsoft", "Software Development", "10K+ employees", ...). The agent reading it is an LLM: a list of
# texts is enough for it, and it doesn't break when the site renames its (often generated) CSS classes.

PAGE_TEXT = r"<(?:h[1-6]|p|span|a|dd|dt|li|td|th|button|div)\b[^>]*>\s*([^<>\s][^<>]{0,300}?)\s*</"
MAX_PAGE_TEXTS = 200


def plan_html_output(goal: StepPlan, entry: Entry, screen: list[str] | None) -> OutputPlan:
    texts = re.findall(PAGE_TEXT, entry.response_body or "")
    if not texts:
        return OutputPlan(
            {"from": None, "fields": {}, "limit": None, "next_params": None},
            source="none",
            warnings=["goal response is HTML with no text in it: no output extracted"],
        )
    goal.extract["items"] = {"from": "body", "type": "regex", "expr": PAGE_TEXT}
    shown = sum(any(t in x for x in texts) for t in screen or [])
    note = f"page text ({len(texts)} texts; {shown} of {len(screen or [])} shown on the page)"
    output = {
        "from": f"steps.{goal.id}.items",
        "fields": {},
        "limit": min(len(texts), MAX_PAGE_TEXTS),
        "next_params": None,
    }
    return OutputPlan(output, None, texts[:5], "page_text", warnings=[f"output is the {note}"])


# --- streamed answers (data rows, one per line; see m1_load) -----------------------------------------
# The results are the rows that appear in the real run's answer and not in the marker run's, sharing one
# layout. A result is often spread over several rows that point to each other, so rows are compared after
# their pointers are filled in (spider.run.request.extract.resolve_rows, the same code the executor runs for
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
    """Paths (relative to an item) whose readable value differs between results, in document order (for
    a single result, every readable value), then values like "1,323,457 followers" found by pattern."""
    leaves = [dict(jsonpaths.flatten(i)) for i in items]
    counts = Counter(p for leaf in leaves for p in leaf)
    out, seen = [], set()
    for path, _ in jsonpaths.flatten(items[0]):
        if counts[path] < 0.6 * len(items):
            continue
        vals = [leaf[path] for leaf in leaves if path in leaf]
        varies = len(items) == 1 or len({json.dumps(v) for v in vals}) >= 2
        if varies and all(_readable(v) for v in vals) and json.dumps(vals) not in seen:
            seen.add(json.dumps(vals))  # the same values at two spots: offer them once
            out.append((path, vals))
    return out + count_fields(items)


# "1,323,457 followers", "· 12K subscribers", "48 employees": a number and the same word in every result.
COUNT = re.compile(r"^\W*([0-9][0-9,.]*)\s*([KkMmBb]?)\W*\s*([A-Za-z][A-Za-z ]{1,30}?)\s*$")


def count_fields(items: list[Any]) -> list[tuple[str, list[Any]]]:
    """Counts whose position shifts between results (an extra line moves them), found by what they look
    like: a JSONPath filter on the value at any depth, so each result's own count is found wherever it is."""
    units: Counter[str] = Counter()
    for item in items:
        found = set()
        for _, v in jsonpaths.flatten(item):
            m = COUNT.match(v.replace("\u00a0", " ")) if isinstance(v, str) else None
            if m:
                found.add(m.group(3).strip().lower())
        units.update(found)
    out = []
    for unit, n in units.items():
        if n < max(1, 0.6 * len(items)) or not re.fullmatch(r"[a-z ]+", unit):
            continue
        pattern = "[0-9][0-9,.]*[^A-Za-z0-9]*[KMBkmb]?[^A-Za-z0-9]*" + unit
        expr = f'$..[?(@ =~ "{pattern}")]'
        vals = [m[0] for m in (jsonpaths.find(expr, i) for i in items) if m]
        if len(vals) >= 0.6 * len(items):
            out.append((expr, vals))
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
    from spider.run.request.extract import resolve_rows

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


def split_joined(fields: dict[str, str]) -> dict[str, str]:
    """A value kept at several spots (``A | B``) becomes ``name``, ``name_2``…: one JSONPath per field, as
    the card wants; the agent reads whichever one a result has."""
    out: dict[str, str] = {}
    for name, expr in fields.items():
        parts = expr.split(" | ") if " | " in expr else [expr]
        for n, part in enumerate(parts, 1):
            key = name if n == 1 else f"{name}_{n}"
            while key in out or (n > 1 and key in fields):
                key += "_alt"
            out[key] = part
    return out


def _fallback_names(fields: list[tuple[str, list[Any]]]) -> dict[str, str]:
    """Without the LLM: each result's link and its text, in page order."""
    out: dict[str, str] = {}
    links = [p for p, vals in fields if all(isinstance(v, str) and URLISH.match(v) for v in vals)]
    if links:
        out["link"] = links[0]
    for n, (path, _) in enumerate([f for f in fields if f[0] not in links][: MAX_FIELDS - len(out)], 1):
        out[f"text_{n}"] = path
    return out


def find_screen_rows(full: list[tuple[str, str, Any]], screen: list[str]) -> ResultRows | None:
    """Reads without input: the row layout that holds the most of what the page showed (one row or many),
    once pointers are filled in, and a pattern that matches exactly those rows."""
    from spider.run.request.extract import resolve_rows

    texts = {t for t in screen if len(t) >= 3}
    by_id = {rid: v for rid, _, v in full}
    groups: dict[str, list[tuple[str, str, Any]]] = {}
    for row in full:
        groups.setdefault(_shape(row[2]), []).append(row)
    ranked = []
    for rows in groups.values():
        items = [resolve_rows(v, by_id) for _, _, v in rows]
        held = {str(v) for i in items for _, v in jsonpaths.flatten(i) if str(v) in texts}
        if held:
            ranked.append((len(held), rows, items))
    ranked.sort(key=lambda r: r[0], reverse=True)
    for _, rows, items in ranked:
        group = {id(r) for r in rows}
        pattern = row_selector([t for _, t, _ in rows], [r[1] for r in full if id(r) not in group])
        fields = varying_fields(items)
        if pattern is not None and fields:
            return ResultRows(pattern, items, fields)
    return None


def plan_stream_output(
    goal: StepPlan,
    m1_goal: Entry,
    real_goal: Entry | None,
    task: str,
    llm: LLM,
    description: str = "",
    screen: list[str] | None = None,
    compare_runs: bool = True,
) -> OutputPlan:
    none = {"from": None, "fields": {}, "limit": None, "next_params": None}
    found = None
    if compare_runs:
        found = find_result_rows(_rows(m1_goal) if real_goal else [], _rows(real_goal or m1_goal))
    if found is None and screen:
        found = find_screen_rows(_rows(real_goal or m1_goal), screen)
    if found is None:
        warning = (
            "streamed answer: no rows hold the text the page showed"
            if screen
            else "streamed answer: no set of result rows grew between the marker run and the real run"
        )
        return OutputPlan(none, source="none", warnings=[warning])
    goal.extract["items"] = {
        "from": "body",
        "type": "regex",
        "expr": found.pattern,
        "parse": "json",
        "resolve": "rows",
    }
    candidates = found.fields[:MAX_CANDIDATES]
    named = llm.name_output_fields(task, [(p, vals[:3]) for p, vals in candidates], description)
    fields = split_joined(named or _fallback_names(candidates))
    output = {
        "from": f"steps.{goal.id}.items",
        "fields": fields,
        "limit": len(found.items),
        "next_params": None,
    }
    return OutputPlan(output, None, found.items[0], "llm" if named else "fallback")
