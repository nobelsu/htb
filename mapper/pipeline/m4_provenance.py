"""M4 provenance: label every field of the goal request (and, recursively, of each new step).

Rules, in order (docs/mapper-design.md, "M4 provenance algorithm"):

1. **param**: m1's field contains marker A and m2's same field contains marker B. The wire context
   decides the filter: URL path → ``urlencode``; Rest.li → ``escape_restli``; JSON, query, form,
   header → none. A marker that is only part of the value is spliced into the literal rest.
2. **session_cookie**: the value equals a session cookie (optionally after stripping quotes) →
   ``{{session.cookies.NAME}}`` (``| strip_quotes``). ``cookie.*`` fields are never templated: the
   executor's jar sends them; they only mean the action needs an ``auth_provider``.
3. **dependency**: the value appears in an earlier response (JSON body, response header,
   ``Set-Cookie``, HTML/text body). Prefer JSON, then nearest in time; confirm on m2.
4. **constant**: same in m1 and m2, not found anywhere.
5. **unknown**: differs between m1 and m2, not found (likely computed in JS). Timestamps flagged.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from mapper import jsonpaths, placeholders, restli
from mapper.pipeline.m1_load import Entry
from mapper.pipeline.m2_normalize import Field, leaf_fields
from mapper.pipeline.m3_goal import counterpart, field_mentions, value_forms
from mapper.pipeline.runs import Bundle

MAX_DEPTH = 4

# The executor's tier sets these itself; templating them would pin one browser's fingerprint.
BROWSER_HEADERS = frozenset(
    {
        "user-agent",
        "accept-encoding",
        "accept-language",
        "referer",
        "priority",
        "upgrade-insecure-requests",
        "dnt",
        "cache-control",
        "pragma",
        "if-none-match",
        "if-modified-since",
        "purpose",
        "sec-gpc",
    }
)

TRIVIAL_WORDS = frozenset({"true", "false", "null", "none", "yes", "no", "on", "off", "undefined"})
LOCALE_RE = re.compile(r"^[a-z]{2,3}([-_][A-Za-z]{2,4})?$")
VERSION_RE = re.compile(r"^v?\d+(\.\d+){0,3}$")
MIME_RE = re.compile(r"^[a-z*]+/[a-z0-9.+*-]+(;.*)?$", re.I)

# Path characters a browser leaves as-is (RFC 3986 pchar minus "%").
PATH_SAFE = "-._~:@!$&'()*+,;="

SOURCE_RANK = {"json": 0, "header": 1, "cookie": 2, "html": 3, "text": 3, "stream": 3}


@dataclass
class Source:
    """Where a dependency's value came from, in m1, plus the extract that finds it."""

    entry: Entry
    kind: str  # json | header | cookie | html | text
    extract: dict
    confirmed: bool
    m2_entry: Entry | None = None
    hint: str = ""  # suggested extract key


@dataclass
class Label:
    field: Field
    provenance: str  # param | constant | dependency | session_cookie | unknown | omitted
    template: Any = None  # for params: the spliced template; else filled by M5
    params: tuple[str, ...] = ()
    filter: str | None = None
    encoding: str | None = None  # how the marker was found: raw | percent | json | restli
    cookie: str | None = None
    strip_quotes: bool = False
    source: Source | None = None
    m2: Field | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def address(self) -> str:
        return self.field.address


@dataclass
class StepDraft:
    entry: Entry
    m2_entry: Entry | None
    depth: int
    labels: list[Label]
    is_goal: bool = False

    def label(self, address: str) -> Label | None:
        return next((lab for lab in self.labels if lab.address == address), None)


@dataclass
class Provenance:
    goal: StepDraft
    steps: list[StepDraft]
    warnings: list[str] = field(default_factory=list)

    @property
    def unknowns(self) -> list[Label]:
        return [lab for lab in self.goal.labels if lab.provenance == "unknown"]

    @property
    def uses_session(self) -> bool:
        return any(lab.provenance == "session_cookie" for d in self.steps for lab in d.labels)


