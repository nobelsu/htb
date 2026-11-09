"""M4 provenance: label every field of the goal request (and, recursively, of each new step).

Rules, in order (docs/harmap.md, "M4 provenance algorithm"):

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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from spider.mappers.harmap.helpers import jsonpaths, placeholders, restli
from spider.mappers.harmap.helpers.runs import Bundle
from spider.mappers.harmap.infer.goal import counterpart, field_mentions, value_forms
from spider.mappers.harmap.infer.load import Entry
from spider.mappers.harmap.infer.normalize import Field, leaf_fields

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
VERSION_RE = re.compile(r"^(v\d+(\.\d+){0,3}|\d+(\.\d+){1,3})$")  # v2, 1.4.2; not a bare number (an id)
MIME_RE = re.compile(r"^[a-z*]+/[a-z0-9.+*-]+(;.*)?$", re.I)
# a dotted code name (``proto.sdui.actions.core.ShowToast``): part of the site's code, not a fetched value
CODE_NAME_RE = re.compile(r"^[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)+$")
CAMEL_NAME_RE = re.compile(r"^[a-z]+([A-Z][a-z0-9]+){2,}$")  # invitationManagerReceivedAllCount


def code_like(value: Any) -> bool:
    """A name from the site's code (a dotted class name, a camelCase key): never an item's detail."""
    s = as_text(value)
    return bool(CODE_NAME_RE.match(s) or CAMEL_NAME_RE.match(s))


# words that say nothing about which input a field carries
GENERIC_WORDS = frozenset(
    {"id", "ids", "urn", "urns", "key", "value", "type", "url", "path", "query", "body"}
)
PAGING_INPUTS = frozenset({"start", "count", "cursor", "page", "offset", "limit", "next_token"})

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
    picks: list[str] = field(default_factory=list)  # inputs chosen by clicking an item (the explorer's)


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
    # (spec rule 5 names only URL paths; see docs/harmap.md "Form bodies")
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
    for e in earlier:  # earliest first: a value every response repeats comes from the first (page load)
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
    order = {id(e): i for i, e in enumerate(earlier)}
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
) -> tuple[Source | None, Source | None]:
    """``(source, loose)``: the earlier response a value comes from, and (when no extract can pick it out
    alone) the first page it merely appears on, which is enough to tell a picked item."""
    earlier = [e for e in ctx.m1.entries if e.started < entry.started and e is not entry]
    hits = _hits(earlier, f)
    if f2 is not None and f2.value == f.value:  # a fixed value: from the first page holding it, not
        order = {id(e): i for i, e in enumerate(earlier)}  # a later prefetch of some other page
        hits.sort(key=lambda h: order[id(h.entry)])
    loose: Source | None = None
    if not hits:
        return None, None
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
                loose = loose or hit
                continue
        if m2e is None or text2 is None:
            first = first or hit
            continue
        if _evaluate(hit, m2e) == [text2] and _evaluate(hit, hit.entry) == [text1]:
            hit.confirmed = True
            return hit, None
        first = first or hit
    return first, loose


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

    # 3. earlier response. A value that differs between the runs, or looks like a token, may have been
    # fetched. One m2 never sent, and that is no token, is part of the site's code: searching for it finds
    # pages that merely repeat it (e.g. the member's own profile) and adds them as needless steps.
    same = f2 is not None and f2.value == f.value
    varies = (f2 is not None and not same) or (f2 is None and ctx.m2 is None)
    fixed_token = token_like(f.value) and not CODE_NAME_RE.match(as_text(f.value))
    # a clicked item's details travel in the address or the body; headers carry page and tracking state
    picking = not same and bool(ctx.picks) and not code_like(f.value) and f.location != "header"
    short_name = isinstance(f.value, str) and len(f.value) >= 3 and f.value.isalpha()  # "Ada", "Jay"
    if (
        depth < MAX_DEPTH
        and (not is_trivial(f.value) or (picking and short_name))
        and (varies or fixed_token)
    ):
        src, loose = _find_source(ctx, f, f2, entry, m2_entry)
        if picking:  # m1 and m2 clicked different items: the value is the agent's input
            if (
                src is not None
                and not src.confirmed
                and _picked_item(_with_m2_page(ctx, src, f2, m2_entry), f, f2)
            ):
                return _pick_label(f, f2, "", notes)
            if (
                src is None
                and loose is not None
                and _picked_item(_with_m2_page(ctx, loose, f2, m2_entry), f, f2)
            ):
                return _pick_label(f, f2, "", notes)
        # a plain word only one recorded page held (a prefetch m2 never made) is the site's own word:
        # no reason to fetch that page for it
        lone = src is not None and ctx.m2 is not None and not src.confirmed and src.m2_entry is None
        if lone and not token_like(f.value):
            src = None
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


def _general(path: str) -> str:
    return re.sub(r"\[\d+\]", "[*]", path)


def _with_m2_page(ctx: _Ctx, src: Source, f2: Field | None, m2_entry: Entry | None) -> Source:
    """A page source with no m2 counterpart (m2 opened the list at another address, e.g. after a
    redirect) gets the latest m2 page, before m2's request, that shows m2's value."""
    if src.m2_entry is not None or ctx.m2 is None or f2 is None or src.kind not in ("html", "text", "stream"):
        return src
    text2 = as_text(f2.value)
    before = m2_entry.started if m2_entry is not None else None
    pages = [
        e
        for e in ctx.m2.entries
        if e.response_kind == src.kind
        and (before is None or e.started < before)
        and text2 in (e.response_body or "")
    ]
    if pages:
        src.m2_entry = pages[-1]
    return src


def _picked_item(src: Source, f: Field, f2: Field | None) -> bool:
    """m1 and m2 picked different items of one list: m2's value sits in the same list as m1's (same path,
    another index), where a dependency would sit at the very same spot in both runs."""
    m2e, text2 = src.m2_entry, as_text(f2.value) if f2 is not None else None
    if m2e is None or not text2:
        return False
    if src.kind == "json" and m2e.response_json is not None:
        p1 = src.extract["expr"]
        return any(
            _leaf_equals(leaf, text2) and p != p1 and _general(p) == _general(p1)
            for p, leaf in jsonpaths.flatten(m2e.response_json)
        )
    return src.kind in ("html", "text", "stream") and text2 in (m2e.response_body or "")


def _words(text: str) -> set[str]:
    """``payload.invitationId`` → {payload, invitation} (camelCase split, generic words left out)."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {w for w in re.findall(r"[a-z]+", spaced.lower()) if w not in GENERIC_WORDS}


