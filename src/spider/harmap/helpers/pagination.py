"""Pagination from the ``page2`` run (docs/harmap.md, "Pagination").

Diff the goal request of ``page2`` (page 2) against ``real`` (page 1). A numeric field that went
``0 → N`` is a result offset: template ``{{params.page | offset:N}}``, ``page_size: N``. A field that went
``1 → 2`` is a page number: template ``{{params.page}}``, ``page_size`` = items on page 1. Either way the
action gets an optional integer ``page`` param (default 1) and ``output.next_params``; a total-count field
in the response (same on both pages, at least the page's item count) becomes ``until``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from spider.harmap.helpers import jsonpaths, placeholders
from spider.harmap.helpers.runs import Bundle
from spider.harmap.pipeline.load import Entry
from spider.harmap.pipeline.normalize import leaf_fields
from spider.harmap.pipeline.provenance import Provenance

PAGE_PARAM = "page"
TOTAL_NAMES = [
    re.compile(r"^total$", re.I),
    re.compile(r"^(total_?count|total_?results|total_?hits|num_?found|num_?results|hits_?total)$", re.I),
    re.compile(r"^(count|total_?items|result_?count)$", re.I),
]
MAX_PAGE_VALUE = 1_000_000


@dataclass
class Pagination:
    address: str  # the goal field that pages, e.g. query.start
    mode: str  # "offset" | "page"
    step: int  # N for offset:N
    page_size: int
    until_path: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def template(self) -> str:
        return placeholders.param(PAGE_PARAM, f"offset:{self.step}" if self.mode == "offset" else None)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _changed_numbers(
    real: Bundle, page1: Entry, page2_bundle: Bundle, page2: Entry
) -> list[tuple[str, int, int]]:
    f1 = {f.address: f for f in leaf_fields(real.fields(page1))}
    f2 = {f.address: f for f in leaf_fields(page2_bundle.fields(page2))}
    out = []
    for address, a in f1.items():
        b = f2.get(address)
        if b is None or a.location in ("header", "cookie"):
            continue
        x, y = _as_int(a.value), _as_int(b.value)
        if x is not None and y is not None and x != y and max(x, y) < MAX_PAGE_VALUE:
            out.append((address, x, y))
    return out


def find_total(page1: Any, page2: Any, items_on_page: int) -> str | None:
    """JSONPath of a total-results count: same on both pages and at least the page's item count."""
    if page1 is None:
        return None
    for names in TOTAL_NAMES:
        for path, value in jsonpaths.flatten(page1):
            key = re.findall(r"\.([A-Za-z_][A-Za-z0-9_]*)$|\['([^']+)'\]$", path)
            name = next((a or b for a, b in key), "") if key else ""
            n = _as_int(value)
            if not name or n is None or not names.match(name) or n < items_on_page:
                continue
            if page2 is not None and jsonpaths.find(path, page2) != [value]:
                continue
            return path
    return None


def infer(
    real: Bundle, page1: Entry, page2_bundle: Bundle, page2: Entry, items_path: str | None
) -> tuple[Pagination | None, list[str]]:
    """Pagination from the real (page 1) and page2 (page 2) goal requests. Returns (pagination, warnings)."""
    changed = _changed_numbers(real, page1, page2_bundle, page2)
    if not changed:
        return None, ["page2 run: no numeric request field changed between page 1 and page 2"]
    warnings = []
    if len(changed) > 1:
        warnings.append(
            f"page2 run: several numeric fields changed {changed}; using the first offset-like one"
        )
    items = jsonpaths.find(items_path, page1.response_json) if items_path and page1.response_json else []
    n_items = len(items[0]) if items and isinstance(items[0], list) else 0
    for address, x, y in changed:
        if x == 0 and y > 0:
            pag = Pagination(address, "offset", y, y)
            break
        if x == 1 and y == 2 and n_items:
            pag = Pagination(address, "page", 1, n_items)
            break
    else:
        address, x, y = changed[0]
        return None, [
            *warnings,
            f"page2 run: {address} went {x} → {y}; not an offset from 0 or a page number",
        ]
    if pag.mode == "offset" and n_items and n_items != pag.page_size:
        warnings.append(f"page 1 returned {n_items} items but the offset moved by {pag.page_size}")
    pag.until_path = find_total(page1.response_json, page2.response_json, n_items)
    pag.notes.append(f"pagination: {address} went {x} → {y} between page 1 and page 2")
    return pag, warnings


def apply(prov: Provenance, pag: Pagination) -> bool:
    """Turn the paging field on m1's goal into the ``page`` param. False if the field isn't there."""
    lab = prov.goal.label(pag.address)
    if lab is None:
        return False
    lab.provenance = "param"
    lab.params = (PAGE_PARAM,)
    lab.template = pag.template
    lab.filter = f"offset:{pag.step}" if pag.mode == "offset" else None
    lab.encoding = "raw"
    lab.notes = list(pag.notes)
    return True


PAGE_PARAM_SPEC = {"type": "integer", "required": False, "default": 1, "description": "1-based page number"}
