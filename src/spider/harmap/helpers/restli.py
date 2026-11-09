"""Rest.li 2.0 URL-value syntax: ``(k:v,k2:List(a,b),k3:(n:x))``.

Reserved characters inside keys and string values are percent-encoded: ``% ( ) , : '`` (and spaces).
The empty string is written ``''``. We parse the *raw* (still-escaped) text so escaped brackets in
user input don't break the structure, then percent-decode each leaf.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, unquote

# Matches the card spec's `escape_restli` filter: percent-encodes `% ( ) , : '` and spaces.
_ESCAPES = {"%": "%25", "(": "%28", ")": "%29", ",": "%2C", ":": "%3A", "'": "%27", " ": "%20"}


def escape(value: str) -> str:
    return "".join(_ESCAPES.get(ch, ch) for ch in value)


class RestliError(ValueError):
    pass


def looks_like(raw: str) -> bool:
    raw = raw.strip()
    return (raw.startswith("(") or raw.startswith("List(")) and raw.endswith(")")


class _Parser:
    def __init__(self, text: str) -> None:
        self.s = text
        self.i = 0

    def error(self, msg: str) -> RestliError:
        return RestliError(f"{msg} at {self.i} in {self.s!r}")

    def peek(self, token: str) -> bool:
        return self.s.startswith(token, self.i)

    def expect(self, token: str) -> None:
        if not self.peek(token):
            raise self.error(f"expected {token!r}")
        self.i += len(token)

    def value(self) -> Any:
        if self.peek("List("):
            self.i += len("List(")
            items: list[Any] = []
            if self.peek(")"):
                self.i += 1
                return items
            while True:
                items.append(self.value())
                if self.peek(","):
                    self.i += 1
                    continue
                self.expect(")")
                return items
        if self.peek("("):
            self.i += 1
            obj: dict[str, Any] = {}
            if self.peek(")"):
                self.i += 1
                return obj
            while True:
                key = self.string()
                self.expect(":")
                obj[key] = self.value()
                if self.peek(","):
                    self.i += 1
                    continue
                self.expect(")")
                return obj
        return self.string()

    def string(self) -> str:
        if self.peek("''"):
            self.i += 2
            return ""
        start = self.i
        while self.i < len(self.s) and self.s[self.i] not in "(),:'":
            self.i += 1
        if self.i == start:
            raise self.error("expected a value")
        return unquote(self.s[start : self.i])


def parse(raw: str) -> Any:
    p = _Parser(raw.strip())
    out = p.value()
    if p.i != len(p.s):
        raise p.error("trailing characters")
    return out


def try_parse(raw: str) -> Any | None:
    """Parse ``raw`` if it is a Rest.li object/list (also when the whole value was URL-encoded once)."""
    for candidate in (raw, unquote(raw)):
        if looks_like(candidate):
            try:
                return parse(candidate)
            except RestliError:
                continue
    return None


def dumps(value: Any) -> str:
    """Inverse of ``parse`` (used for tests and for templating literal parts)."""
    if isinstance(value, dict):
        return "(" + ",".join(f"{escape(str(k))}:{dumps(v)}" for k, v in value.items()) + ")"
    if isinstance(value, list):
        return "List(" + ",".join(dumps(v) for v in value) + ")"
    text = str(value)
    return escape(text) if text else "''"


def render(value: Any, leaf, path: str = "$") -> str:
    """Like ``dumps``, but each leaf is rendered by ``leaf(jsonpath, value) -> str`` (already escaped).

    Used to rebuild a templated Rest.li value: literal leaves come back escaped, templated leaves
    come back as ``{{params.x | escape_restli}}``. Paths match ``jsonpaths.flatten``.
    """
    from spider.harmap.helpers.jsonpaths import child

    if isinstance(value, dict):
        if not value:
            return leaf(path, value)
        return (
            "("
            + ",".join(f"{escape(str(k))}:{render(v, leaf, child(path, str(k)))}" for k, v in value.items())
            + ")"
        )
    if isinstance(value, list):
        if not value:
            return leaf(path, value)
        return "List(" + ",".join(render(v, leaf, child(path, i)) for i, v in enumerate(value)) + ")"
    return leaf(path, value)


def quote_component(value: str) -> str:
    """Percent-encode for use in a URL path segment (the spec's ``urlencode`` filter)."""
    return quote(value, safe="")
