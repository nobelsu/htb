"""Signal matching (CARD_SPEC.md "Signals") and response content-type checks."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

from runtime.tiers import HttpResponse
from schema.card import ContentType


def _path(location: str) -> str:
    return urlsplit(location).path or "/"


def match_signal(signals: Iterable[str], resp: HttpResponse) -> str | None:
    """Return the first signal that matches the response, if any."""
    for signal in signals:
        kind, _, value = signal.partition(":")
        if kind == "status":
            if resp.status == int(value):
                return signal
        elif kind == "redirect":
            paths = [_path(loc) for _, loc in resp.hops if loc] + [_path(resp.final_url)]
            if any(p.startswith(value) for p in paths):
                return signal
        elif kind == "body_contains":
            if value in resp.text:
                return signal
    return None


def media_type(content_type_header: str | None) -> str:
    return (content_type_header or "").split(";", 1)[0].strip().lower()


def content_type_ok(expected: ContentType, header: str | None) -> bool:
    mt = media_type(header)
    if expected == "any":
        return True
    if expected == "json":
        return mt == "application/json" or mt.endswith("+json")
    if expected == "html":
        return mt in ("text/html", "application/xhtml+xml")
    return mt.startswith("text/") and mt != "text/html"  # "text"