@dataclass
class _Ctx:
    m1: Bundle
    m2: Bundle | None
    values_a: dict[str, str]
    values_b: dict[str, str]
    base_url: str
    capture_time: datetime | None
    warnings: list[str] = field(default_factory=list)


# --- value classification ---------------------------------------------------------------------


def as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def is_trivial(value: Any) -> bool:
    if value is None or isinstance(value, (bool, dict, list)):
        return True
    s = as_text(value).strip()
    if len(s) < 4 or s.lower() in TRIVIAL_WORDS:
        return True
    if s.isdigit() and len(s) < 5:
        return True
    return bool(LOCALE_RE.match(s) or VERSION_RE.match(s) or MIME_RE.match(s))


def token_like(value: Any) -> bool:
    s = as_text(value)
    return len(s) >= 8 and any(c.isdigit() for c in s) and any(c.isalpha() for c in s)


def is_timestamp(value: Any, around: datetime | None) -> bool:
    s = as_text(value)
    if not s.isdigit() or len(s) not in (10, 13) or around is None:
        return False
    seconds = int(s) / (1000 if len(s) == 13 else 1)
    return abs(seconds - around.timestamp()) < timedelta(days=2).total_seconds()


# --- rule 1: markers ---------------------------------------------------------------------------


def marker_encoding(f: Field, marker: str) -> str:
    """Which wire form of ``marker`` appears in ``f`` (raw | percent | json | restli)."""
    forms = value_forms(marker)
    if f.context == "restli":
        return "restli"
    if f.context == "json":
        return "json" if forms["json"] != marker and forms["json"] in f.raw else "raw"
    if forms["percent"] != marker and (forms["percent"] in f.raw or forms["percent_plus"] in f.raw):
        return "percent"
    return "raw"


def filter_for(context: str) -> str | None:
    # form bodies are sent as one text string, so each filled-in value is percent-encoded there too
    # (spec rule 5 names only URL paths; see docs/mapper-design.md "Form bodies")
    return {"path": "urlencode", "restli": "escape_restli", "form": "urlencode"}.get(context)


def literal_for(context: str, text: str) -> str:
    if context == "path":
        return quote(text, safe=PATH_SAFE)
    if context == "restli":
        return restli.escape(text)
    if context == "form":
        return quote(text, safe="")
    return text


def splice(value: str, replacements: list[tuple[str, str]], literal) -> str:
    """Replace each needle in ``value`` with its placeholder; ``literal`` renders the rest."""
    spans: list[tuple[int, int, str]] = []
    for needle, placeholder in replacements:
        start = 0
        while needle and (i := value.find(needle, start)) >= 0:
            if not any(s < i + len(needle) and i < e for s, e, _ in spans):
                spans.append((i, i + len(needle), placeholder))
            start = i + len(needle)
    spans.sort()
    out, pos = [], 0
    for s, e, placeholder in spans:
        if s > pos:
            out.append(literal(value[pos:s]))
        out.append(placeholder)
        pos = e
    if pos < len(value):
        out.append(literal(value[pos:]))
    return "".join(out)


def _param_label(f: Field, f2: Field | None, names: list[str], ctx: _Ctx) -> Label:
    filt = filter_for(f.context)
    value = as_text(f.value)
    replacements = [(ctx.values_a[n], placeholders.param(n, filt)) for n in names]
    template = splice(value, replacements, lambda t: literal_for(f.context, t))
    encodings = sorted({marker_encoding(f, ctx.values_a[n]) for n in names})
    return Label(
        f,
        "param",
        template=template,
        params=tuple(names),
        filter=filt,
        encoding=",".join(encodings),
        m2=f2,
    )


# --- rule 2: session cookies -------------------------------------------------------------------


def _set_before(bundle: Bundle, entry: Entry) -> set[str]:
    return {name for e in bundle.entries if e.started < entry.started for name, _ in e.set_cookies}


