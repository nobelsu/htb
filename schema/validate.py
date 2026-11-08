"""Save-time card validation (CARD_SPEC.md "Save-time validation"), ported from tools/check_card.py."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from jsonpath_ng.ext.parser import parse as jp_parse
from pydantic import ValidationError

from schema.card import Action, Card, Request
from schema.params import coerce_params, format_field_errors
from schema.placeholders import (
    PlaceholderError,
    find_placeholders,
    is_valid_signal,
    parse_step_ref,
)

TOOL_PREFIX_RE = re.compile(r"[a-z0-9_]{1,20}")
MAX_TOOL_NAME = 64


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    card: Card | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


class CardInvalid(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("card failed validation:\n  " + "\n  ".join(errors))


def _strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)
    elif isinstance(obj, str):
        yield obj


def _request_strings(req: Request) -> Iterator[str]:
    yield req.url
    yield from _strings(req.query)
    yield from _strings(req.headers)
    yield from _strings(req.body)


class _Checker:
    def __init__(self, card: Card, report: ValidationReport) -> None:
        self.card = card
        self.errors = report.errors
        self.warnings = report.warnings
        self.provider_ids = {p.id for p in card.connect_providers}

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def placeholders(
        self,
        texts: Iterator[str],
        where: str,
        params: set[str],
        step_keys: set[tuple[str, str]],
        auth: str | None,
    ) -> None:
        for text in texts:
            try:
                found = find_placeholders(text)
            except PlaceholderError as e:
                self.err(f"{where}: {e}")
                continue
            for ph in found:
                if ph.namespace == "params" and ph.path[0] not in params:
                    self.err(f"{where}: unknown param {ph.path[0]!r} in {ph.raw}")
                elif ph.namespace == "session" and auth is None:
                    self.err(f"{where}: {ph.raw} used where auth_provider is null")
                elif ph.namespace == "steps" and (ph.path[0], ph.path[1]) not in step_keys:
                    self.err(f"{where}: {ph.raw} is not an earlier step's extract key")

    def jsonpath(self, expr: str, where: str) -> None:
        try:
            jp_parse(expr)
        except Exception as e:  # jsonpath_ng raises several exception types
            self.err(f"{where}: bad jsonpath {expr!r}: {e}")

    def css(self, expr: str, where: str) -> None:
        from selectolax.lexbor import LexborHTMLParser

        try:
            LexborHTMLParser("<p></p>").css(expr)
        except Exception:
            self.err(f"{where}: bad CSS selector {expr!r}")

    def regex(self, expr: str, where: str) -> None:
        try:
            re.compile(expr)
        except re.error as e:
            self.err(f"{where}: bad regex {expr!r}: {e}")

    def signals(self, signals: list[str], where: str) -> None:
        for s in signals:
            if not is_valid_signal(s):
                self.err(
                    f"{where}: bad signal {s!r} (expected status:NNN, redirect:/path or body_contains:text)"
                )

    def site(self) -> None:
        c = self.card
        if not TOOL_PREFIX_RE.fullmatch(c.tool_prefix):
            self.err(f"tool_prefix {c.tool_prefix!r} must match [a-z0-9_]{{1,20}}")
        self.signals(c.blocked_signals, "blocked_signals")
        self.signals(c.changed_signals, "changed_signals")
        seen: set[str] = set()
        for p in c.connect_providers:
            where = f"connect_providers.{p.id}"
            if p.id in seen:
                self.err(f"{where}: duplicate provider id")
            seen.add(p.id)
            self.signals(p.logged_out_signals, f"{where}.logged_out_signals")
            self.placeholders(_request_strings(p.session_check), f"{where}.session_check", set(), set(), p.id)
            for path in p.session_check.expect.paths:
                self.jsonpath(path, f"{where}.session_check.expect")
        auth = c.defaults.auth_provider
        if auth is not None and auth not in self.provider_ids:
            self.err(f"defaults.auth_provider {auth!r} matches no connect_providers[].id")

    def action(self, a: Action) -> None:
        c, n = self.card, a.name
        tool = f"{c.tool_prefix}_{n}"
        if len(tool) > MAX_TOOL_NAME:
            self.err(f"{n}: tool name {tool!r} is longer than {MAX_TOOL_NAME} characters")
        auth = a.effective_auth_provider(c.defaults)
        if auth is not None and auth not in self.provider_ids:
            self.err(f"{n}: auth_provider {auth!r} matches no connect_providers[].id")
        if a.kind == "write" and not a.requires_confirmation:
            self.warnings.append(f"{n}: write action without requires_confirmation")

        params = set(a.params)
        for pn, spec in a.params.items():
            if pn.startswith("_"):
                self.err(f"{n}.params.{pn}: param names starting with '_' are reserved")
            if spec.type == "enum" and not spec.values:
                self.err(f"{n}.params.{pn}: enum without values")
            if spec.required and spec.has_default:
                self.err(f"{n}.params.{pn}: param is both required and given a default")
            if spec.has_default and spec.default is not None:
                _, errs = coerce_params({pn: spec}, {pn: spec.default})
                if errs:
                    self.err(f"{n}.params.{pn}: default {format_field_errors(errs)}")

        # Inherited headers are used by every step, so check them under this action's auth.
        header_texts = list(c.defaults.headers.values()) + list(a.headers.values())
        self.placeholders(iter(header_texts), f"{n}.headers", params, set(), auth)

        step_keys: set[tuple[str, str]] = set()
        step_ids: set[str] = set()
        for st in a.steps:
            where = f"{n}.steps.{st.id}"
            if st.id in step_ids:
                self.err(f"{where}: duplicate step id")
            step_ids.add(st.id)
            self.placeholders(_request_strings(st.request), where, params, step_keys, auth)
            for path in st.expect.paths:
                self.jsonpath(path, f"{where}.expect.paths")
            for key, ex in st.extract.items():
                ew = f"{where}.extract.{key}"
                if ex.type == "jsonpath":
                    self.jsonpath(ex.expr, ew)
                elif ex.type == "css":
                    self.css(ex.expr, ew)
                    if ex.from_ != "body" or ex.parse or ex.then:
                        self.err(f"{ew}: a css extract reads the body, with no 'parse'/'then'")
                else:
                    self.regex(ex.expr, ew)
                    if ex.then is not None:
                        self.jsonpath(ex.then, f"{ew}.then")
                if ex.type == "jsonpath" and (ex.parse or ex.then):
                    self.err(f"{ew}: 'parse'/'then' are only allowed on regex extracts")
            # Keys are added after the step's own request is checked: no self-references.
            step_keys.update((st.id, k) for k in st.extract)

        o = a.output
        if o.from_ is not None:
            self.step_ref(o.from_, f"{n}.output.from", step_keys)
            for fname, expr in o.fields.items():
                self.jsonpath(expr, f"{n}.output.fields.{fname}")
        np_ = o.next_params
        if np_ is not None:
            if np_.param not in params:
                self.err(f"{n}.output.next_params.param {np_.param!r} is not a param")
            if o.limit != np_.page_size:
                self.err(f"{n}: output.limit ({o.limit}) must equal next_params.page_size ({np_.page_size})")
            if np_.until is not None:
                self.step_ref(np_.until, f"{n}.output.next_params.until", step_keys)

        if a.verify is not None:
            self.placeholders(_request_strings(a.verify.request), f"{n}.verify", params, step_keys, auth)
            for path in a.verify.expect.paths:
                self.jsonpath(path, f"{n}.verify.expect.paths")
            self.jsonpath(a.verify.assert_.expr, f"{n}.verify.assert")

        if a.kind == "read" and not a.examples:
            self.err(f"{n}: read action needs at least one example")
        for i, example in enumerate(a.examples):
            _, errs = coerce_params(a.params, example.params)
            for k in [k for k in errs if k.startswith("_")]:
                errs[k] = "examples may not use reserved params"
            if errs:
                self.err(f"{n}.examples[{i}]: {format_field_errors(errs)}")

    def step_ref(self, ref: str, where: str, step_keys: set[tuple[str, str]]) -> None:
        try:
            key = parse_step_ref(ref)
        except PlaceholderError as e:
            self.err(f"{where}: {e}")
            return
        if key not in step_keys:
            self.err(f"{where}: {ref!r} is not a step extract key")


def validate_card(data: dict[str, Any] | Card) -> ValidationReport:
    """Run every save-time rule. Returns a report; `report.ok` means the card may be saved."""
    report = ValidationReport()
    if isinstance(data, Card):
        card = data
    else:
        try:
            card = Card.model_validate(data)
        except ValidationError as e:
            for err in e.errors():
                loc = ".".join(str(p) for p in err["loc"]) or "<card>"
                report.errors.append(f"{loc}: {err['msg']}")
            return report
    checker = _Checker(card, report)
    checker.site()
    names: set[str] = set()
    for a in card.actions:
        if a.name in names:
            report.errors.append(f"duplicate action name {a.name!r}")
        names.add(a.name)
        checker.action(a)
    report.card = card
    return report


def load_card(data: dict[str, Any]) -> Card:
    """Validate and return the card, raising CardInvalid on any error."""
    report = validate_card(data)
    if not report.ok or report.card is None:
        raise CardInvalid(report.errors)
    return report.card
