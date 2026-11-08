"""M1 load + filter: HAR → candidate entries.

Keep same-site hosts (registrable-domain match), any method, JSON/HTML/text/form content, and streamed
data rows (one ``<hex id>:<JSON>`` row per line, the React Server Components format LinkedIn's web app and
Next.js App Router sites answer with, often labelled ``application/octet-stream``).
Drop static assets (images, fonts, css, js, media), known analytics/ads hosts, OPTIONS preflights,
and requests that never got a response. Every drop is recorded with a reason for the mapping report.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mapper.bundle import parse_iso
from mapper.domains import registrable_domain, same_site

TRACKER_DOMAINS = frozenset(
    {
        "google-analytics.com",
        "googletagmanager.com",
        "doubleclick.net",
        "googlesyndication.com",
        "googleadservices.com",
        "facebook.net",
        "hotjar.com",
        "segment.io",
        "segment.com",
        "sentry.io",
        "mixpanel.com",
        "amplitude.com",
        "nr-data.net",
        "newrelic.com",
        "datadoghq.com",
        "browser-intake-datadoghq.com",
        "clarity.ms",
        "fullstory.com",
        "optimizely.com",
        "ads-twitter.com",
        "quantserve.com",
        "scorecardresearch.com",
    }
)
# Leftmost host label that marks a same-site tracking/ads subdomain (e.g. px.ads.linkedin.com).
TRACKER_LABELS = frozenset(
    {"ads", "px", "pixel", "analytics", "stats", "telemetry", "metrics", "tracking", "beacon", "collector"}
)
TRACKER_PATH = re.compile(r"^/(li/track|sensorCollect|collect|beacon|pixel|telemetry)(/|$)", re.I)

STATIC_RESOURCE_TYPES = frozenset(
    {"image", "font", "stylesheet", "script", "media", "manifest", "texttrack", "websocket", "eventsource"}
)
BEACON_RESOURCE_TYPES = frozenset({"ping", "beacon", "csp_report"})
STATIC_EXT = re.compile(
    r"\.(png|jpe?g|gif|webp|avif|svg|ico|bmp|woff2?|ttf|otf|eot|css|js|mjs|map|mp4|webm|mp3|wav|ogg)$",
    re.I,
)


# One data row per line, ``<hex id>:<payload>``. Recognised by shape, never by a site's header names.
STREAM_ROW = re.compile(r"^[0-9a-f]{1,8}:")
STREAM_JSON_ROW = re.compile(r"(?m)^[0-9a-f]{1,8}:(\[.*\]|\{.*\})$")


def is_row_stream(body: str | None) -> bool:
    """Most (non-empty) lines are ``<hex id>:...`` rows, starting with the first, and some carry JSON."""
    if not body:
        return False
    lines = [ln for ln in body[:200_000].split("\n") if ln.strip()][:400]
    if not lines or not STREAM_ROW.match(lines[0]):
        return False
    rows = sum(1 for ln in lines if STREAM_ROW.match(ln))
    return rows * 2 >= len(lines) and STREAM_JSON_ROW.search(body[:200_000]) is not None


def content_kind(mime: str | None, body: str | None = None) -> str:
    """Classify a response: json | html | text | form | multipart | static | stream | other | empty."""
    kind = _mime_kind(mime, body)
    if kind in ("other", "text") and is_row_stream(body):
        return "stream"
    return kind


def _mime_kind(mime: str | None, body: str | None) -> str:
    m = (mime or "").split(";")[0].strip().lower()
    if not m:
        return "empty" if not body else "text"
    if m == "application/json" or "+json" in m or m in ("text/json", "application/x-ndjson"):
        return "json"
    if m in ("text/html", "application/xhtml+xml"):
        return "html"
    if m == "application/x-www-form-urlencoded":
        return "form"
    if m.startswith("multipart/"):
        return "multipart"
    if m in ("text/css", "text/javascript", "application/javascript", "application/x-javascript"):
        return "static"
    if m.startswith(("image/", "font/", "audio/", "video/")) or m in (
        "application/font-woff",
        "application/vnd.ms-fontobject",
    ):
        return "static"
    if m.startswith("text/") or m in ("application/xml", "application/graphql"):
        return "text"
    return "other"


def _pairs(items: list[dict] | None) -> list[tuple[str, str]]:
    return [(i["name"], i.get("value", "")) for i in items or []]


def _content_text(content: dict | None) -> str | None:
    if not content or content.get("text") is None:
        return None
    text = content["text"]
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(text).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    return text


@dataclass
class Entry:
    """One HTTP exchange from the HAR, with just what the pipeline needs."""

    index: int
    started: datetime
    started_iso: str
    time_ms: float
    method: str
    url: str
    request_headers: list[tuple[str, str]]
    request_cookies: list[tuple[str, str]]
    request_body: str | None
    request_mime: str
    status: int
    response_headers: list[tuple[str, str]]
    response_mime: str
    response_body: str | None
    redirect_url: str = ""
    resource_type: str | None = None
    raw: dict = field(default_factory=dict, repr=False)
    blocked: bool = False  # a write's final request, stopped by the explorer before it reached the site

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()

    @property
    def scheme(self) -> str:
        return urlsplit(self.url).scheme.lower()

    @property
    def path(self) -> str:
        return urlsplit(self.url).path or "/"

    @property
    def raw_query(self) -> str:
        return urlsplit(self.url).query

    @property
    def base_url(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}"

    def header(self, name: str) -> str | None:
        name = name.lower()
        return next((v for k, v in self.request_headers if k.lower() == name), None)

    def response_header(self, name: str) -> str | None:
        name = name.lower()
        return next((v for k, v in self.response_headers if k.lower() == name), None)

    @property
    def set_cookies(self) -> list[tuple[str, str]]:
        """``(name, value)`` for each ``Set-Cookie`` on the response."""
        out = []
        for k, v in self.response_headers:
            if k.lower() == "set-cookie":
                for line in v.split("\n"):  # some HAR writers join multiple cookies with newlines
                    name, sep, rest = line.partition("=")
                    if sep:
                        out.append((name.strip(), rest.split(";", 1)[0].strip()))
        return out

    @cached_property
    def response_kind(self) -> str:
        return content_kind(self.response_mime, self.response_body)

    @cached_property
    def request_kind(self) -> str:
        return content_kind(self.request_mime, self.request_body)

    @cached_property
    def response_rows(self) -> list | None:
        """A streamed response's JSON rows, in order (``None`` unless ``response_kind`` is ``stream``).
        Kept apart from ``response_json``: row positions change between answers, so nothing should
        address a value by them; values inside streams are found by text instead."""
        if self.response_kind != "stream":
            return None
        rows = []
        for m in STREAM_JSON_ROW.finditer(self.response_body or ""):
            try:
                rows.append(json.loads(m.group(1)))
            except ValueError:
                continue
        return rows

    @cached_property
    def response_json(self) -> Any:
        return _try_json(self.response_body) if self.response_kind in ("json", "text") else None

    @cached_property
    def request_json(self) -> Any:
        if self.request_kind in ("json", "text", "empty") and self.request_body:
            return _try_json(self.request_body)
        return None

    @property
    def label(self) -> str:
        return f"#{self.index} {self.method} {self.path}"


def _try_json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def entry_from_har(index: int, e: dict) -> Entry:
    req, resp = e.get("request", {}), e.get("response", {})
    post = req.get("postData") or {}
    started_iso = e.get("startedDateTime", "")
    return Entry(
        index=index,
        started=parse_iso(started_iso),
        started_iso=started_iso,
        time_ms=float(e.get("time") or 0),
        method=req.get("method", "GET").upper(),
        url=req.get("url", ""),
        request_headers=_pairs(req.get("headers")),
        request_cookies=_pairs(req.get("cookies")),
        request_body=post.get("text"),
        request_mime=post.get("mimeType", ""),
        status=int(resp.get("status") or 0),
        response_headers=_pairs(resp.get("headers")),
        response_mime=(resp.get("content") or {}).get("mimeType", ""),
        response_body=_content_text(resp.get("content")),
        redirect_url=resp.get("redirectURL", "") or "",
        resource_type=e.get("_resourceType"),
        raw=e,
    )


def load_har(path: str | Path) -> list[Entry]:
    har = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = [entry_from_har(i, e) for i, e in enumerate(har.get("log", {}).get("entries", []))]
    entries.sort(key=lambda en: (en.started, en.index))
    return entries


def is_tracker_host(host: str) -> bool:
    if registrable_domain(host) in TRACKER_DOMAINS:
        return True
    return host.split(".", 1)[0] in TRACKER_LABELS and "." in host


def drop_reason(entry: Entry, domain: str) -> str | None:
    """Why M1 drops ``entry`` (``None`` = keep)."""
    if entry.scheme not in ("http", "https"):
        return "non-http"
    if entry.method == "OPTIONS":
        return "preflight"
    if is_tracker_host(entry.host):
        return "analytics/ads host"
    if not same_site(entry.host, domain):
        return "third-party host"
    if TRACKER_PATH.match(entry.path) or entry.resource_type in BEACON_RESOURCE_TYPES:
        return "analytics beacon"
    if entry.status <= 0 and not entry.blocked:
        return "no response"
    if entry.resource_type in STATIC_RESOURCE_TYPES:
        return f"static asset ({entry.resource_type})"
    kind = entry.response_kind
    if kind == "static" or (kind in ("empty", "other") and STATIC_EXT.search(entry.path)):
        return "static asset"
    if kind == "other" and entry.request_kind not in ("json", "form"):
        return f"unsupported content type ({entry.response_mime})"
    return None


@dataclass
class FilterResult:
    kept: list[Entry]
    dropped: list[tuple[Entry, str]]


def filter_entries(entries: list[Entry], domain: str) -> FilterResult:
    kept, dropped = [], []
    for entry in entries:
        reason = drop_reason(entry, domain)
        if reason is None:
            kept.append(entry)
        else:
            dropped.append((entry, reason))
    return FilterResult(kept, dropped)


def load_candidates(har_path: str | Path, domain: str) -> FilterResult:
    """M1: load a bundle's HAR and filter it down to candidate entries."""
    return filter_entries(load_har(har_path), domain)
