"""JSONPath / regex / CSS extraction and output projection (CARD_SPEC.md "Extraction")."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from jsonpath_ng.ext.parser import parse as _jp_parse

from spider.card.model import Extract

_NO_JSON = object()


class ExtractError(ValueError):
    """The response cannot be read the way the mapping says (structure problem → PAGE_CHANGED)."""


# Streamed data rows (CARD_SPEC.md "Extraction", Streamed rows): one `<hex id>:<payload>` per line.
STREAM_ROW = re.compile(r"(?m)^([0-9a-f]{1,8}):(\[.*\]|\{.*\}|\".*\")$")
ROW_POINTER = re.compile(r"^\$[L@]?([0-9a-f]{1,8})((?::[^:]+)*)$")
MAX_FILL_INS = 5000
_MISSING = object()


def stream_rows(text: str) -> dict[str, Any]:
    """The JSON rows of a streamed answer, by row id (rows that aren't JSON, like `I` rows, are left out)."""
    rows: dict[str, Any] = {}
    for m in STREAM_ROW.finditer(text):
        try:
            rows[m.group(1)] = json.loads(m.group(2))
        except ValueError:
            continue
    return rows


def _at(value: Any, keys: list[str]) -> Any:
    for k in keys:
        if k == "props" and isinstance(value, list) and len(value) == 4 and value[0] == "$":
            value = value[3]  # a React element ["$", type, key, props]
        elif isinstance(value, dict) and k in value:
            value = value[k]
        elif isinstance(value, list) and k.isdigit() and int(k) < len(value):
            value = value[int(k)]
        else:
            return _MISSING
    return value


def resolve_rows(value: Any, rows: Mapping[str, Any]) -> Any:
    """``value`` with its pointers to other rows filled in from ``rows`` (CARD_SPEC.md, Streamed rows)."""
    budget = [MAX_FILL_INS]

    def fill(v: Any, seen: frozenset[str]) -> Any:
        if isinstance(v, dict):
            return {k: fill(x, seen) for k, x in v.items()}
        if isinstance(v, list):
            return [fill(x, seen) for x in v]
        if isinstance(v, str) and v.startswith("$") and budget[0] > 0:
            m = ROW_POINTER.match(v)
            if m and m.group(1) in rows and m.group(1) not in seen:
                target = _at(rows[m.group(1)], [k for k in m.group(2).split(":") if k])
                if target is not _MISSING:
                    budget[0] -= 1
                    return fill(target, seen | {m.group(1)})
        return v

    return fill(value, frozenset())


@lru_cache(maxsize=1024)
def _compiled(expr: str) -> Any:
    return _jp_parse(expr)


def jsonpath_all(expr: str, data: Any) -> list[Any]:
    return [m.value for m in _compiled(expr).find(data)]


@dataclass
class ResponseView:
    """What an extract can read from one response."""

    text: str
    headers: Mapping[str, str]  # lowercased names
    cookies: Mapping[str, str]  # the execution's jar after this response
    _json: Any = _NO_JSON

    def json(self) -> Any:
        if self._json is _NO_JSON:
            try:
                self._json = json.loads(self.text)
            except ValueError as e:
                raise ExtractError(f"response body is not JSON: {e}") from None
        return self._json


def css_items(html: str, selector: str) -> list[dict[str, Any]]:
    """Each element ``selector`` matches in ``html``: its text (whitespace collapsed) and its link (its own
    href, else the first link inside it, else null)."""
    from selectolax.lexbor import LexborHTMLParser

    out: list[dict[str, Any]] = []
    for node in LexborHTMLParser(html).css(selector):
        link = node if node.tag == "a" else node.css_first("a[href]")
        href = link.attributes.get("href") if link is not None else None
        out.append({"text": " ".join((node.text(separator=" ") or "").split()), "href": href})
    return out


_GOOD_CLASS = re.compile(r"^[A-Za-z][\w-]{1,40}$")
_BAD_CLASS = re.compile(r"\d{3,}|^css-|^sc-|^_")


def html_selector(html: str, texts: list[str], need: float = 0.7) -> str | None:
    """One CSS selector for the elements of ``html`` that show ``texts`` (the deepest element whose text is,
    or starts with, each; lifted to its link), covering at least ``need`` of them with the fewest matches.
    The same rules the recorder uses in the browser, applied to the HTML a plain request gets: a site's
    scripts can restructure the page after it loads, so a selector from the live page may miss here."""
    from selectolax.lexbor import LexborHTMLParser

    doc = LexborHTMLParser(html)
    for junk in doc.css("script, style, noscript, template"):
        junk.decompose()
    if doc.body is None:
        return None
    nodes = [n for n in doc.body.traverse() if n.tag and not n.tag.startswith("-")]

    def norm(text: str | None) -> str:
        return " ".join((text or "").split())

    found = []
    for t in texts[:10]:
        want = norm(t)
        if not want:
            continue
        best = None
        for n in nodes:
            if norm(n.text()) == want:
                best = n  # the deepest one
        if best is None and len(want) >= 4:
            for n in nodes:
                if norm(n.text()).startswith(want):
                    best = n
        if best is None:
            continue
        link: Any = best
        while link is not None and link.tag != "a":
            link = link.parent
        if link is not None and link.attributes.get("href") and norm(link.text()).startswith(want):
            best = link
        found.append(best)
    if not found:
        return None

    def simple(n: Any) -> str:
        classes = [c for c in (n.attributes.get("class") or "").split() if _GOOD_CLASS.match(c)]
        return n.tag + "".join("." + c for c in [c for c in classes if not _BAD_CLASS.search(c)][:2])

    candidates: set[str] = set()
    for n in found:
        own = simple(n)
        candidates.add(own)
        a = n.parent
        for _ in range(4):
            if a is None or a.tag in ("body", "html"):
                break
            anc = simple(a)
            if "." in anc:
                candidates.add(f"{anc} {own}")
            a = a.parent
    ids = {n.mem_id for n in found}
    least = len(found) if len(found) <= 2 else math.ceil(len(found) * need)
    best_sel: tuple[int, int, int, str] | None = None  # (-covered, matches, -length, selector)
    for sel in candidates:
        try:
            matches = doc.css(sel)
        except Exception:
            continue
        covered = sum(1 for m in matches if m.mem_id in ids)
        if covered < least:
            continue
        key = (-covered, len(matches), -len(sel), sel)
        if best_sel is None or key < best_sel:
            best_sel = key
    return best_sel[3] if best_sel else None


def run_extract(ex: Extract, view: ResponseView) -> list[Any]:
    """Every extract produces a list of matches."""
    if ex.type == "css":
        return css_items(view.text, ex.expr)
    if ex.type == "jsonpath":
        if ex.from_ == "body":
            source: Any = view.json()
        elif ex.from_ == "headers":
            source = dict(view.headers)
        else:
            source = dict(view.cookies)
        return jsonpath_all(ex.expr, source)

    if ex.from_ == "body":
        text = view.text
    elif ex.from_ == "headers":
        text = "\n".join(f"{k}: {v}" for k, v in view.headers.items())
    else:
        text = "\n".join(f"{k}={v}" for k, v in view.cookies.items())
    rx = re.compile(ex.expr)
    found = [m.group(1) if rx.groups else m.group(0) for m in rx.finditer(text)]
    if ex.parse != "json":
        return found
    rows = stream_rows(text) if ex.resolve == "rows" else None
    out: list[Any] = []
    for raw in found:
        try:
            parsed = json.loads(raw)
        except ValueError as e:
            raise ExtractError(f"regex match is not JSON: {e}") from None
        if rows is not None:
            parsed = resolve_rows(parsed, rows)
        if ex.then is None:
            out.append(parsed)
        else:
            out.extend(jsonpath_all(ex.then, parsed))
    return out


def project(items: list[Any], fields: Mapping[str, str]) -> list[Any]:
    """Apply `output.fields` to each item. A field with no match is None. No fields → items as-is."""
    if not fields:
        return list(items)
    out: list[dict[str, Any]] = []
    for item in items:
        row: dict[str, Any] = {}
        for name, expr in fields.items():
            matches = jsonpath_all(expr, item) if isinstance(item, (dict, list)) else []
            row[name] = matches[0] if matches else None
        out.append(row)
    return out
