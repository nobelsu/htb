"""M3 goal: pick the single request that performs the task.

* **Read:** entries whose fields contain an input value (in any encoding) and whose JSON response has a
  list-like structure; the earliest one after the value was typed and after the last click/Enter before
  the goal mark. If no JSON candidate exists, HTML/text responses that mention the value are used.
* **Write:** the last same-site non-GET/HEAD entry started after the click that preceded the goal mark
  and before the mark (the operator marks right after the click); fallback: the first one after it.
* **Ties** (several distinct endpoints equally plausible): an optional LLM picks; without one, the
  earliest wins and the tie is reported.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import quote, quote_plus

from spider.harmap.helpers import restli
from spider.harmap.helpers.runs import Bundle
from spider.harmap.pipeline.load import Entry
from spider.harmap.pipeline.normalize import Field

GRACE = timedelta(milliseconds=500)  # a read can start a moment after the mark keypress is read


def value_forms(value: str) -> dict[str, str]:
    """The encodings a typed value can take on the wire, keyed by name."""
    return {
        "raw": value,
        "percent": quote(value, safe=""),
        "percent_plus": quote_plus(value),
        "json": json.dumps(value)[1:-1],
        "restli": restli.escape(value),
    }


def field_mentions(f: Field, value: str) -> bool:
    if not value:
        return False
    if isinstance(f.value, str) and value in f.value:
        return True
    return any(form in f.raw for form in value_forms(value).values())


def entry_mentions(bundle: Bundle, entry: Entry, value: str) -> bool:
    return any(field_mentions(f, value) for f in bundle.fields(entry))


def has_list(obj) -> bool:
    if isinstance(obj, list):
        return True
    if isinstance(obj, dict):
        return any(has_list(v) for v in obj.values())
    return False


def endpoint_key(entry: Entry, values: dict[str, str] | None = None) -> tuple[str, str, str]:
    """(method, host, path) with any input value in the path replaced by ``{}``."""
    path = entry.path
    for v in (values or {}).values():
        for form in sorted(set(value_forms(v).values()), key=len, reverse=True):
            if form:
                path = path.replace(form, "{}")
    return entry.method, entry.host, path


OPERATION_PARAMS = ("queryId", "operationName", "operation", "op", "action", "componentId", "sduiid")


def operation_key(entry: Entry) -> tuple:
    """Endpoint plus the query values that name the operation (a GraphQL ``queryId``/``operationName``):
    one ``/graphql`` path serves many different reads."""
    pairs = [kv.split("=", 1) for kv in entry.raw_query.split("&") if "=" in kv]
    ops = tuple(sorted((k, v.split(".")[0]) for k, v in pairs if k in OPERATION_PARAMS))
    return entry.method, entry.host, entry.path, ops


@dataclass
class Goal:
    entry: Entry
    reason: str
    candidates: list[Entry] = field(default_factory=list)
    tie: bool = False
    typed_at: datetime | None = None
    mark: datetime | None = None
    shown: list[Entry] = field(default_factory=list)  # the candidates as the live run recorded them


class GoalNotFound(LookupError):
    pass


def _typed_at(bundle: Bundle, values: dict[str, str]) -> datetime | None:
    times = [
        t
        for t, a in bundle.ui_events("input")
        if any(v and v in str(a.get("value", "")) for v in values.values())
    ]
    return min(times) if times else None


def _anchor(bundle: Bundle, after: datetime | None, mark: datetime | None) -> datetime | None:
    """Time of the last click/Enter between ``after`` and ``mark``."""
    if mark is None:
        return None
    clicks = [t for t, _ in bundle.ui_events("click", "key") if t <= mark and (after is None or t >= after)]
    return clicks[-1] if clicks else None


def _pick_mark(bundle: Bundle, mark_index: int) -> datetime | None:
    marks = bundle.goal_marks
    if not marks:
        return None
    return marks[mark_index] if -len(marks) <= mark_index < len(marks) else marks[-1]


def _distinct_endpoints(entries: list[Entry], values: dict[str, str]) -> int:
    return len({endpoint_key(e, values) for e in entries})


def find_read_goal(bundle: Bundle, values: dict[str, str] | None = None, mark_index: int = 0) -> Goal:
    values = values if values is not None else bundle.values
    typed = _typed_at(bundle, values)
    mark = _pick_mark(bundle, mark_index)
    mentioned = [
        e
        for e in bundle.entries
        if any(entry_mentions(bundle, e, v) for v in values.values() if v)
        and (typed is None or e.started >= typed)
    ]
    json_lists = [
        e
        for e in mentioned
        if (e.response_kind == "json" and has_list(e.response_json))
        or (e.response_kind == "stream" and e.response_rows)  # streamed data rows are a list
    ]
    candidates = json_lists or [e for e in mentioned if e.response_kind in ("html", "text", "json", "stream")]
    if not candidates:
        raise GoalNotFound(f"{bundle.run}: no request carries the input values {sorted(values)}")

    anchor = _anchor(bundle, typed, mark)
    window = candidates
    if mark is not None:
        lo = anchor or typed
        window = [e for e in candidates if (lo is None or e.started >= lo) and e.started <= mark + GRACE]
        window = window or candidates
    # mark_index > 0 (e.g. page2): the goal is the latest request before that mark, not the earliest
    window = sorted(window, key=lambda e: e.started)
    if mark_index != 0 and mark is not None:
        nearest = [e for e in window if e.started <= mark + GRACE]
        chosen = (nearest or window)[-1]
    else:
        chosen = window[0]
    tie = _distinct_endpoints(window, values) > 1
    kind = "JSON list" if json_lists else "HTML/text"
    reason = f"{kind} response; carries {', '.join(sorted(values))}; earliest after the triggering click"
    return Goal(chosen, reason, window, tie, typed, mark)


WRITE_GRACE = timedelta(milliseconds=500)  # the mark is taken just after the click that sends


def find_write_goal(bundle: Bundle, mark_index: int = 0) -> Goal:
    writes = [e for e in bundle.entries if e.method not in ("GET", "HEAD")]
    if not writes:
        raise GoalNotFound(f"{bundle.run}: no non-GET/HEAD request in the capture")
    mark = _pick_mark(bundle, mark_index)
    # Explored writes: the request the blocker stopped that carries the made-up text is the send itself
    # (its timing says little: it starts inside the click, and bot sensors post around it)
    values = [v for v in bundle.values.values() if v]
    carrying = [
        e for e in writes if e.blocked and values and any(entry_mentions(bundle, e, v) for v in values)
    ]
    if carrying:  # drafts saved while typing come before the send, retries of it after
        at_mark = [e for e in carrying if mark is None or e.started <= mark + WRITE_GRACE]
        chosen = at_mark[-1] if at_mark else carrying[0]
        reason = "the blocked request that carries the typed input (the send the explorer stopped)"
        return Goal(chosen, reason, carrying, False, None, mark)
    if mark is None:
        return Goal(writes[0], "first non-GET/HEAD request (no goal mark)", writes, len(writes) > 1)
    anchor = _anchor(bundle, None, mark)
    # no grace for writes: the mark comes after the click, so the write has already started
    before = [e for e in writes if e.started <= mark and (anchor is None or e.started >= anchor)]
    if before:
        chosen = before[-1]
        reason = "last non-GET/HEAD request between the triggering click and the goal mark"
        pool = before
    else:
        after = [e for e in writes if e.started > mark]
        if not after:
            chosen, pool = writes[-1], writes
            reason = "last non-GET/HEAD request (none near the goal mark)"
        else:
            chosen, pool = after[0], after
            reason = "first non-GET/HEAD request after the goal mark"
    return Goal(chosen, reason, pool, _distinct_endpoints(pool, {}) > 1, None, mark)


MAX_SCREEN_CANDIDATES = 14  # with small data answers holding the page's text, for the LLM to weigh
NAV_SLACK = timedelta(seconds=2)
SMALL = 50_000  # bytes: a "who am I" answer is small; big ones are pages, feeds and app settings
MIN_AT_ACTION = 5_000  # bytes: a data answer fetched at the action smaller than this is a setting or flag
MIN_SCREEN_SHARE = 1 / 10  # less than this of what the page showed: the answer was not recorded
SCREEN_GRACE = timedelta(seconds=3)  # a page that performs the task fetches its data right after loading


def similar_entry(bundle: Bundle, like: Entry) -> Entry | None:
    """The same endpoint in another run when one path segment differs (a picked item's id in the path:
    /api/conversations/<id>/messages): same method, host and segment count, one segment at most apart."""
    segs = like.path.strip("/").split("/")
    same = [
        e
        for e in bundle.entries
        if (e.method, e.host) == (like.method, like.host)
        and len(e.path.strip("/").split("/")) == len(segs)
        and sum(a != b for a, b in zip(e.path.strip("/").split("/"), segs, strict=True)) <= 1
    ]
    return same[0] if same else None


def find_screen_goal(
    bundle: Bundle, screen: list[str], mark_index: int = 0, values: dict[str, str] | None = None
) -> Goal:
    """Reads that take no input (nothing typed to look for): the answer that holds most of what the page
    showed. Each text counts less the more answers carry it (menus are on every page), and a data answer
    (JSON, streamed rows) wins over the page's HTML unless it holds less than half as much."""
    mark = _pick_mark(bundle, mark_index)
    anchor = _anchor(bundle, None, mark)  # the click/Enter that performed the task, if it was one
    texts = [t for t in dict.fromkeys(screen) if len(t) >= 3]
    forms = {t: (t, json.dumps(t)[1:-1]) for t in texts}
    seen = [
        e
        for e in bundle.entries
        if (not values or any(entry_mentions(bundle, e, v) for v in values.values() if v))
        and e.response_kind in ("json", "stream", "html", "text")
        and e.response_body
        and 200 <= e.status < 300
        and (mark is None or e.started <= mark + SCREEN_GRACE)
    ]
    pool = [e for e in seen if anchor is None or e.started >= anchor]
    holds = {id(e): {t for t in texts if any(f in e.response_body for f in forms[t])} for e in seen}
    spread = {t: max(1, sum(t in holds[id(e)] for e in pool)) for t in texts}
    score = {id(e): sum(1 / spread[t] for t in holds[id(e)]) for e in seen}
    scored = [e for e in pool if score[id(e)] > 0]
    if not scored:
        raise GoalNotFound(f"{bundle.run}: no answer holds the text the page showed")
    best = max(score[id(e)] for e in scored)
    data = [e for e in scored if e.response_kind in ("json", "stream")]
    first = scored
    if data and max(score[id(e)] for e in data) * 2 >= best:
        first = data  # a data answer goes first; the pages stay candidates (app settings look like data)
    top = max(score[id(e)] for e in first)
    window = sorted((e for e in first if score[id(e)] == top), key=lambda e: e.started)
    held = len(holds[id(window[0])])
    reason = f"its answer holds {held} of the {len(texts)} texts the page showed"
    ranked = sorted(scored, key=lambda e: -score[id(e)])
    candidates = [window[0], *[e for e in ranked if e is not window[0]][:5]]
    at_action = []
    if mark is not None:  # data fetched right at the task's action, even if it words things differently
        # (first and last name apart, while the page shows "Ada Lovelace"); small answers are settings
        at_action = [
            e
            for e in seen
            if e.response_kind in ("json", "stream")
            and len(e.response_body or "") >= MIN_AT_ACTION
            and mark - NAV_SLACK <= e.started <= mark + SCREEN_GRACE
        ]
        at_action = sorted(at_action, key=lambda e: -len(e.response_body or ""))[:3]  # results are big
    if held < len(texts) * MIN_SCREEN_SHARE:
        raise GoalNotFound(
            f"{bundle.run}: no recorded answer holds the page's result (best: {held} of {len(texts)} "
            "texts); the recording missed it, explore the task again"
        )
    # no input at all (who am I): the data may have loaded before the task, on every page
    if not values and not bundle.meta.get("inputs_via"):
        dense = [
            e
            for e in seen
            if e.response_kind in ("json", "stream") and score[id(e)] > 0 and len(e.response_body) < SMALL
        ]
        dense.sort(key=lambda e: (-score[id(e)], len(e.response_body)))  # rare text first, not menu labels
        taken = {operation_key(c) for c in candidates}
        fresh = []
        for e in dense:  # each loads on every page: once per endpoint, and not one already listed
            if operation_key(e) not in taken:
                taken.add(operation_key(e))
                fresh.append(e)
        candidates += fresh[:5]
    candidates += at_action
    unique, ops = [], set()
    for e in candidates:  # each operation once
        if operation_key(e) not in ops and len(unique) < MAX_SCREEN_CANDIDATES:
            ops.add(operation_key(e))
            unique.append(e)
    candidates = unique
    return Goal(window[0], reason, candidates, False, None, mark)


def find_goal(bundle: Bundle, kind: str | None = None, mark_index: int = 0) -> Goal:
    kind = kind or bundle.kind_hint
    if kind == "write":
        return find_write_goal(bundle, mark_index)
    return find_read_goal(bundle, mark_index=mark_index)


def counterpart(
    bundle: Bundle, like: Entry, like_values: dict[str, str], before: datetime | None = None
) -> Entry | None:
    """The entry in ``bundle`` for the same endpoint as ``like`` (nearest before ``before``)."""
    key = endpoint_key(like, like_values)
    same = [e for e in bundle.entries if endpoint_key(e, bundle.values) == key]
    same = [e for e in same if operation_key(e) == operation_key(like)] or same  # the same GraphQL query
    if before is not None:
        earlier = [e for e in same if e.started < before]
        if earlier:
            return earlier[-1]
    return same[0] if same else None
