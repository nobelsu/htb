"""Infer one card action from a flow's capture runs. Pure: no browser, no network, no LLM.

1. **Pick the action request.** Requests are grouped across runs by method, host and path shape. Third-party
   requests are ignored, and so are requests carrying only part of a marker (a typeahead). The winner carries
   the inputs (markers in marker runs, real values in real runs), its response holds the values the page
   showed, and it was sent after the inputs were entered (post-submit).
2. **Template it by comparing runs.** Each part (path segment, query value, header, body value) is an input
   (with the filter its place needs), a constant (same in every run), or a value that changes per run, whose
   source must be found: a cookie the site set, or an earlier response (a multi-step action; the extract must
   match exactly once). Cache busters are dropped. Anything else is reported.
3. **Output.** The list (or object) in the response whose items match at least 60% of the visible values;
   candidate fields are every leaf the items share, for the labeller to name and keep.
4. **Pagination.** Page 2's request differs in one number: ``offset:N``, ``page_size`` and ``next_params``,
   with ``until`` from a total in the response.
5. **Safety.** A non-GET request is a write (unless the flow says read), and a write always has
   ``requires_confirmation: true``.

Inference fails loudly (``InferError``, with reasons) rather than guess.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlsplit

from spider.mappers.flowmap.record.capture import SECRET, Exchange, Run
from spider.mappers.flowmap.record.flows import Flow, Goto, Select, placeholders, render
from spider.mappers.flowmap.record.holdback import HELD_HEADER
from spider.run.request.extract import STREAM_ROW, css_items, html_selector, resolve_rows, stream_rows

# Endpoints that sound like the page's background chatter, not an action: only ever a tie-breaker.
BACKGROUND = re.compile(
    r"realtime|subscri|presence|track|telemetry|metric|analytic|beacon|collect|/events?\b|/log\b|ping", re.I
)
# A path that is one random-looking token (``/tapi2hqLqA7G0fpO``): a bot-detection or tracking beacon.
OPAQUE = re.compile(r"^/?(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{10,}/?$")
OUTPUT_MATCH = 0.6  # share of visible values the output must account for
MIN_CONFIDENCE = 0.5
# Request headers the browser or the HTTP client sets itself; never part of a card.
SKIP_HEADERS = {
    "host",
    "content-length",
    "connection",
    "cookie",
    "accept-encoding",
    "user-agent",
    "upgrade-insecure-requests",
    "priority",
    "pragma",
    "cache-control",
    "dnt",
    "te",
    "accept-language",
    "if-none-match",
    "if-modified-since",
}
SKIP_HEADER_PREFIXES = ("sec-ch-", "sec-fetch-", ":")
CACHE_BUSTER_KEYS = {"_", "t", "ts", "cb", "_t", "nocache", "cachebuster", "timestamp"}
TOTAL_NAMES = re.compile(
    r"^(total|totalresults|total_results|totalcount|total_count|count|numfound|hits|nbhits)$", re.I
)
PERSISTED_QUERY = re.compile(r"queryid|sha256hash|persistedquery|operationid", re.I)
_REG_SUFFIXES = {"co", "com", "org", "net", "ac", "gov", "edu"}


class InferError(Exception):
    def __init__(self, message: str, reasons: list[str] | None = None) -> None:
        super().__init__(message)
        self.reasons = reasons or []


@dataclass
class Inferred:
    """A draft card action plus what the labeller and the assembler need."""

    action: str
    kind: str  # read | write
    base_url: str
    steps: list[dict[str, Any]]
    params: dict[str, dict[str, Any]]
    output: dict[str, Any]
    examples: list[dict[str, Any]]
    auth_provider: str | None
    changed_signals: list[str]
    confidence: float
    reasons: list[str]
    field_samples: dict[str, list[Any]] = field(default_factory=dict)  # candidate field path → sample values
    page_param: str | None = None


# ---------- small helpers ----------


def registrable(host: str) -> str:
    """example.com for www.api.example.com, foo.co.uk for a.foo.co.uk; IPs stay as they are."""
    labels = host.split(".")
    if host.replace(".", "").isdigit() or len(labels) <= 2:
        return host
    if labels[-2] in _REG_SUFFIXES and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_site(url: str, site_hosts: set[str]) -> bool:
    host = urlsplit(url).hostname or ""
    return any(host == h or registrable(host) == registrable(h) for h in site_hosts if h)


def norm(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    return " ".join(text.casefold().split()).strip(" .,:;!?\"'()[]")


def visible_match(leaf: Any, visible: str) -> bool:
    a, b = norm(leaf), norm(visible)
    if not a or not b:
        return False
    return a == b or (len(a) >= 3 and a in b) or (len(b) >= 4 and b in a)


def value_forms(value: str) -> set[str]:
    """Wire forms of a typed value. Adapted from mappers/harmap/infer/goal.py (DECISIONS M3)."""
    return {value, quote(value, safe=""), quote_plus(value), json.dumps(value)[1:-1], escape_restli(value)}


def escape_restli(value: str) -> str:
    out = value.replace("%", "%25")
    for ch, enc in (("(", "%28"), (")", "%29"), (",", "%2C"), (":", "%3A"), ("'", "%27"), (" ", "%20")):
        out = out.replace(ch, enc)
    return out


def splice(value: str, replacements: list[tuple[str, str]]) -> str:
    """Replace each needle in ``value`` by its placeholder, left to right, without overlaps.
    Adapted from mappers/harmap/infer/provenance.py (DECISIONS M3)."""
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
        out.append(value[pos:s])
        out.append(placeholder)
        pos = e
    out.append(value[pos:])
    return "".join(out)


def _json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


# A streamed answer's JSON rows (CARD_SPEC "Streamed rows"): what a `regex` + `resolve: "rows"` extract reads.
STREAM_EXPR = r"(?m)^[0-9a-f]{1,8}:(\[.*\]|\{.*\})$"


def _stream_doc(body: str | None) -> list[Any] | None:
    """A streamed answer (React Server Components rows, LinkedIn's newer pages) as one JSON document: each
    object/array row with its pointers to other rows filled in. None when the body isn't such a stream."""
    if not body or len(STREAM_ROW.findall(body[:200_000])) < 2:
        return None
    rows = stream_rows(body)
    # Only the top rows (no other row points to them): a row that's pointed to is already inside one, filled
    # in, and reading it on its own too would find each result twice.
    pointed = set(re.findall(r'"\$[L@]?([0-9a-f]{1,8})(?=[":])', body))
    top = [v for k, v in rows.items() if k not in pointed and isinstance(v, (dict, list))]
    return [resolve_rows(v, rows) for v in top] or None


def _doc(body: str | None) -> Any:
    """The response as JSON: plain JSON, or a streamed answer's rows filled in."""
    doc = _json(body)
    return doc if doc is not None else _stream_doc(body)


def _ctype(headers: Iterable[tuple[str, str]]) -> str:
    return next((v for k, v in headers if k.lower() == "content-type"), "")


def content_kind(ex: Exchange) -> str:
    c = _ctype(ex.response_headers).lower()
    if "json" in c:
        return "json"
    if "html" in c:
        return "html"
    return "text" if c.startswith("text") else "any"


def jp(path: tuple[Any, ...]) -> str:
    """A JSONPath for a concrete or generalized path (``"*"`` = every index)."""
    out = "$"
    for seg in path:
        if seg == "*":
            out += "[*]"
        elif isinstance(seg, int):
            out += f"[{seg}]"
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", seg):
            out += f".{seg}"
        else:
            out += "['" + seg.replace("'", "\\'") + "']"
    return out


def leaves(doc: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[tuple[Any, ...], Any]]:
    """Every scalar leaf with its concrete path."""
    if isinstance(doc, dict):
        for k, v in doc.items():
            yield from leaves(v, (*path, k))
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            yield from leaves(v, (*path, i))
    else:
        yield path, doc


def general(path: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple("*" if isinstance(s, int) else s for s in path)


# ---------- request parts ----------


@dataclass(frozen=True)
class Part:
    where: str  # path | query | header | json | form | text
    key: Any  # segment index, query name, header name, JSON path tuple, form name, or ""
    value: str


def parts(ex: Exchange) -> list[Part]:
    url = urlsplit(ex.url)
    out = [Part("path", i, unquote(seg)) for i, seg in enumerate(s for s in url.path.split("/") if s)]
    seen: set[str] = set()
    for k, v in parse_qsl(url.query, keep_blank_values=True):
        if k not in seen:
            seen.add(k)
            out.append(Part("query", k, v))
    for k, v in ex.request_headers:
        low = k.lower()
        if low not in SKIP_HEADERS and not low.startswith(SKIP_HEADER_PREFIXES):
            out.append(Part("header", low, v))
    if ex.post_data:
        body = _json(ex.post_data)
        if isinstance(body, dict | list):
            out += [Part("json", p, v if isinstance(v, str) else json.dumps(v)) for p, v in leaves(body)]
        elif "x-www-form-urlencoded" in _ctype(ex.request_headers):
            out += [Part("form", k, v) for k, v in parse_qsl(ex.post_data, keep_blank_values=True)]
        else:
            out.append(Part("text", "", ex.post_data))
    return out


def carries(ex: Exchange, value: str) -> bool:
    if not value:
        return False
    if any(value in p.value for p in parts(ex)):
        return True
    raw = ex.url + (ex.post_data or "")
    return any(f in raw for f in value_forms(value))


def partially_carries(ex: Exchange, marker: str) -> bool:
    """A typeahead sends what's typed so far: a prefix of the marker, never all of it."""
    return not carries(ex, marker) and any(carries(ex, marker[:n]) for n in range(3, len(marker)))


def path_shape(ex: Exchange, values: Iterable[str]) -> tuple[str, str, str]:
    url = urlsplit(ex.url)
    vals = [v for v in values if v]
    segs = ["*" if any(v in unquote(s) for v in vals) else s for s in url.path.split("/")]
    return ex.method, url.hostname or "", "/".join(segs)


# ---------- the inference ----------


@dataclass
class _Ctx:
    flow: Flow
    runs: list[Run]
    site_hosts: set[str]
    reasons: list[str] = field(default_factory=list)
    penalty: float = 0.0
    step_ids: dict[tuple[str, str, str], str] = field(default_factory=dict)
    prior: list[dict[str, Any]] = field(default_factory=list)  # earlier steps, in order

    def doubt(self, reason: str, weight: float) -> None:
        self.reasons.append(reason)
        self.penalty += weight


def infer(flow: Flow, runs: list[Run], prefer: tuple[str, str, str] | None = None) -> Inferred:
    runs = [r for r in runs if r.error is None] or runs
    failed = [r.run_id for r in runs if r.error]
    if failed:
        raise InferError("every capture run failed", [f"{r.run_id}: {r.error}" for r in runs])
    real = [r for r in runs if r.kind == "real"]
    if not real:
        raise InferError("no real run to infer from")
    hosts = {
        urlsplit(render(s.url, flow.example_values(), url=True)).hostname or ""
        for s in flow.steps
        if isinstance(s, Goto)
    }
    ctx = _Ctx(flow, runs, hosts | {flow.domain})

    groups = _groups(ctx)
    main_sig, main = _pick(ctx, groups, prefer)
    method = main_sig[0]
    kind = (
        "write" if (method not in ("GET", "HEAD") and flow.kind != "read") or flow.kind == "write" else "read"
    )
    if method not in ("GET", "HEAD") and kind == "read":
        ctx.doubt(f"{method} request treated as a read because the flow says read", 0.0)

    base = _origin(main[real[0].run_id].url)
    page_param = None
    output: dict[str, Any] = {"from": None}
    field_samples: dict[str, list[Any]] = {}
    main_id = _step_id(ctx, main_sig, main, fallback="main")
    extract: dict[str, Any] = {}
    expect_paths: list[str] = []
    page = content_kind(main[real[0].run_id]) == "html" and kind == "read"
    if page:  # the page itself holds the results (rendered on the server): read them with CSS (level 2)
        selector, items = _html_output(ctx, main)
        extract["items"] = {"from": "body", "type": "css", "expr": selector}
        fields, field_samples = _fields(items, ("text",))
        output = {"from": f"steps.{main_id}.items", "fields": fields}
        ctx.reasons.append("the page renders its results on the server: they're read from its HTML")
    elif kind == "read":  # a write was held back: its only response is the stand-in, nothing to read
        found = _output(ctx, main)
        if found is not None:
            from_expr, container, fields, field_samples, _ = found
            first_body = main[real[0].run_id].body
            streamed = _json(first_body) is None and _stream_doc(first_body) is not None
            if streamed and from_expr.startswith("$[*]"):
                # A streamed answer: every JSON row, pointers filled in, then the results inside a row.
                extract["items"] = {
                    "from": "body",
                    "type": "regex",
                    "expr": STREAM_EXPR,
                    "parse": "json",
                    "resolve": "rows",
                    "then": "$" + from_expr[len("$[*]") :] or "$",
                }
                ctx.reasons.append("the answer is a stream of rows: read with resolve: rows")
                expect_paths = []
            else:
                extract["items"] = {"from": "body", "type": "jsonpath", "expr": from_expr}
                expect_paths = [container] if container != "$" else []
            output = {"from": f"steps.{main_id}.items", "fields": fields}
        elif kind == "read":
            raise InferError("the response doesn't hold what the page showed", ctx.reasons)

    page_info = _pagination(ctx, main) if kind == "read" and not page else None
    request = _template_request(ctx, main_sig, main, base, page_info)
    if page_info is not None:
        page_param = page_info["param"]
        output["limit"] = page_info["page_size"]
        nxt: dict[str, Any] = {"param": page_param, "increment": 1, "page_size": page_info["page_size"]}
        if page_info.get("total") is not None:
            extract["total"] = {"from": "body", "type": "jsonpath", "expr": page_info["total"]}
            nxt["until"] = f"steps.{main_id}.total"
        output["next_params"] = nxt

    first = main[real[0].run_id]
    statuses = sorted({main[r.run_id].status or 0 for r in runs if r.run_id in main and r.kind == "real"})
    step = {
        "id": main_id,
        "request": request,
        "expect": {"status": statuses, "content_type": content_kind(first), "paths": expect_paths},
        "extract": extract,
    }
    params = _params(ctx, page_param)
    examples: list[dict[str, Any]] = []
    if kind == "read":
        for r in real[:2]:
            examples.append({"params": {k: r.values[k] for k in flow.inputs}, "min_items": 1})
    changed = (
        ["body_contains:PersistedQueryNotFound"]
        if any(PERSISTED_QUERY.search(str(k)) for k in request.get("query", {}))
        else []
    )
    confidence = round(max(0.0, 1.0 - ctx.penalty), 2)
    if confidence < MIN_CONFIDENCE:
        raise InferError(f"too unsure to write a card (confidence {confidence})", ctx.reasons)
    return Inferred(
        action=flow.action,
        kind=kind,
        base_url=base,
        steps=[*ctx.prior, step],
        params=params,
        output=output,
        examples=examples,
        auth_provider=flow.auth_provider,
        changed_signals=changed,
        confidence=confidence,
        reasons=ctx.reasons,
        field_samples=field_samples,
        page_param=page_param,
    )


def _origin(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}"


def _all_values(run: Run) -> list[str]:
    return [v for v in run.values.values() if v]


def _groups(ctx: _Ctx) -> dict[tuple[str, str, str], dict[str, Exchange]]:
    """Same-site requests grouped by (method, host, path shape); the first exchange per run, or the first that
    carries the run's input values."""
    groups: dict[tuple[str, str, str], dict[str, Exchange]] = {}
    for run in ctx.runs:
        for ex in run.exchanges:
            if ex.phase != "flow" or not same_site(ex.url, ctx.site_hosts) or ex.status is None:
                continue
            sig = path_shape(ex, _all_values(run))
            group = groups.setdefault(sig, {})
            held = group.get(run.run_id)
            if held is None:
                group[run.run_id] = ex
            elif not _carries_any(held, run) and _carries_any(ex, run):
                # The same page blank first, then with the input (a GET form): the second is the action.
                group[run.run_id] = ex
            elif held.status and 300 <= held.status < 400 and ex.redirected_from == held.id:
                group[run.run_id] = ex  # redirected to the same place (LinkedIn's 307): use the target
    return groups


def _carries_any(ex: Exchange, run: Run) -> bool:
    return any(v and carries(ex, v) for v in _all_values(run))


def _held(ex: Exchange) -> bool:
    return any(k.lower() == HELD_HEADER for k, _ in ex.response_headers)


def _submit_step(flow: Flow) -> int:
    used = [i for i, s in enumerate(flow.steps) if placeholders(s)]
    return max(used) if used else 0


def background(path: str) -> bool:
    """A live update, a tracking ping or a beacon (never the action itself, when there's a choice)."""
    return bool(BACKGROUND.search(path) or OPAQUE.match(urlsplit(path).path or path))


def _pick(
    ctx: _Ctx,
    groups: dict[tuple[str, str, str], dict[str, Exchange]],
    prefer: tuple[str, str, str] | None = None,
) -> tuple[Any, dict[str, Exchange]]:
    scored = _ranked(ctx, groups)
    if prefer is not None:  # confirmed by blocking it (serve/server.py): the others are noise
        chosen = [t for t in scored if t[1] == prefer]
        if chosen:
            ctx.reasons.append("confirmed by blocking it: without it the results don't appear")
            scored = chosen + [t for t in scored if t[1] != prefer and t[0] != chosen[0][0]]
    real_ids = {r.run_id for r in ctx.runs if r.kind == "real"}
    in_every_real = [t for t in scored if real_ids <= set(t[2])]
    if in_every_real and in_every_real[0] is not scored[0]:
        # The top one isn't there in every real run (a widget that loads now and then): the best that is.
        ctx.reasons.append(f"{scored[0][1][2]} wasn't in every real run; the next best that is was used")
        scored = in_every_real + [t for t in scored if t not in in_every_real]
    best = scored[0]
    if len(scored) > 1 and scored[1][0] == best[0]:
        raise InferError(
            "two requests fit equally well",
            [f"{s[1][0]} {s[1][1]}{s[1][2]}" for s in scored[:2]],
        )
    if best[0][4] == 0:  # post: sent after the inputs were entered
        ctx.doubt("the chosen request was sent before the inputs were entered", 0.2)
    missing = [r.run_id for r in ctx.runs if r.run_id not in best[2]]
    if any(r.kind == "real" for r in ctx.runs if r.run_id in missing):
        raise InferError("the action request is missing from some real runs", missing)
    if missing:
        ctx.doubt(f"the action request wasn't sent in marker runs {', '.join(missing)}", 0.1)
    return best[1], best[2]


def candidates(flow: Flow, runs: list[Run], limit: int = 3) -> list[tuple[str, str, str]]:
    """The likeliest requests behind the action, best first (method, host, path shape), to block-test."""
    runs = [r for r in runs if r.error is None] or runs
    hosts = {
        urlsplit(render(s.url, flow.example_values(), url=True)).hostname or ""
        for s in flow.steps
        if isinstance(s, Goto)
    }
    ctx = _Ctx(flow, runs, hosts | {flow.domain})
    try:
        return [t[1] for t in _ranked(ctx, _groups(ctx))[:limit]]
    except InferError:
        return []


def request_sig(method: str, url: str, values: Iterable[str]) -> tuple[str, str, str]:
    """A live request's (method, host, path shape), as candidates() names them."""
    parts = urlsplit(url)
    vals = [v for v in values if v]
    segs = ["*" if any(v in unquote(s) for v in vals) else s for s in parts.path.split("/")]
    return method, parts.hostname or "", "/".join(segs)


def _ranked(
    ctx: _Ctx, groups: dict[tuple[str, str, str], dict[str, Exchange]]
) -> list[tuple[Any, tuple[str, str, str], dict[str, Exchange]]]:
    flow = ctx.flow
    submit = _submit_step(flow)
    submit_is_goto = isinstance(flow.steps[submit], Goto)
    marker_runs = [r for r in ctx.runs if r.kind == "marker"]
    real = [r for r in ctx.runs if r.kind == "real"]
    scored = []
    held = {sig for sig, by_run in groups.items() if any(_held(ex) for ex in by_run.values())}
    for sig, by_run in groups.items():
        if flow.kind == "write" and held and sig not in held:
            continue  # the write is the request that was held back (record/holdback.py)
        exs_all = list(by_run.values())
        if flow.kind != "write" and not any((ex.body or "").strip() for ex in exs_all):
            continue  # a read returns something: an always-empty answer is a beacon (analytics, presence)
        partial = False
        marker_hits = 0
        for r in marker_runs:
            ex = by_run.get(r.run_id)
            for m in r.markers.values():
                if ex is not None and carries(ex, m):
                    marker_hits += 1
                elif ex is not None and partially_carries(ex, m):
                    partial = True
        if partial and not marker_hits:
            continue  # a typeahead
        real_hits = sum(
            1
            for r in real
            if r.run_id in by_run
            for v in r.values.values()
            if v and carries(by_run[r.run_id], v)
        )
        out = 0.0
        for r in real:
            ex = by_run.get(r.run_id)
            if ex is not None and r.visible:
                if content_kind(ex) == "html" and flow.results_selector is not None:
                    live = flow.results_selector if isinstance(flow.results_selector, str) else None
                    share = _html_score(ex.body, live, r.visible) if live else 0.0
                    if share < OUTPUT_MATCH:  # the live page's selector may not fit the HTML: build one
                        rebuilt = html_selector(ex.body or "", r.visible)
                        share = max(share, _html_score(ex.body, rebuilt, r.visible) if rebuilt else 0.0)
                    out += share
                else:
                    out += _output_score(_doc(ex.body), r.visible)
        exs = list(by_run.values())
        post = sum(1 for ex in exs if ex.step > submit or (submit_is_goto and ex.step == submit))
        writes = sig[0] not in ("GET", "HEAD")
        json_resp = sum(1 for ex in exs if content_kind(ex) == "json")
        ok = sum(1 for ex in exs if ex.status and 200 <= ex.status < 300)
        if flow.inputs and not marker_hits and not real_hits and not out:
            continue
        score = (
            round(out, 2),
            marker_hits + real_hits,
            int(writes and flow.kind != "read"),
            int(not background(sig[2])),  # on a tie, not a live update or a tracking ping
            post,
            ok,
            json_resp,
        )
        scored.append((score, sig, by_run))
    if not scored:
        raise InferError(
            "no request carries the inputs or what the page showed",
            ["is the action's request sent from another site, or does the page render it on the server?"],
        )
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored


# ---------- output ----------


def _html_score(body: str | None, selector: str, visible: list[str]) -> float:
    """Share of the visible results that ``selector`` finds in an HTML response."""
    if not body or not visible:
        return 0.0
    try:
        texts = [i["text"] for i in css_items(body, selector)]
    except Exception:
        return 0.0
    return sum(1 for vis in visible if any(visible_match(t, vis) for t in texts)) / len(visible)


def _html_output(ctx: _Ctx, main: dict[str, Exchange]) -> tuple[str, list[dict[str, Any]]]:
    """The results read from the page's HTML, checked against every real run. The recorder's selector comes
    from the live page; a site's scripts can restructure the page after it loads, so when that selector
    finds too little in the HTML, one is built from the HTML itself (the same rules)."""
    real = [r for r in ctx.runs if r.kind == "real" and r.visible]
    selector = ctx.flow.results_selector if isinstance(ctx.flow.results_selector, str) else None
    misses = selector is None or any(
        _html_score(main[r.run_id].body, selector, r.visible) < OUTPUT_MATCH for r in real
    )
    if selector is None or misses:
        rebuilt = html_selector(main[real[0].run_id].body or "", real[0].visible) if real else None
        if rebuilt is not None and rebuilt != selector:
            ctx.reasons.append(f"the selector was rebuilt from the page's HTML ({rebuilt!r})")
            selector = rebuilt
    if not selector:
        raise InferError(
            "the page renders its results on the server, and no selector finds them in its HTML",
            ["record again and point at the results, so their selector is known"],
        )
    first: list[dict[str, Any]] = []
    for r in real:
        share = _html_score(main[r.run_id].body, selector, r.visible)
        if share < OUTPUT_MATCH:
            raise InferError(
                "the page's HTML doesn't hold what the page showed",
                [
                    f"{r.run_id}: {selector!r} finds {round(share * 100)}% of the visible results "
                    "(the page may fill them in with scripts)"
                ],
            )
        if share < 1:
            ctx.doubt(f"{r.run_id}: the HTML matches {round(share * 100)}% of the visible results", 0.05)
        if not first:
            first = css_items(main[r.run_id].body or "", selector)
    return selector, first


def _output_score(doc: Any, visible: list[str]) -> float:
    if doc is None or not visible:
        return 0.0
    by_path: dict[tuple[Any, ...], list[Any]] = {}
    for p, v in leaves(doc):
        by_path.setdefault(general(p), []).append(v)
    best = 0.0
    for vals in by_path.values():
        hits = sum(1 for vis in visible if any(visible_match(v, vis) for v in vals))
        best = max(best, hits / len(visible))
    return best


def _best_leaf(doc: Any, visible: list[str]) -> tuple[tuple[Any, ...], float] | None:
    by_path: dict[tuple[Any, ...], list[Any]] = {}
    for p, v in leaves(doc):
        by_path.setdefault(general(p), []).append(v)
    ranked = []
    for gp, vals in by_path.items():
        hits = sum(1 for vis in visible if any(visible_match(v, vis) for v in vals))
        exact = sum(1 for vis in visible if any(norm(v) == norm(vis) for v in vals))
        ranked.append((hits / len(visible), exact, -len(gp), gp))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][3], ranked[0][0]


def _root(gp: tuple[Any, ...]) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """(item root, field path relative to it) for a generalized leaf path."""
    if "*" in gp:
        i = len(gp) - 1 - gp[::-1].index("*")
        return gp[: i + 1], gp[i + 1 :]
    return gp[:-1], gp[-1:]


def _output(
    ctx: _Ctx, main: dict[str, Exchange]
) -> tuple[str, str, dict[str, str], dict[str, list[Any]], int] | None:
    roots = []
    for r in ctx.runs:
        if r.kind != "real" or not r.visible:
            continue
        doc = _doc(main[r.run_id].body)
        if doc is None:
            kind = content_kind(main[r.run_id])
            raise InferError(
                f"the action's response is {kind}, not JSON",
                ["server-rendered pages need regex extraction, which flowmap doesn't do yet"],
            )
        found = _best_leaf(doc, r.visible)
        if found is None or found[1] < OUTPUT_MATCH:
            share = 0 if found is None else round(found[1] * 100)
            raise InferError(
                "the response doesn't hold what the page showed",
                [
                    f"{r.run_id}: the best match covers {share}% of the visible values "
                    f"(needs {round(OUTPUT_MATCH * 100)}%)"
                ],
            )
        if found[1] < 1:
            ctx.doubt(
                f"{r.run_id}: the response matches {round(found[1] * 100)}% of the visible values", 0.05
            )
        roots.append((_root(found[0]), doc, r))
    if not roots:
        if ctx.flow.results_selector is not None:
            ctx.doubt("the page showed no results in any real run, so the output is a guess", 0.3)
        return None
    shapes = {root for (root, _rel), _doc, _r in roots}
    if len(shapes) > 1:
        raise InferError("the runs disagree on where the results are", [jp(s) for s in shapes])
    (root, rel), doc, _ = roots[0]
    items_expr = jp(root)
    container = jp(root[:-1]) if root and root[-1] == "*" else jp(root)
    items = _items(doc, root)
    fields, samples = _fields(items, rel)
    page_size = len(items)
    return items_expr, container, fields, samples, page_size


def _items(doc: Any, root: tuple[Any, ...]) -> list[Any]:
    from jsonpath_ng.ext import parse

    return [m.value for m in parse(jp(root)).find(doc)]


def _fields(
    items: list[Any], matched: tuple[Any, ...], cap: int = 25
) -> tuple[dict[str, str], dict[str, list[Any]]]:
    """Candidate fields: leaf paths (relative to each item) present in at least half the items."""
    counts: dict[tuple[Any, ...], list[Any]] = {}
    for item in items:
        seen = set()
        for p, v in leaves(item):
            gp = general(p)
            if gp in seen or (isinstance(v, str) and SECRET.search(v)):
                continue
            seen.add(gp)
            counts.setdefault(gp, []).append(v)
    keep = [gp for gp, vals in counts.items() if len(vals) * 2 >= max(1, len(items))]
    keep.sort(key=lambda gp: (gp != matched, len(gp)))
    fields = {jp(gp): jp(gp) for gp in keep[:cap]}
    samples = {jp(gp): counts[gp][:3] for gp in keep[:cap]}
    return fields, samples


# ---------- pagination ----------


def _pagination(ctx: _Ctx, main: dict[str, Exchange]) -> dict[str, Any] | None:
    found = []
    for r in ctx.runs:
        if r.kind != "real" or r.visible_page2 is None:
            continue
        ex1 = main[r.run_id]
        sig = path_shape(ex1, _all_values(r))
        ex2 = next(
            (e for e in r.exchanges if e.phase == "next_page" and path_shape(e, _all_values(r)) == sig), None
        )
        if ex2 is None:
            ctx.doubt(f"{r.run_id}: page 2 was shown but its request wasn't the action request", 0.1)
            continue
        p1 = {(p.where, p.key): p.value for p in parts(ex1) if p.where in ("query", "json", "form", "path")}
        p2 = {(p.where, p.key): p.value for p in parts(ex2) if p.where in ("query", "json", "form", "path")}
        changed = [k for k in p2 if p1.get(k) != p2[k] and p2[k].lstrip("-").isdigit()]
        changed = [k for k in changed if k not in p1 or p1[k].lstrip("-").isdigit()]
        if len(changed) != 1:
            ctx.doubt(f"{r.run_id}: page 2 changed {len(changed)} numbers, expected 1; no pagination", 0.1)
            continue
        key = changed[0]
        v1, v2 = int(p1.get(key, "0")), int(p2[key])
        doc = _json(ex1.body)
        found.append((key, v1, v2, doc, ex1, r))
    if not found:
        return None
    keys = {f[0] for f in found}
    if len(keys) > 1:
        ctx.doubt("runs disagree on the page parameter; no pagination", 0.1)
        return None
    key, v1, v2, doc, ex1, r = found[0]
    step = v2 - v1
    items = _output_items_count(ctx, doc, r)
    if v1 == 1 and step == 1:
        template, page_size = "{{params.page}}", items
    elif v1 == 0 and step == 1:
        template, page_size = "{{params.page | offset:1}}", items
    elif v1 == 0 and step > 1:
        template, page_size = f"{{{{params.page | offset:{step}}}}}", step
        if items and items != step:
            ctx.doubt(f"page 1 had {items} items but the offset grows by {step}", 0.1)
    else:
        ctx.doubt(f"page parameter goes {v1} → {v2}, which isn't a page or an offset I know", 0.1)
        return None
    total = _total(doc, items)
    name = "page" if "page" not in ctx.flow.inputs else "page_number"
    return {
        "param": name,
        "key": key,
        "template": template.replace("params.page", f"params.{name}"),
        "page_size": page_size,
        "total": total,
    }


def _output_items_count(ctx: _Ctx, doc: Any, run: Run) -> int:
    found = _best_leaf(doc, run.visible) if doc is not None else None
    if found is None:
        return 0
    root, _ = _root(found[0])
    return len(_items(doc, root))


def _total(doc: Any, page_size: int) -> str | None:
    """A total-results number outside the item list: more than a page, named like a total (total* first)."""
    if not isinstance(doc, dict | list):
        return None
    ranked = []
    for p, v in leaves(doc):
        if not p or any(isinstance(s, int) for s in p) or isinstance(v, bool) or not isinstance(v, int):
            continue
        name = str(p[-1])
        if TOTAL_NAMES.match(name) and v > page_size:
            ranked.append((not name.lower().startswith("total"), len(p), p))
    return jp(min(ranked)[2]) if ranked else None


# ---------- templating ----------


def _filter_for(where: str, whole: str, needle: str) -> str:
    if where == "path":
        return " | urlencode"
    if where == "form":
        return " | urlencode"
    if where == "query" and whole != needle and re.search(r"[(:,]", whole):
        return " | escape_restli"
    return ""


def _template_request(
    ctx: _Ctx,
    sig: tuple[str, str, str],
    by_run: dict[str, Exchange],
    base: str,
    page_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    order = [r for r in ctx.runs if r.run_id in by_run]
    first = by_run[order[0].run_id]
    per_run = {r.run_id: {(p.where, p.key): p.value for p in parts(by_run[r.run_id])} for r in order}
    keys = list(dict.fromkeys(k for r in order for k in per_run[r.run_id]))
    url = urlsplit(first.url)
    path_segments = [s for s in url.path.split("/") if s]
    query: dict[str, Any] = {}
    headers: dict[str, str] = {}
    body_json = _json(first.post_data) if first.post_data else None
    form: list[tuple[str, str]] = []
    text_body: str | None = None
    for key in keys:
        where, k = key
        values = {rid: pv[key] for rid, pv in per_run.items() if key in pv}
        if len(values) < len(order):
            if where == "header":
                continue  # sent in some runs only: optional
            ctx.doubt(f"{where} {k!r} is sent in some runs only", 0.05)
        if page_info is not None and (where, k) == page_info["key"]:
            tmpl: str | None = page_info["template"]
        else:
            tmpl = _template_value(ctx, where, k, values, order, by_run, base)
        if tmpl is None:
            if where == "path":
                raise InferError(f"path segment {k} changes per run and I can't find where it comes from")
            continue
        if where == "path":
            path_segments[k] = tmpl
        elif where == "query":
            query[k] = tmpl
        elif where == "header":
            headers[k] = tmpl
        elif where == "json":
            if "{{" in tmpl:  # constants keep their JSON type as captured
                body_json = _set_path(body_json, k, tmpl)
        elif where == "form":
            form.append((k, tmpl))
        else:
            text_body = tmpl
    if page_info is not None and page_info["key"] not in per_run[order[0].run_id]:
        where, k = page_info["key"]
        if where == "query":
            query[k] = page_info["template"]
    origin = _origin(first.url)
    prefix = "{{base_url}}" if origin == base else origin
    request: dict[str, Any] = {"method": first.method, "url": prefix + "/" + "/".join(path_segments)}
    if url.path.endswith("/") and path_segments:
        request["url"] += "/"
    if query:
        request["query"] = query
    if headers:
        request["headers"] = headers
    if first.post_data:
        if body_json is not None and isinstance(_json(first.post_data), dict | list):
            request["body"] = body_json
        elif form:
            request["body"] = "&".join(f"{k}={v}" for k, v in form)
        else:
            request["body"] = text_body
    return request


def _set_path(doc: Any, path: tuple[Any, ...], value: Any) -> Any:
    """``doc`` with ``value`` at ``path``; a path this run's body doesn't have (the body's shape differs
    between runs, as Amazon's does) is left alone rather than crashing the map."""
    if not path:
        return value
    node = doc
    try:
        for seg in path[:-1]:
            node = node[seg]
        node[path[-1]] = value
    except (KeyError, IndexError, TypeError):
        pass
    return doc


def _template_value(
    ctx: _Ctx,
    where: str,
    key: Any,
    values: dict[str, str],
    order: list[Run],
    by_run: dict[str, Exchange],
    base: str,
) -> str | None:
    vals = list(values.values())
    rids = list(values)
    runs = {r.run_id: r for r in order}
    # 1. an input (or several) inside the value, in every run
    replacements: list[tuple[str, str]] = []
    for name in ctx.flow.inputs:
        needles = [runs[rid].values.get(name, "") for rid in rids]
        if all(n and n in v for n, v in zip(needles, vals, strict=True)):
            filt = _filter_for(where, vals[0], needles[0])
            replacements.append((name, filt))
    if replacements:
        out = vals[0]
        spliced = splice(
            out, [(runs[rids[0]].values[n], "{{params." + n + f + "}}") for n, f in replacements]
        )
        # the rest of the value must be the same in every run
        for rid, v in values.items():
            other = splice(v, [(runs[rid].values[n], "{{params." + n + f + "}}") for n, f in replacements])
            if other != spliced:
                ctx.doubt(f"{where} {key!r}: the text around the input changes per run", 0.1)
                break
        return spliced.replace(base, "{{base_url}}") if where in ("header", "query") else spliced
    # 2. a constant
    if len(set(vals)) == 1 and not SECRET.search(vals[0]):
        if ctx.flow.kind == "write":  # an id the page showed: the same in every run only because they all did
            linked = _ids_from_page(ctx, vals[0], rids, order, by_run)
            if linked is not None:
                return linked
        return vals[0].replace(base, "{{base_url}}") if base in vals[0] else vals[0]
    # 3. changes per run (or is a session value): find where it comes from
    if (where in ("query", "form") and str(key).lower() in CACHE_BUSTER_KEYS) or all(
        _looks_like_time(v) for v in vals
    ):
        ctx.reasons.append(f"dropped {where} {key!r}: a cache buster")
        return None
    source = _find_source(ctx, values, order, by_run)
    if source is not None:
        return source
    if where == "header":
        ctx.doubt(f"dropped header {key!r}: it changes per run and I can't find where it comes from", 0.1)
        return None
    ctx.doubt(f"{where} {key!r} changes per run and I can't find where it comes from", 0.3)
    return None


def _ids_from_page(
    ctx: _Ctx, value: str, rids: list[str], order: list[Run], by_run: dict[str, Exchange]
) -> str | None:
    """A write's fixed value with ids in it (``urn:li:fsd_company:74126343``) that an earlier page, opened
    with the action's inputs, shows: every run followed the same company, so the id never changed, but
    another company has another id. Each such id is read from that page instead."""
    out, found = value, False
    for digits in dict.fromkeys(re.findall(r"\d{4,}", value)):
        source = _find_source(ctx, {rid: digits for rid in rids}, order, by_run, from_input_pages=True)
        if source is not None:
            out, found = out.replace(digits, source), True
    if found:
        ctx.reasons.append(f"ids in {value[:60]!r} are read from the page the inputs opened")
    return out if found else None


def _looks_like_time(v: str) -> bool:
    return v.isdigit() and len(v) in (10, 13) and v[0] == "1"


def _find_source(
    ctx: _Ctx,
    values: dict[str, str],
    order: list[Run],
    by_run: dict[str, Exchange],
    *,
    from_input_pages: bool = False,
) -> str | None:
    """A cookie the site set, or a value from an earlier response, consistent in every run.
    ``from_input_pages``: only a response to a request that carried one of the inputs (no cookies)."""
    runs = {r.run_id: r for r in order}
    # a cookie
    for name in set() if from_input_pages else {c.name for r in order for c in r.cookies}:
        quoted = True
        ok = True
        for rid, v in values.items():
            cookie = next((c.value for c in runs[rid].cookies if c.name == name), None)
            if cookie is None or cookie.strip('"') != v:
                ok = False
                break
            quoted = quoted and cookie != v
        if not ok:
            continue
        filt = " | strip_quotes" if quoted else ""
        setter = _cookie_setter(ctx, name, order, by_run)
        if setter is not None:
            sid, sig, setters = setter
            key = _extract_key(name)
            _add_prior(
                ctx, sid, sig, setters, {key: {"from": "cookies", "type": "jsonpath", "expr": jp((name,))}}
            )
            return "{{steps." + sid + "." + key + filt + "}}"
        if ctx.flow.auth_provider:
            return "{{session.cookies." + name + filt + "}}"
        raise InferError(
            f"the request needs cookie {name!r}, which no request in the flow sets",
            ["it's probably a login cookie: set auth_provider in the flow"],
        )
    # an earlier response
    first_rid = next(iter(values))
    main_first = by_run[first_rid]
    run0 = runs[first_rid]
    inputs = [v for v in run0.values.values() if v]
    for ex in run0.exchanges:
        if ex.id >= main_first.id or not same_site(ex.url, ctx.site_hosts):
            continue
        if from_input_pages and not any(carries(ex, v) for v in inputs):
            continue
        sig = path_shape(ex, _all_values(run0))
        earlier = _same_group(order, sig, by_run)
        if earlier is None:
            continue
        found = _extract_for(values, earlier)
        if found is not None:
            key, extract = found
            sid = _step_id(ctx, sig, earlier, fallback="prepare")
            _add_prior(ctx, sid, sig, earlier, {key: extract})
            return "{{steps." + sid + "." + key + "}}"
    return None


def _same_group(
    order: list[Run], sig: tuple[str, str, str], by_run: dict[str, Exchange]
) -> dict[str, Exchange] | None:
    out = {}
    for r in order:
        limit = by_run[r.run_id].id
        ex = next((e for e in r.exchanges if e.id < limit and path_shape(e, _all_values(r)) == sig), None)
        if ex is None:
            return None
        out[r.run_id] = ex
    return out


def _cookie_setter(
    ctx: _Ctx, name: str, order: list[Run], by_run: dict[str, Exchange]
) -> tuple[str, tuple[str, str, str], dict[str, Exchange]] | None:
    first = order[0]
    for ex in first.exchanges:
        if ex.id >= by_run[first.run_id].id:
            break
        if any(
            k.lower() == "set-cookie" and re.search(rf"(^|\n){re.escape(name)}=", v)
            for k, v in ex.response_headers
        ):
            sig = path_shape(ex, _all_values(first))
            group = _same_group(order, sig, by_run)
            if group is None:
                return None
            return _step_id(ctx, sig, group, fallback="session"), sig, group
    return None


def _extract_for(values: dict[str, str], earlier: dict[str, Exchange]) -> tuple[str, dict[str, Any]] | None:
    rids = list(values)
    # JSON leaf at the same path in every run, matching exactly once
    doc0 = _json(earlier[rids[0]].body)
    if doc0 is not None:
        for p, v in leaves(doc0):
            if str(v) != values[rids[0]] or not p:
                continue
            expr = jp(p)
            if all(_jp_one(_json(earlier[rid].body), expr) == values[rid] for rid in rids):
                return _extract_key(str(p[-1]) if isinstance(p[-1], str) else "value"), {
                    "from": "body",
                    "type": "jsonpath",
                    "expr": expr,
                }
    # response header
    for k, v in earlier[rids[0]].response_headers:
        if v == values[rids[0]] and all(
            dict((a.lower(), b) for a, b in earlier[rid].response_headers).get(k.lower()) == values[rid]
            for rid in rids
        ):
            return _extract_key(k), {"from": "headers", "type": "jsonpath", "expr": jp((k.lower(),))}
    # text (an HTML page): a regex with the literal text before the value
    text0 = earlier[rids[0]].body or ""
    for m in re.finditer(re.escape(values[rids[0]]), text0):  # the first place whose lead-in is unique
        i = m.start()
        if i <= 0:
            continue
        window = text0[max(0, i - 40) : i]
        tag = window.rfind("<")
        left = window[tag + 1 :] if tag >= 0 else window[-24:]
        expr = re.escape(left).replace("\\ ", " ") + "([^\"'<>&\\s]+)"
        if all(re.findall(expr, earlier[rid].body or "") == [values[rid]] for rid in rids):
            return "value", {"from": "body", "type": "regex", "expr": expr}
    return None


def _jp_one(doc: Any, expr: str) -> str | None:
    if doc is None:
        return None
    from jsonpath_ng.ext import parse

    found = [m.value for m in parse(expr).find(doc)]
    return str(found[0]) if len(found) == 1 else None


def _extract_key(name: str) -> str:
    key = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    return key if key and key[0].isalpha() else "value"


def _step_id(ctx: _Ctx, sig: tuple[str, str, str], by_run: dict[str, Exchange], fallback: str) -> str:
    if sig in ctx.step_ids:
        return ctx.step_ids[sig]
    segs = [s for s in sig[2].split("/") if s and s != "*" and not s.isdigit()]
    base = _extract_key(segs[-1]) if segs else ("home" if sig[2].strip("/") == "" else fallback)
    base = base[:30] or fallback
    sid, n = base, 2
    while sid in ctx.step_ids.values():
        sid, n = f"{base}_{n}", n + 1
    ctx.step_ids[sig] = sid
    return sid


def _add_prior(
    ctx: _Ctx, sid: str, sig: tuple[str, str, str], by_run: dict[str, Exchange], extract: dict[str, Any]
) -> None:
    for step in ctx.prior:
        if step["id"] == sid:
            step["extract"].update(extract)
            return
    if len(ctx.prior) >= 3:
        raise InferError("the action needs more than three earlier requests; record a simpler flow")
    first = next(iter(by_run.values()))
    base = _origin(first.url)
    request = _template_request(ctx, sig, by_run, base)
    statuses = sorted({ex.status or 0 for ex in by_run.values()})
    ctx.prior.append(
        {
            "id": sid,
            "request": request,
            "expect": {"status": statuses, "content_type": content_kind(first), "paths": []},
            "extract": extract,
        }
    )


def _params(ctx: _Ctx, page_param: str | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    selects = {n for s in ctx.flow.steps if isinstance(s, Select) for n in placeholders(s)}
    for name, inp in ctx.flow.inputs.items():
        if not inp.marker and name in selects:
            out[name] = {
                "type": "enum",
                "required": True,
                "values": list(dict.fromkeys(inp.examples)),
                "example": inp.examples[0],
            }
        elif all(e.lstrip("-").isdigit() for e in inp.examples):
            out[name] = {"type": "integer", "required": True, "example": int(inp.examples[0])}
        else:
            out[name] = {"type": "string", "required": True, "example": inp.examples[0]}
        if inp.description:
            out[name]["description"] = inp.description
    if page_param is not None:
        out[page_param] = {
            "type": "integer",
            "required": False,
            "default": 1,
            "description": "1-based page number",
        }
    return out
