"""Redaction of secrets before anything is logged or returned."""

from __future__ import annotations

from collections.abc import Mapping

REDACTED = "[redacted]"
_SENSITIVE_PARTS = ("cookie", "csrf", "xsrf", "auth", "token", "session", "api-key", "apikey")


def is_sensitive_header(name: str) -> bool:
    n = name.lower()
    return any(part in n for part in _SENSITIVE_PARTS)


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: (REDACTED if is_sensitive_header(k) else v) for k, v in headers.items()}


def redact_cookies(cookies: Mapping[str, str]) -> dict[str, str]:
    return {k: REDACTED for k in cookies}


def scrub(text: str, secrets: list[str]) -> str:
    """Replace any secret value that leaked into free text (e.g. an error message)."""
    for s in secrets:
        if len(s) >= 4:
            text = text.replace(s, REDACTED)
    return text
