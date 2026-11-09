"""M2 normalize: entries → addressed fields.

Addresses (docs/harmap-design.md, Definitions):

* ``url.path[i]``            i-th path segment (0-based), percent-decoded
* ``query.NAME``             query value (``query.NAME[i]`` when the name repeats)
* ``header.NAME``            request header, lower-cased name
* ``cookie.NAME``            request cookie (quotes kept, as sent)
* ``body.$.a.b``             JSON body leaf (JSONPath)
* ``body.NAME``              form body field
* ``body``                   any other text body, whole

Structured values are also split into sub-fields under their parent's address:
Rest.li ``(k:v,...)`` values → ``query.variables.$.k`` (context ``restli``), and JSON values such as
GraphQL ``variables={...}`` → ``query.variables.$.k`` (context ``json``).

``Field.context`` records how the value sits on the wire, which M4 uses to choose a filter:
``path`` | ``query`` | ``header`` | ``cookie`` | ``form`` | ``json`` | ``restli`` | ``text``.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, unquote_plus

from harmap import jsonpaths, restli
from harmap.pipeline.m1_load import Entry

# Headers an HTTP client computes itself; never part of a request template.
SKIP_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "cookie",  # exploded into cookie.* fields
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "te",
        "trailer",
        "proxy-connection",
    }
)


@dataclass(frozen=True)
class Field:
    address: str
    location: str  # url | query | header | cookie | body
    value: Any  # decoded value; native JSON type for JSON leaves
    raw: str  # as it appeared on the wire
    context: str  # path | query | header | cookie | form | json | restli | text
    parent: str | None = None  # containing field's address, for sub-fields
    structure: str | None = None  # "restli" | "json" when this field has sub-fields

    @property
    def text(self) -> str:
        """Value as a string (JSON scalars rendered the way they'd appear in text)."""
        if isinstance(self.value, str):
            return self.value
        return json.dumps(self.value)


def _split_pairs(raw: str) -> list[tuple[str, str]]:
    pairs = []
    for part in raw.split("&"):
        if not part:
            continue
        name, _, value = part.partition("=")
        pairs.append((name, value))
    return pairs


def _indexed_names(names: list[str]) -> list[str]:
    counts = Counter(names)
    seen: Counter[str] = Counter()
    out = []
    for n in names:
        if counts[n] > 1:
            out.append(f"{n}[{seen[n]}]")
            seen[n] += 1
        else:
            out.append(n)
    return out


def _structured(address: str, location: str, raw: str, decoded: str) -> tuple[str | None, list[Field]]:
    """Sub-fields for a Rest.li or JSON value. Returns (structure, sub_fields)."""
    tree = restli.try_parse(raw)
    if tree is not None:
        return "restli", [
            Field(f"{address}.{path}", location, leaf, restli.dumps(leaf), "restli", parent=address)
            for path, leaf in jsonpaths.flatten(tree)
        ]
    stripped = decoded.strip()
    if stripped[:1] in ("{", "["):
        try:
            tree = json.loads(stripped)
        except ValueError:
            return None, []
        return "json", [
            Field(f"{address}.{path}", location, leaf, json.dumps(leaf), "json", parent=address)
            for path, leaf in jsonpaths.flatten(tree)
        ]
    return None, []


def _encoded_pairs(location: str, raw: str, context: str) -> list[Field]:
    """Fields for ``a=1&b=2`` text (query strings and form bodies)."""
    pairs = _split_pairs(raw)
    names = _indexed_names([unquote_plus(n) for n, _ in pairs])
    fields: list[Field] = []
    for name, (_, raw_value) in zip(names, pairs, strict=True):
        address = f"{location}.{name}"
        decoded = unquote_plus(raw_value)
        structure, subs = _structured(address, location, raw_value, decoded)
        fields.append(Field(address, location, decoded, raw_value, context, structure=structure))
        fields.extend(subs)
    return fields


def path_fields(entry: Entry) -> list[Field]:
    segments = [s for s in entry.path.split("/") if s]
    return [Field(f"url.path[{i}]", "url", unquote(seg), seg, "path") for i, seg in enumerate(segments)]


def query_fields(entry: Entry) -> list[Field]:
    return _encoded_pairs("query", entry.raw_query, "query")


def header_fields(entry: Entry) -> list[Field]:
    out = []
    names = [k.lower() for k, _ in entry.request_headers]
    for name, (_, value) in zip(_indexed_names(names), entry.request_headers, strict=True):
        base = name.split("[", 1)[0]
        if base.startswith(":") or base in SKIP_HEADERS:
            continue
        out.append(Field(f"header.{name}", "header", value, value, "header"))
    return out


def cookie_fields(entry: Entry) -> list[Field]:
    cookies = entry.request_cookies
    if not cookies and entry.header("cookie"):
        cookies = []
        for part in (entry.header("cookie") or "").split(";"):
            name, sep, value = part.strip().partition("=")
            if sep:
                cookies.append((name, value))
    return [Field(f"cookie.{name}", "cookie", value, value, "cookie") for name, value in cookies]


def body_fields(entry: Entry) -> list[Field]:
    body = entry.request_body
    if not body:
        return []
    kind = entry.request_kind
    if kind == "form":
        return _encoded_pairs("body", body, "form")
    parsed = entry.request_json
    if parsed is not None and kind in ("json", "text", "empty"):
        return [
            Field(f"body.{path}", "body", leaf, json.dumps(leaf), "json")
            for path, leaf in jsonpaths.flatten(parsed)
        ]
    return [Field("body", "body", body, body, "text")]


def normalize(entry: Entry) -> list[Field]:
    """M2: every addressable field of ``entry``'s request."""
    return [
        *path_fields(entry),
        *query_fields(entry),
        *header_fields(entry),
        *cookie_fields(entry),
        *body_fields(entry),
    ]


def leaf_fields(fields: list[Field]) -> list[Field]:
    """Fields without sub-fields (structured parents are replaced by their parts)."""
    return [f for f in fields if f.structure is None]
