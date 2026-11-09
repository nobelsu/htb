"""M0 recon (informational): guess what a site is built with, and find data embedded in its pages.

Looks at every captured response (assets included, since M1 would drop them): framework markers in
HTML and headers, JSON-LD blocks, and embedded JSON blobs such as Next.js's ``__NEXT_DATA__``. For an
embedded blob it suggests a ready-made regex extract (``parse: "json"``), like the spec's example. The
result only goes into the mapping report; it never changes the card.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from spider.harmap.pipeline.load import Entry

# (platform, where to look, pattern)
SIGNATURES: list[tuple[str, str, re.Pattern]] = [
    ("Next.js", "body", re.compile(r'<script id="__NEXT_DATA__"|/_next/static/')),
    ("Next.js", "header", re.compile(r"^x-powered-by: next\.js", re.I | re.M)),
    ("Nuxt", "body", re.compile(r"window\.__NUXT__|/_nuxt/")),
    ("Shopify", "body", re.compile(r"cdn\.shopify\.com|Shopify\.theme")),
    ("Shopify", "header", re.compile(r"^x-shopify-stage:", re.I | re.M)),
    (
        "WordPress",
        "body",
        re.compile(r"/wp-content/|/wp-json/|<meta name=\"generator\" content=\"WordPress", re.I),
    ),
    ("GraphQL API", "url", re.compile(r"/graphql\b", re.I)),
    ("LinkedIn Rest.li API", "url", re.compile(r"/voyager/api/")),
    ("LinkedIn Rest.li API", "header", re.compile(r"^x-restli-protocol-version:", re.I | re.M)),
]

JSON_LD = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
JSON_SCRIPT = re.compile(
    r'<script[^>]*\bid="([^"]+)"[^>]*type="application/json"[^>]*>|'
    r'<script[^>]*type="application/json"[^>]*\bid="([^"]+)"[^>]*>',
    re.I,
)


@dataclass
class Embedded:
    page: str  # path of the page that carries it
    script_id: str
    top_keys: list[str]
    extract: dict  # suggested card extract


@dataclass
class Recon:
    platforms: dict[str, list[str]] = field(default_factory=dict)  # platform → evidence
    json_ld_types: list[str] = field(default_factory=list)
    embedded: list[Embedded] = field(default_factory=list)

    def add(self, platform: str, evidence: str) -> None:
        seen = self.platforms.setdefault(platform, [])
        if evidence not in seen and len(seen) < 3:
            seen.append(evidence)


def _headers_text(entry: Entry) -> str:
    return "\n".join(f"{k}: {v}" for k, v in entry.response_headers)


def _ld_types(block: str) -> list[str]:
    try:
        data = json.loads(block)
    except ValueError:
        return []
    items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
    out = []
    for item in items:
        t = item.get("@type") if isinstance(item, dict) else None
        out += t if isinstance(t, list) else [t] if isinstance(t, str) else []
    return out


def embedded_extract(script_id: str) -> dict:
    # [\s\S] rather than "." so the blob may span lines without needing a DOTALL flag
    expr = f'<script id="{re.escape(script_id)}"[^>]*>([\\s\\S]*?)</script>'
    return {"from": "body", "type": "regex", "expr": expr, "parse": "json"}


def recon(entries: list[Entry]) -> Recon:
    r = Recon()
    for e in entries:
        body = (e.response_body or "") if e.response_kind in ("html", "text", "static", "json") else ""
        where = {"body": body[:500_000], "header": _headers_text(e), "url": e.url}
        for platform, place, pattern in SIGNATURES:
            if pattern.search(where[place]):
                r.add(platform, f"{place} of {e.method} {e.path}")
        if e.response_kind != "html" or not body:
            continue
        for block in JSON_LD.findall(body):
            for t in _ld_types(block):
                if t not in r.json_ld_types:
                    r.json_ld_types.append(t)
        for m in JSON_SCRIPT.finditer(body):
            script_id = m.group(1) or m.group(2)
            if any(x.script_id == script_id and x.page == e.path for x in r.embedded):
                continue
            extract = embedded_extract(script_id)
            found = re.findall(extract["expr"], body)
            try:
                data = json.loads(found[0]) if len(found) == 1 else None
            except ValueError:
                data = None
            if isinstance(data, dict):
                r.embedded.append(Embedded(e.path, script_id, sorted(data)[:12], extract))
    return r
