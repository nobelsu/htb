"""Builders for synthetic HAR entries shaped like Playwright's (see test_capture for the real thing)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

T0 = datetime(2026, 11, 7, 15, 0, 0, tzinfo=UTC)


def ts(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def har_entry(
    method: str,
    url: str,
    *,
    at: float = 0.0,
    status: int = 200,
    mime: str = "application/json",
    body: Any = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    post: str | None = None,
    post_mime: str = "",
    response_headers: dict[str, str] | list[tuple[str, str]] | None = None,
    resource_type: str | None = "fetch",
) -> dict:
    if body is not None and not isinstance(body, str):
        body = json.dumps(body)
    req_headers = [{"name": k, "value": v} for k, v in (headers or {}).items()]
    if cookies:
        req_headers.append({"name": "Cookie", "value": "; ".join(f"{k}={v}" for k, v in cookies.items())})
    resp_pairs = response_headers.items() if isinstance(response_headers, dict) else response_headers or []
    entry = {
        "startedDateTime": ts(at),
        "time": 5.0,
        "request": {
            "method": method,
            "url": url,
            "httpVersion": "http/1.1",
            "cookies": [{"name": k, "value": v} for k, v in (cookies or {}).items()],
            "headers": req_headers,
            "queryString": [{"name": k, "value": v} for k, v in parse_qsl(urlsplit(url).query)],
            "headersSize": -1,
            "bodySize": len(post or ""),
        },
        "response": {
            "status": status,
            "statusText": "",
            "httpVersion": "http/1.1",
            "cookies": [],
            "headers": [{"name": "Content-Type", "value": mime}]
            + [{"name": k, "value": v} for k, v in resp_pairs],
            "content": {"size": len(body or ""), "mimeType": mime, **({"text": body} if body else {})},
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": len(body or ""),
        },
        "cache": {},
        "timings": {"send": 0, "wait": 4, "receive": 1},
    }
    if post is not None:
        entry["request"]["postData"] = {"mimeType": post_mime, "text": post}
    if resource_type:
        entry["_resourceType"] = resource_type
    return entry


def write_har(path: Path, entries: list[dict]) -> Path:
    path.write_text(json.dumps({"log": {"version": "1.2", "creator": {"name": "test"}, "entries": entries}}))
    return path