def _session_cookie(f: Field, entry: Entry, ctx: _Ctx) -> tuple[str, bool] | None:
    value = as_text(f.value)
    if is_trivial(value):
        return None
    cookies = dict(ctx.m1.state_cookies)
    cookies.update(dict(entry.request_cookies))
    set_during_capture = _set_before(ctx.m1, entry)
    for strip in (False, True):
        for name, cv in cookies.items():
            if name in set_during_capture or len(cv) < 4:
                continue
            candidate = cv.strip('"') if strip else cv
            if candidate == value and (not strip or candidate != cv):
                return name, strip
    return None


# --- rule 3: earlier responses -----------------------------------------------------------------


def _leaf_equals(leaf: Any, text: str) -> bool:
    if isinstance(leaf, bool) or leaf is None or isinstance(leaf, (dict, list)):
        return False
    return as_text(leaf) == text


def _common_suffix(a: str, b: str) -> str:
    n = 0
    while n < min(len(a), len(b)) and a[-1 - n] == b[-1 - n]:
        n += 1
    return a[len(a) - n :]


def _common_prefix(a: str, b: str) -> str:
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return a[:n]


def _escape(text: str) -> str:
    """``re.escape`` without escaping spaces (not special outside verbose mode): readable in reports."""
    return re.escape(text).replace("\\ ", " ")


def _snap_left(pre: str, n: int, slack: int = 12) -> str:
    """Last ``n`` chars of ``pre``, extended left so it doesn't start mid-word."""
    start = max(0, len(pre) - n)
    floor = max(0, start - slack)
    while start > floor and pre[start - 1].isalnum() and pre[start].isalnum():
        start -= 1
    return pre[start:]


def _snap_right(suf: str, n: int, slack: int = 12) -> str:
    """First ``n`` chars of ``suf``, extended right so it doesn't end mid-word."""
    end = min(len(suf), n)
    ceil = min(len(suf), end + slack)
    while end < ceil and suf[end - 1].isalnum() and suf[end].isalnum():
        end += 1
    return suf[:end]


def build_regex(
    body1: str, value1: str, body2: str | None = None, value2: str | None = None
) -> tuple[str, bool]:
    """Regex with ~20 chars of stable context each side; group 1 = the value. Returns (expr, unique)."""
    i = body1.find(value1)
    pre, suf = body1[max(0, i - 80) : i], body1[i + len(value1) : i + len(value1) + 80]
    if body2 and value2 and value2 in body2:
        j = body2.find(value2)
        pre = _common_suffix(pre, body2[max(0, j - 80) : j])
        suf = _common_prefix(suf, body2[j + len(value2) : j + len(value2) + 80])
    expr = ""
    # ~20 chars of prefix; the suffix as short as uniqueness allows (one char usually ends the value)
    for n_pre, n_suf in ((20, 1), (20, 20), (30, 30), (45, 45), (80, 80)):
        p, s = _snap_left(pre, n_pre), _snap_right(suf, n_suf)
        stop = s[:1]
        if stop and stop not in value1 and stop not in (value2 or "") and stop != "\n":
            group = f"([^{re.escape(stop)}]+)"
        elif s:
            group = "(.+?)"
        else:
            group = r"(\S+)"
        expr = _escape(p) + group + _escape(s)
        ok1 = re.findall(expr, body1) == [value1]
        ok2 = body2 is None or value2 is None or re.findall(expr, body2) == [value2]
        if ok1 and ok2:
            return expr, True
    return expr, False


def _key_hint(f: Field, path: str | None = None) -> str:
    if path:
        m = re.findall(r"\.([A-Za-z_][A-Za-z0-9_]*)|\['([^']+)'\]", path)
        if m:
            return next(a or b for a, b in reversed(m))
    name = f.address.split(".", 1)[-1]
    return name.replace("$.", "").split(".")[-1]