ROLES = {
    "id": "id",
    "ids": "id",
    "urn": "id",
    "urns": "id",
    "url": "url",
    "link": "url",
    "href": "url",
    "token": "token",
    "name": "name",
}


def _role(text: str) -> str | None:
    """What kind of value a name or key ends in: ``profileId`` / ``profile_urn`` → "id"."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    words = re.findall(r"[a-z]+", spaced.lower())
    return ROLES.get(words[-1]) if words else None


def _core(lab: Label) -> str:
    """The part of a picked value that changes with the item: m1's value without the start and end it
    shares with m2's (cut at a separator), e.g. ``https://site/in/jane-doe`` vs ``…/in/max`` →
    ``jane-doe``. Two fields built from one item's slug or number then share one input."""
    a = as_text(lab.field.value)
    b = as_text(lab.m2.value) if lab.m2 is not None else ""
    if not b or a == b:
        return a
    pre = _common_prefix(a, b)
    pre = pre[: max((pre.rfind(c) + 1 for c in "/:=?&#-_.;,| "), default=0)]
    rest_a, rest_b = a[len(pre) :], b[len(pre) :]
    suf = _common_suffix(rest_a, rest_b)  # "=" ends base64 padding, not a separate part
    cut = min((suf.find(c) for c in "/:?&#-_.;,| " if c in suf), default=len(suf))
    suf = suf[cut:]
    core = rest_a[: len(rest_a) - len(suf)] if suf else rest_a
    return core if len(core) >= 3 else a


