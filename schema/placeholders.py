"""Placeholder and signal grammar shared by the validator and the runtime."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

PLACEHOLDER_RE = re.compile(r"\{\{(.*?)\}\}")
FILTER_NAMES = frozenset({"urlencode", "strip_quotes", "int", "offset", "escape_restli"})
_OFFSET_RE = re.compile(r"offset:(\d+)")
SIGNAL_RE = re.compile(r"^(status:\d{3}|redirect:/\S*|body_contains:.+)$")

Namespace = Literal["base_url", "params", "session", "steps"]


class PlaceholderError(ValueError):
    pass


@dataclass(frozen=True)
class Filter:
    name: str
    arg: int | None = None


@dataclass(frozen=True)
class Placeholder:
    raw: str  # the full "{{ ... }}" text
    namespace: Namespace
    path: tuple[str, ...]  # e.g. ("query",) or ("cookies", "JSESSIONID") or ("search", "total")
    filters: tuple[Filter, ...]

    @property
    def uses_escape_restli(self) -> bool:
        return any(f.name == "escape_restli" for f in self.filters)


def parse_filter(text: str) -> Filter:
    text = text.strip()
    if text.startswith("offset"):
        m = _OFFSET_RE.fullmatch(text)
        if not m:
            raise PlaceholderError(f"bad offset filter {text!r} (expected offset:N)")
        return Filter("offset", int(m.group(1)))
    if text not in FILTER_NAMES:
        raise PlaceholderError(f"unknown filter {text!r}")
    return Filter(text)


def parse_placeholder(inner: str, raw: str | None = None) -> Placeholder:
    parts = [p.strip() for p in inner.split("|")]
    ref, filter_texts = parts[0], parts[1:]
    filters = tuple(parse_filter(f) for f in filter_texts)
    raw = raw if raw is not None else "{{" + inner + "}}"
    if ref == "base_url":
        return Placeholder(raw, "base_url", (), filters)
    ns, _, rest = ref.partition(".")
    segs = tuple(rest.split(".")) if rest else ()
    if ns == "params":
        if len(segs) != 1 or not segs[0]:
            raise PlaceholderError(f"bad params ref {ref!r}")
        return Placeholder(raw, "params", segs, filters)
    if ns == "session":
        if len(segs) != 2 or segs[0] != "cookies" or not segs[1]:
            raise PlaceholderError(f"bad session ref {ref!r} (expected session.cookies.NAME)")
        return Placeholder(raw, "session", segs, filters)
    if ns == "steps":
        if len(segs) != 2 or not all(segs):
            raise PlaceholderError(f"bad steps ref {ref!r} (expected steps.ID.KEY)")
        return Placeholder(raw, "steps", segs, filters)
    raise PlaceholderError(f"unknown namespace {ns!r} in {ref!r}")


def find_placeholders(text: str) -> list[Placeholder]:
    """Parse every placeholder in `text`. Raises PlaceholderError on the first bad one."""
    return [parse_placeholder(m.group(1), m.group(0)) for m in PLACEHOLDER_RE.finditer(text)]


def parse_step_ref(ref: str) -> tuple[str, str]:
    """Parse 'steps.ID.KEY' (used by output.from and next_params.until)."""
    parts = ref.split(".")
    if len(parts) != 3 or parts[0] != "steps" or not parts[1] or not parts[2]:
        raise PlaceholderError(f"bad step ref {ref!r} (expected steps.ID.KEY)")
    return parts[1], parts[2]


def is_valid_signal(signal: str) -> bool:
    return bool(SIGNAL_RE.match(signal))