def _hits(earlier: list[Entry], f: Field) -> list[Source]:
    text = as_text(f.value)
    hits: list[Source] = []
    for e in reversed(earlier):  # nearest first
        if e.response_json is not None:
            for path, leaf in jsonpaths.flatten(e.response_json):
                if _leaf_equals(leaf, text):
                    hits.append(
                        Source(
                            e,
                            "json",
                            {"from": "body", "type": "jsonpath", "expr": path},
                            False,
                            hint=_key_hint(f, path),
                        )
                    )
        for name, v in e.response_headers:
            low = name.lower()
            if low not in ("set-cookie", "date", "content-type", "content-length") and v == text:
                expr = jsonpaths.child("$", low)
                hits.append(
                    Source(
                        e, "header", {"from": "headers", "type": "jsonpath", "expr": expr}, False, hint=low
                    )
                )
        for name, v in e.set_cookies:
            if v == text or v.strip('"') == text:
                expr = jsonpaths.child("$", name)
                hits.append(
                    Source(
                        e, "cookie", {"from": "cookies", "type": "jsonpath", "expr": expr}, False, hint=name
                    )
                )
        if e.response_kind in ("html", "text", "stream") and e.response_body and text in e.response_body:
            hits.append(Source(e, e.response_kind, {"from": "body", "type": "regex", "expr": ""}, False))
    order = {id(e): i for i, e in enumerate(reversed(earlier))}
    hits.sort(key=lambda h: (SOURCE_RANK[h.kind], order[id(h.entry)]))
    return hits


def _evaluate(src: Source, entry: Entry) -> list[str]:
    ex = src.extract
    if ex["from"] == "body" and ex["type"] == "jsonpath":
        doc = entry.response_json
        return [as_text(v) for v in jsonpaths.find(ex["expr"], doc)] if doc is not None else []
    if ex["from"] == "headers":
        doc = {k.lower(): v for k, v in entry.response_headers}
        return [as_text(v) for v in jsonpaths.find(ex["expr"], doc)]
    if ex["from"] == "cookies":
        doc = {k: v.strip('"') if v.startswith('"') else v for k, v in entry.set_cookies}
        return [as_text(v) for v in jsonpaths.find(ex["expr"], doc)]
    if ex["type"] == "regex":
        return re.findall(ex["expr"], entry.response_body or "")
    return []


def _find_source(
    ctx: _Ctx, f: Field, f2: Field | None, entry: Entry, m2_entry: Entry | None
) -> Source | None:
    earlier = [e for e in ctx.m1.entries if e.started < entry.started and e is not entry]
    hits = _hits(earlier, f)
    if not hits:
        return None
    text1 = as_text(f.value)
    text2 = as_text(f2.value) if f2 is not None else None
    first: Source | None = None
    for hit in hits:
        m2e = None
        if ctx.m2 is not None and m2_entry is not None:
            m2e = counterpart(ctx.m2, hit.entry, ctx.values_a, before=m2_entry.started)
        hit.m2_entry = m2e
        if hit.kind in ("html", "text", "stream"):
            body2 = m2e.response_body if m2e is not None else None
            expr, unique = build_regex(hit.entry.response_body or "", text1, body2, text2)
            hit.extract = {"from": "body", "type": "regex", "expr": expr}
            hit.hint = _key_hint(f)
            if not unique:
                continue
        if m2e is None or text2 is None:
            first = first or hit
            continue
        if _evaluate(hit, m2e) == [text2] and _evaluate(hit, hit.entry) == [text1]:
            hit.confirmed = True
            return hit
        first = first or hit
    return first


# --- labelling ---------------------------------------------------------------------------------


def _literal_label(f: Field, f2: Field | None, provenance: str, ctx: _Ctx, notes: list[str]) -> Label:
    template: Any = f.raw if f.context == "path" else f.value
    if isinstance(template, str) and template == ctx.base_url:
        template = placeholders.BASE_URL
    return Label(f, provenance, template=template, m2=f2, notes=notes)