def _score(name: str, keys: list[str]) -> float:
    """How well an input name fits a value's field keys: shared words, plus half a point when both end
    in the same kind of thing (an id, a url, a token), less a little for each word the key adds."""
    want = _words(name)
    best = 0.0
    for key in keys:
        got = _words(key)
        shared = len(want & got)
        if not shared:
            continue
        role = 0.5 if _role(name) and _role(name) == _role(key) else 0.0
        best = max(best, shared + role - 0.01 * len(got - want))
    return best


def _field_key(lab: Label) -> str:
    """The field's own key: ``body.$.payload.isDisabled.key`` → ``isDisabled.key``'s meaningful part."""
    keys = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", lab.field.address.split(".", 1)[-1])
    meaningful = [k for k in keys if _words(k)]
    return meaningful[-1] if meaningful else ""


Namer = Callable[[list[dict]], "list[str] | None"]


def _key_name(lab: Label) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", _field_key(lab)).lower()


def name_picks(
    drafts: list[StepDraft], picks: list[str], others: list[str], namer: Namer | None = None
) -> None:
    """Name every picked field. A value is reduced to the part that changes with the item (``_core``);
    fields sharing that part carry one input, and two different parts never share one. An unknown field
    holding exactly such a part (the member number inside a urn) carries it too. With several parts an
    optional ``namer`` (the LLM) names them all; else each takes the listed input that best fits its
    fields' keys (the picked inputs first, then the ones the site supplies, like an invitation's token),
    a picked input still free, or its field's own key."""
    names = list(dict.fromkeys([*picks, *(o for o in others if o not in PAGING_INPUTS)]))
    labs = [lab for d in drafts for lab in d.labels if lab.encoding == "picked" and not lab.params[0]]
    if picks and not labs:  # an item was clicked but no field changed with it: a value read off a page
        for d in drafts:  # that differs between the runs is that item (the page held the clicked one)
            for i, lab in enumerate(d.labels):
                src = lab.source
                if (
                    lab.provenance == "dependency"
                    and src is not None
                    and src.kind in ("html", "text", "stream")
                    and lab.field.location != "header"
                    and lab.m2 is not None
                    and lab.m2.value != lab.field.value
                    and not code_like(lab.field.value)
                ):
                    d.labels[i] = _pick_label(lab.field, lab.m2, "", lab.notes)
                    labs.append(d.labels[i])
    # a field that is plainly one listed input ("conversationUrn" for conversation_urn) takes the whole
    # value, as the agent gets it from another action, even where part of it is the same in both runs
    cores = {
        id(lab): as_text(lab.field.value)
        for lab in labs
        if any(_score(n, [_field_key(lab)]) >= 1.5 for n in names)
    }

    def core_of(lab: Label) -> str:
        return cores.get(id(lab)) or _core(lab)

    groups: dict[str, list[Label]] = {}
    for lab in labs:
        groups.setdefault(core_of(lab), []).append(lab)
    # the same detail elsewhere is the same input: an unknown whose changing part it is (its number inside
    # a urn), or a value some earlier page was searched for (the compose page's recipient)
    for d in drafts:
        for i, lab in enumerate(d.labels):
            if lab.provenance not in ("unknown", "dependency") or code_like(lab.field.value):
                continue
            if lab.field.location == "header":
                continue
            key = as_text(lab.field.value) if lab.provenance == "dependency" else _core(lab)
            if key in groups:
                d.labels[i] = _pick_label(lab.field, lab.m2, "", [], key)
                cores[id(d.labels[i])] = key
                groups[key].append(d.labels[i])
    if not groups:
        return
    chosen: dict[str, str] = {}
    if namer is not None and len(groups) > 1:
        asked = [
            {
                "keys": sorted({m.field.address.split(".", 1)[-1][-80:] for m in members}),
                "m1": core,
                "m2": _core_m2(members[0], core),
            }
            for core, members in groups.items()
        ]
        got = namer(asked)
        if got and len(got) == len(groups) and len(set(got)) == len(got):
            chosen = dict(zip(groups, got, strict=True))
    if not chosen:
        scored = []
        for core, members in groups.items():
            keys = [_field_key(m) for m in members]
            for n in names:
                score = _score(n, keys)
                if score:
                    scored.append((-score, names.index(n), core, n))
        for _, _, core, n in sorted(scored):
            if core not in chosen and n not in chosen.values():
                chosen[core] = n
        for core, members in groups.items():  # left over: a free picked input, else the field's own key
            if core in chosen:
                continue
            free = [p for p in picks if p not in chosen.values()]
            # named after the field holding the most of it ("memberId" over "state:…:<number>")
            best = max(
                members, key=lambda m: (len(core) / max(1, len(as_text(m.field.value))), -len(_key_name(m)))
            )
            name = free[0] if free else (_key_name(best) or "item")
            while name in chosen.values():
                name += "_2"
            chosen[core] = name
    for d in drafts:
        for i, lab in enumerate(d.labels):
            if lab.encoding == "picked" and not lab.params[0]:
                core = core_of(lab)
                d.labels[i] = _pick_label(lab.field, lab.m2, chosen[core], lab.notes, core)


