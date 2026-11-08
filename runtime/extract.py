"""JSONPath / regex / CSS extraction and output projection (CARD_SPEC.md "Extraction")."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from jsonpath_ng.ext.parser import parse as _jp_parse

from schema.card import Extract

_NO_JSON = object()


class ExtractError(ValueError):
    """The response cannot be read the way the mapping says (structure problem → PAGE_CHANGED)."""


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
    out: list[Any] = []
    for raw in found:
        try:
            parsed = json.loads(raw)
        except ValueError as e:
            raise ExtractError(f"regex match is not JSON: {e}") from None
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
