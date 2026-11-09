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


def make_bundle(
    run: str,
    entries: list[dict],
    *,
    values: dict[str, str] | None = None,
    kind: str = "read",
    marks: list[float] | None = None,
    actions: list[dict] | None = None,
    state_cookies: dict[str, str] | None = None,
    domain: str = "example.com",
):
    """An in-memory Bundle from synthetic HAR entries (M1 filter applied, like load_bundle)."""
    from spider.harmap.helpers.runs import Bundle
    from spider.harmap.pipeline.load import entry_from_har, filter_entries

    is_marker_run = run in ("m1", "m2")
    meta = {
        "bundle_version": "1",
        "domain": domain,
        "task": "t",
        "run": run,
        "markers": dict(values or {}) if is_marker_run else {},
        "real_inputs": {} if is_marker_run else dict(values or {}),
        "kind_hint": kind,
        "goal_marks": [ts(m) for m in marks or []],
        "started_at": ts(0),
    }
    parsed = sorted((entry_from_har(i, e) for i, e in enumerate(entries)), key=lambda e: (e.started, e.index))
    result = filter_entries(parsed, domain)
    return Bundle(meta, result.kept, result.dropped, list(actions or []), dict(state_cookies or {}))


def action(kind: str, at: float, **extra) -> dict:
    return {"t": ts(at), "type": kind, **extra}
