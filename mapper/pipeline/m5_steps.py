"""M5 prune + order: provenance → ``steps[]`` (request templates + dependency extracts).

Only requests reachable backwards from the goal are kept (M4 only ever adds those), ordered by
capture time. Step ids are short slugs from the endpoint (``session``, ``search``). Each dependency
becomes an ``extract`` on its source step and a ``{{steps.ID.KEY}}`` placeholder where it is used.
Request shape follows action_map.example.json: ``{method, url, query{}, headers{}, body}``.
``expect`` and output extracts are added by M6.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

from mapper import jsonpaths, placeholders, restli
from mapper.pipeline.m2_normalize import Field
from mapper.pipeline.m4_provenance import Label, Provenance, StepDraft, filter_for
from mapper.pipeline.runs import Bundle

GENERIC_SEGMENTS = frozenset(
    {
        "api",
        "rest",
        "graphql",
        "gql",
        "voyager",
        "json",
        "ajax",
        "data",
        "v1",
        "v2",
        "v3",
        "v4",
        "public",
        "web",
    }
)
_IDENT_RE = re.compile(r"[^a-z0-9_]+")


def snake(text: str) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    return _IDENT_RE.sub("_", text.lower()).strip("_")


def slug_for(entry, bundle: Bundle | None = None) -> str:
    """Short step id from the endpoint: ``/api/search`` → ``search``."""
    body = entry.request_json
    if isinstance(body, dict) and isinstance(body.get("operationName"), str):
        return snake(body["operationName"])[:24] or "step"
    query_id = next(
        (unquote(v) for k, _, v in (p.partition("=") for p in entry.raw_query.split("&")) if k == "queryId"),
        None,
    )
    if query_id:
        name = re.sub(r"^voyager", "", query_id.split(".", 1)[0])
        return snake(name)[:24] or "step"
    segments = [unquote(s) for s in entry.path.split("/") if s]
    for seg in reversed(segments):
        if seg.lower() in GENERIC_SEGMENTS or not re.fullmatch(r"[A-Za-z][A-Za-z_-]{1,30}", seg):
            continue
        return snake(seg)[:24]
    return "home" if not segments else "step"


def _unique(base: str, taken: set[str]) -> str:
    name, n = base, 2
    while name in taken:
        name = f"{base}_{n}"
        n += 1
    taken.add(name)
    return name


def extract_key(hint: str) -> str:
    key = snake(hint) or "value"
    return f"v_{key}" if key[0].isdigit() else key


@dataclass
class StepPlan:
    id: str
    draft: StepDraft
    request: dict
    extract: dict[str, dict] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_card(self) -> dict:
        return {"id": self.id, "request": self.request, "extract": self.extract}


class _Renderer:
    def __init__(self, ids: dict[int, str], keys: dict[tuple[int, str], str]) -> None:
        self.ids, self.keys = ids, keys

    def leaf(self, lab: Label) -> Any:
        context = lab.field.context
        filt = filter_for(context)
        if lab.provenance == "param":
            return lab.template
        if lab.provenance == "dependency":
            src = lab.source
            key = self.keys[(id(src.entry), src.extract["expr"])]
            return placeholders.step(self.ids[id(src.entry)], key, filt)
        if lab.provenance == "session_cookie":
            return placeholders.cookie(lab.cookie, "strip_quotes" if lab.strip_quotes else None, filt)
        value = lab.template
        if context == "restli" and not (isinstance(value, str) and placeholders.is_only_placeholder(value)):
            return restli.dumps(value)
        return value


def _base(name: str) -> tuple[str, int]:
    m = re.fullmatch(r"(.*)\[(\d+)\]", name)
    return (m.group(1), int(m.group(2))) if m else (name, 0)


def _structured_template(parent: Field, labels: dict[str, Label], render: _Renderer) -> str:
    """Rebuild a Rest.li or JSON value with each sub-field rendered as its template."""

    def leaf(path: str, value: Any) -> Any:
        lab = labels.get(f"{parent.address}.{path}")
        return render.leaf(lab) if lab is not None else value

    if parent.structure == "restli":
        tree = restli.try_parse(parent.raw)
        return restli.render(
            tree, lambda p, v: str(leaf(p, v)) if f"{parent.address}.{p}" in labels else restli.dumps(v)
        )
    tree = json.loads(parent.value)
    return json.dumps(jsonpaths.map_leaves(tree, leaf), separators=(",", ":"))


def build_request(
    draft: StepDraft, bundle: Bundle, base_url: str, render: _Renderer
) -> tuple[dict, list[str]]:
    entry = draft.entry
    warnings: list[str] = []
    labels = {lab.address: lab for lab in draft.labels}
    fields = bundle.fields(entry)

    origin = placeholders.BASE_URL if entry.base_url == base_url else entry.base_url
    segments = [str(render.leaf(labels[f.address])) for f in fields if f.location == "url"]
    url = origin + "/" + "/".join(segments)
    if entry.path.endswith("/") and segments:
        url += "/"

    query: dict[str, Any] = {}
    for f in fields:
        if f.location != "query" or f.parent is not None:
            continue
        name, idx = _base(f.address.removeprefix("query."))
        if idx > 0:
            warnings.append(f"repeated query param {name!r}: only the first value is kept")
            continue
        if f.structure:
            query[name] = _structured_template(f, labels, render)
        else:
            query[name] = render.leaf(labels[f.address])

    headers: dict[str, Any] = {}
    for lab in draft.labels:
        if lab.field.location != "header" or lab.provenance == "omitted":
            continue
        name, idx = _base(lab.address.removeprefix("header."))
        if idx == 0:
            headers[name] = render.leaf(lab)

    body: Any = None
    body_fields = [f for f in fields if f.location == "body"]
    if body_fields:
        context = body_fields[0].context
        if context == "json":
            body = jsonpaths.map_leaves(
                entry.request_json,
                lambda p, v: render.leaf(labels[f"body.{p}"]) if f"body.{p}" in labels else v,
            )
        elif context == "form":
            body = {}
            for f in body_fields:
                if f.parent is not None:
                    continue
                name, idx = _base(f.address.removeprefix("body."))
                if idx == 0:
                    body[name] = (
                        _structured_template(f, labels, render)
                        if f.structure
                        else render.leaf(labels[f.address])
                    )
            warnings.append("form-encoded body: card representation not confirmed yet (see step 8)")
        else:
            body = render.leaf(labels["body"])

    return {"method": entry.method, "url": url, "query": query, "headers": headers, "body": body}, warnings


def plan_steps(prov: Provenance, bundle: Bundle) -> list[StepPlan]:
    """M5: ordered steps with request templates and the extracts later steps rely on."""
    drafts = sorted(prov.steps, key=lambda d: (d.entry.started, d.entry.index))
    base_url = prov.goal.entry.base_url

    taken: set[str] = set()
    ids = {id(d.entry): _unique(slug_for(d.entry, bundle), taken) for d in drafts}

    keys: dict[tuple[int, str], str] = {}
    extracts: dict[int, dict[str, dict]] = {id(d.entry): {} for d in drafts}
    taken_keys: dict[int, set[str]] = {id(d.entry): set() for d in drafts}
    for d in drafts:
        for lab in d.labels:
            src = lab.source
            if src is None:
                continue
            k = (id(src.entry), src.extract["expr"])
            if k not in keys:
                keys[k] = _unique(extract_key(src.hint or lab.address), taken_keys[id(src.entry)])
                extracts[id(src.entry)][keys[k]] = dict(src.extract)

    render = _Renderer(ids, keys)
    plans = []
    for d in drafts:
        request, warnings = build_request(d, bundle, base_url, render)
        plans.append(StepPlan(ids[id(d.entry)], d, request, extracts[id(d.entry)], warnings))
    return plans
