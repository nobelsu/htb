"""Card placeholders: ``{{namespace.path}}``, ``{{x | filter1 | filter2}}`` (spec: Placeholders)."""

from __future__ import annotations

import re

FILTERS = frozenset({"urlencode", "strip_quotes", "int", "escape_restli"})  # plus offset:N
OFFSET_RE = re.compile(r"^offset:([1-9][0-9]*)$")
PLACEHOLDER_RE = re.compile(r"\{\{(.*?)\}\}")


def ph(expr: str, *filters: str | None) -> str:
    parts = [expr, *(f for f in filters if f)]
    return "{{" + " | ".join(parts) + "}}"


def param(name: str, *filters: str | None) -> str:
    return ph(f"params.{name}", *filters)


def cookie(name: str, *filters: str | None) -> str:
    return ph(f"session.cookies.{name}", *filters)


def step(step_id: str, key: str, *filters: str | None) -> str:
    return ph(f"steps.{step_id}.{key}", *filters)


BASE_URL = ph("base_url")


def parse(inner: str) -> tuple[str, list[str]]:
    """``" params.q | urlencode "`` → ``("params.q", ["urlencode"])``."""
    parts = [p.strip() for p in inner.split("|")]
    return parts[0], parts[1:]


def find_all(text: str) -> list[tuple[str, list[str]]]:
    return [parse(m.group(1)) for m in PLACEHOLDER_RE.finditer(text)]


def is_only_placeholder(text: str) -> bool:
    m = PLACEHOLDER_RE.fullmatch(text.strip())
    return m is not None


def filter_ok(name: str) -> bool:
    return name in FILTERS or OFFSET_RE.match(name) is not None
