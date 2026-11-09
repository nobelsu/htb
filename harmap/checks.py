"""Mapper-side card checks that ``schema.validate_card`` cannot do.

The save-time rules from the spec live in the runtime's ``schema`` package (and ``tools/check_card.py``); the
mapper calls both. This module only adds what needs the capture itself:

* **No live secrets in the card.** No string in the card may contain a session cookie value or a
  csrf/auth/token header value seen during capture (a constant mistakenly templated literally would
  ship someone's session inside the card).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import Any

SENSITIVE_NAME = re.compile(r"cookie|csrf|xsrf|auth|token|session|secret|password|api[-_]?key|bearer", re.I)
MIN_SECRET_LEN = 8


def strings(obj: Any, where: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from strings(v, f"{where}.{k}" if where else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from strings(v, f"{where}[{i}]")
    elif isinstance(obj, str):
        yield where, obj


def secret_values(cookies: dict[str, str], headers: Iterable[tuple[str, str]]) -> set[str]:
    """Values that must never appear in a card: session cookies and sensitive request headers."""
    out = set()
    for value in cookies.values():
        for v in (value, value.strip('"')):
            if len(v) >= MIN_SECRET_LEN:
                out.add(v)
    for name, value in headers:
        if SENSITIVE_NAME.search(name) and len(value) >= MIN_SECRET_LEN:
            out.add(value)
    return out


def leaked_secrets(card: dict, secrets: set[str]) -> list[str]:
    errors = []
    for where, text in strings(card):
        if any(s in text for s in secrets):
            errors.append(f"{where}: contains a live session value from the capture")
    return errors