def _core_m2(lab: Label, core: str) -> str:
    """m2's counterpart of ``core``: m2's value with the same start and end cut off."""
    a, b = as_text(lab.field.value), as_text(lab.m2.value) if lab.m2 is not None else ""
    i = a.find(core)
    if not b or i < 0:
        return b
    end = len(a) - i - len(core)
    return b[i : len(b) - end if end else len(b)]


def _pick_label(f: Field, f2: Field | None, name: str, notes: list[str], core: str | None = None) -> Label:
    """``name`` empty: named later by ``name_picks``, once every picked field of the action is known.
    ``core``: the part of the value the input fills (the whole value by default)."""
    filt = filter_for(f.context)
    value = as_text(f.value)
    part = core or value
    template = splice(value, [(part, placeholders.param(name, filt))], lambda t: literal_for(f.context, t))
    if name:
        notes = [*notes, f"picked: differs with the item chosen ({name})"]
    return Label(
        f, "param", template=template, params=(name,), filter=filt, encoding="picked", m2=f2, notes=notes
    )


def label_entry(ctx: _Ctx, entry: Entry, m2_entry: Entry | None, depth: int) -> StepDraft:
    m2_fields = ctx.m2.field_map(m2_entry) if ctx.m2 is not None and m2_entry is not None else {}
    labels = [
        label_field(ctx, f, m2_fields.get(f.address), entry, m2_entry, depth)
        for f in leaf_fields(ctx.m1.fields(entry))
    ]
    return StepDraft(entry, m2_entry, depth, labels)


def _reachable(drafts: dict[int, StepDraft], goal: Entry) -> list[StepDraft]:
    """The drafts the goal still needs once picked values became inputs (a page searched only for the
    picked item is no longer fetched)."""
    keep, todo = {id(goal)}, [drafts[id(goal)]]
    while todo:
        for lab in todo.pop().labels:
            if lab.source is not None and id(lab.source.entry) not in keep and id(lab.source.entry) in drafts:
                keep.add(id(lab.source.entry))
                todo.append(drafts[id(lab.source.entry)])
    return [d for k, d in drafts.items() if k in keep]


def trace(
    m1: Bundle,
    goal: Entry,
    m2: Bundle | None = None,
    m2_goal: Entry | None = None,
    namer: Namer | None = None,
) -> Provenance:
    """Label the goal request and every request it depends on (breadth-first, max depth 4).
    ``namer``: optional, names the inputs one picked item carries (see ``name_picks``)."""
    ctx = _Ctx(
        m1=m1,
        m2=m2,
        values_a=m1.values,
        values_b=m2.values if m2 is not None else {},
        base_url=goal.base_url,
        capture_time=m1.started_at,
        picks=[k for k, how in (m1.meta.get("inputs_via") or {}).items() if how == "picked"],
    )
    others = [k for k, how in (m1.meta.get("inputs_via") or {}).items() if how == "other"]
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
    name_picks(list(drafts.values()), ctx.picks, others, namer)
    prov = Provenance(drafts[id(goal)], _reachable(drafts, goal), ctx.warnings)
    for lab in prov.unknowns:
        prov.warnings.append(
            f"unknown value in {lab.address} ({'; '.join(lab.notes)}): action needs tier 'browser'"
        )
    return prov