def label_field(
    ctx: _Ctx, f: Field, f2: Field | None, entry: Entry, m2_entry: Entry | None, depth: int
) -> Label:
    if f.location == "header":
        name = f.address.removeprefix("header.").split("[", 1)[0]
        if name in BROWSER_HEADERS or name.startswith("sec-"):
            return Label(f, "omitted", m2=f2, notes=["browser-managed header"])
    if f.location == "cookie":
        name = f.address.removeprefix("cookie.")
        return Label(f, "session_cookie", cookie=name, m2=f2, notes=["sent by the executor's cookie jar"])

    notes: list[str] = []
    # 1. marker
    names = []
    for p, a in ctx.values_a.items():
        if not field_mentions(f, a):
            continue
        b = ctx.values_b.get(p)
        if ctx.m2 is None:
            names.append(p)
            notes.append("no m2 run: param not cross-checked")
        elif f2 is not None and b and field_mentions(f2, b):
            names.append(p)
        else:
            notes.append(f"m1 carries the {p!r} marker but m2's field does not carry marker B")
    if names:
        lab = _param_label(f, f2, names, ctx)
        lab.notes.extend(notes)
        return lab

    # 2. session cookie
    hit = _session_cookie(f, entry, ctx)
    if hit is not None:
        name, strip = hit
        return Label(f, "session_cookie", cookie=name, strip_quotes=strip, m2=f2, notes=notes)

    # 3. earlier response
    same = f2 is not None and f2.value == f.value
    if depth < MAX_DEPTH and not is_trivial(f.value) and (not same or token_like(f.value)):
        src = _find_source(ctx, f, f2, entry, m2_entry)
        if src is not None:
            if not src.confirmed:
                notes.append("dependency not confirmed on m2")
            return Label(f, "dependency", source=src, m2=f2, notes=notes)

    # 4. constant
    if f2 is None or same:
        if f2 is None and ctx.m2 is not None:
            notes.append("field missing in m2")
        return _literal_label(f, f2, "constant", ctx, notes)

    # 5. unknown
    if is_timestamp(f.value, ctx.capture_time):
        notes.append("looks like a timestamp (epoch near capture time)")
    else:
        notes.append("differs between m1 and m2 and was not found in any earlier response")
    return _literal_label(f, f2, "unknown", ctx, notes)


def label_entry(ctx: _Ctx, entry: Entry, m2_entry: Entry | None, depth: int) -> StepDraft:
    m2_fields = ctx.m2.field_map(m2_entry) if ctx.m2 is not None and m2_entry is not None else {}
    labels = [
        label_field(ctx, f, m2_fields.get(f.address), entry, m2_entry, depth)
        for f in leaf_fields(ctx.m1.fields(entry))
    ]
    return StepDraft(entry, m2_entry, depth, labels)


def trace(m1: Bundle, goal: Entry, m2: Bundle | None = None, m2_goal: Entry | None = None) -> Provenance:
    """Label the goal request and every request it depends on (breadth-first, max depth 4)."""
    ctx = _Ctx(
        m1=m1,
        m2=m2,
        values_a=m1.values,
        values_b=m2.values if m2 is not None else {},
        base_url=goal.base_url,
        capture_time=m1.started_at,
    )
    drafts: dict[int, StepDraft] = {}
    queue: list[tuple[Entry, Entry | None, int]] = [(goal, m2_goal, 0)]
    while queue:
        entry, m2_entry, depth = queue.pop(0)
        if id(entry) in drafts:
            continue
        draft = label_entry(ctx, entry, m2_entry, depth)
        draft.is_goal = entry is goal
        drafts[id(entry)] = draft
        for lab in draft.labels:
            if lab.source is not None and id(lab.source.entry) not in drafts:
                queue.append((lab.source.entry, lab.source.m2_entry, depth + 1))
    prov = Provenance(drafts[id(goal)], list(drafts.values()), ctx.warnings)
    for lab in prov.unknowns:
        prov.warnings.append(
            f"unknown value in {lab.address} ({'; '.join(lab.notes)}): action needs tier 'browser'"
        )
    return prov
