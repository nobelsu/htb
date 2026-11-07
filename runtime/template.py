"""Placeholder resolver and filters (CARD_SPEC.md "Placeholders"). Deliberately not a template engine."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from schema.card import Request
from schema.placeholders import PLACEHOLDER_RE, Filter, Placeholder, find_placeholders

RESTLI_SAFE = "(),:'%"


class TemplateError(ValueError):
    """A placeholder could not be resolved (bad filter input etc.)."""


class MissingParam(TemplateError):
    """An optional param with no value and no default: the enclosing key is dropped."""


class MissingCookie(TemplateError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"session cookie {name!r} is not set")


class StepValueError(TemplateError):
    """A `steps.ID.X` reference did not have exactly one match."""

    def __init__(self, step_id: str, key: str, count: int) -> None:
        self.step_id, self.key, self.count = step_id, key, count
        super().__init__(f"steps.{step_id}.{key} has {count} matches, expected exactly 1")


@dataclass
class Context:
    base_url: str
    params: Mapping[str, Any]
    cookies: Mapping[str, str] = field(default_factory=dict)
    # (step_id, extract_key) -> every match of that extract
    steps: Mapping[tuple[str, str], list[Any]] = field(default_factory=dict)


# ---- filters ---------------------------------------------------------------


def escape_restli(value: str) -> str:
    out = value.replace("%", "%25")  # first, so the escapes below are not double-encoded
    for ch, enc in (("(", "%28"), (")", "%29"), (",", "%2C"), (":", "%3A"), ("'", "%27"), (" ", "%20")):
        out = out.replace(ch, enc)
    return out


def to_int(value: Any) -> int:
    if isinstance(value, bool):
        raise TemplateError(f"cannot convert boolean {value!r} to int")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            pass
    raise TemplateError(f"cannot convert {value!r} to int")


def apply_filter(value: Any, f: Filter) -> Any:
    if f.name == "urlencode":
        return quote(to_text(value), safe="")
    if f.name == "strip_quotes":
        s = to_text(value)
        return s[1:-1] if len(s) >= 2 and s[0] == '"' and s[-1] == '"' else s
    if f.name == "int":
        return to_int(value)
    if f.name == "offset":
        assert f.arg is not None
        return (to_int(value) - 1) * f.arg
    if f.name == "escape_restli":
        return escape_restli(to_text(value))
    raise TemplateError(f"unknown filter {f.name!r}")  # validate_card prevents this


def to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, separators=(",", ":"))


# ---- resolution ------------------------------------------------------------


def lookup(ph: Placeholder, ctx: Context) -> Any:
    if ph.namespace == "base_url":
        return ctx.base_url
    if ph.namespace == "params":
        name = ph.path[0]
        if name not in ctx.params:
            raise MissingParam(f"param {name!r} has no value")
        return ctx.params[name]
    if ph.namespace == "session":
        name = ph.path[1]
        if name not in ctx.cookies:
            raise MissingCookie(name)
        return ctx.cookies[name]
    step_id, key = ph.path
    matches = ctx.steps.get((step_id, key), [])
    if len(matches) != 1:
        raise StepValueError(step_id, key, len(matches))
    return matches[0]


def evaluate(ph: Placeholder, ctx: Context) -> Any:
    value = lookup(ph, ctx)
    for f in ph.filters:
        value = apply_filter(value, f)
    return value


def resolve(template: str, ctx: Context) -> Any:
    """Resolve one string. A string that is *only* a placeholder keeps the value's type."""
    phs = find_placeholders(template)
    if not phs:
        return template
    if len(phs) == 1 and phs[0].raw == template:
        return evaluate(phs[0], ctx)
    parts: list[str] = []
    last = 0
    for m, ph in zip(PLACEHOLDER_RE.finditer(template), phs, strict=True):
        parts.append(template[last : m.start()])
        parts.append(to_text(evaluate(ph, ctx)))
        last = m.end()
    parts.append(template[last:])
    return "".join(parts)


def resolve_text(template: str, ctx: Context) -> str:
    return to_text(resolve(template, ctx))


def resolve_body(body: Any, ctx: Context) -> Any:
    """Resolve a JSON body. Keys (or list items) using an absent optional param are dropped."""
    if isinstance(body, str):
        return resolve(body, ctx)
    if isinstance(body, dict):
        out: dict[str, Any] = {}
        for k, v in body.items():
            try:
                out[k] = resolve_body(v, ctx)
            except MissingParam:
                continue
        return out
    if isinstance(body, list):
        items: list[Any] = []
        for v in body:
            try:
                items.append(resolve_body(v, ctx))
            except MissingParam:
                continue
        return items
    return body  # literal JSON values are sent as written


def _url_text(template: str, ctx: Context) -> str:
    """Resolve a URL. An absent optional param becomes an empty string there."""
    phs = find_placeholders(template)
    parts: list[str] = []
    last = 0
    for m, ph in zip(PLACEHOLDER_RE.finditer(template), phs, strict=True):
        parts.append(template[last : m.start()])
        try:
            parts.append(to_text(evaluate(ph, ctx)))
        except MissingParam:
            pass
        last = m.end()
    parts.append(template[last:])
    return "".join(parts)


def encode_query(query: Mapping[str, Any], ctx: Context) -> str:
    """Build the query string (rule 4): every value percent-encoded, except the restli-safe chars
    of values whose template uses `escape_restli`."""
    pairs: list[str] = []
    for key, template in query.items():
        if isinstance(template, str):
            try:
                value = resolve_text(template, ctx)
            except MissingParam:
                continue
            restli = any(ph.uses_escape_restli for ph in find_placeholders(template))
        else:
            value, restli = to_text(template), False
        safe = RESTLI_SAFE if restli else ""
        pairs.append(f"{quote(key, safe='')}={quote(value, safe=safe)}")
    return "&".join(pairs)


@dataclass
class BuiltRequest:
    method: str
    url: str  # including the encoded query string
    headers: dict[str, str]
    json_body: Any = None
    content: str | None = None  # for non-JSON (string) bodies
    has_body: bool = False


def build_request(req: Request, headers: Mapping[str, str], ctx: Context) -> BuiltRequest:
    """Resolve a step/verify request. `headers` is the already-merged header template map."""
    url = _url_text(req.url, ctx)
    qs = encode_query(req.query, ctx)
    if qs:
        url = f"{url}{'&' if '?' in url else '?'}{qs}"
    out_headers: dict[str, str] = {}
    for name, template in headers.items():
        try:
            out_headers[name] = resolve_text(template, ctx)
        except MissingParam:
            continue
    built = BuiltRequest(method=req.method.upper(), url=url, headers=out_headers)
    if req.body is not None:
        body = resolve_body(req.body, ctx)
        built.has_body = True
        if isinstance(req.body, str):
            built.content = to_text(body)
        else:
            built.json_body = body
    return built


def merge_headers(*layers: Mapping[str, str]) -> dict[str, str]:
    """Merge header maps case-insensitively; later layers win. Names are lowercased."""
    merged: dict[str, str] = {}
    for layer in layers:
        for k, v in layer.items():
            merged[k.lower()] = v
    return merged
