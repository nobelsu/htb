"""Label: the only step that uses an LLM. It writes the action's description, each param's description, and a
name for each output field it keeps. Nothing else.

The model answers through one strict tool (``label_action``). Its answer is then applied in code to the slots
it may fill: a description, descriptions of the params that exist, and names for field paths inference
proposed. It can't add params, rename them, change URLs, requests or safety flags, or invent field paths:
anything else in the answer is ignored.

Without credentials (no ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN``), or when the call fails or is
refused, labels are made deterministically: field names from the last path segment, the matched field plus
up to 8 scalar fields kept, a templated description. The model is ``ANTHROPIC_MODEL`` (default
``claude-opus-5-5``).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from flowmap.flows import Flow
from flowmap.infer import Inferred

log = logging.getLogger("flowmap.label")

DEFAULT_MODEL = "claude-opus-5-5"
# Models that take server-side refusal fallbacks (fallbacks: "default").
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_FIELDS = 12
MAX_DESCRIPTION = 400
DEFAULT_KEEP = 8

LABEL_TOOL: dict[str, Any] = {
    "name": "label_action",
    "description": "Name and describe a website action, its parameters and its output fields for agents.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["description", "params", "fields"],
        "properties": {
            "description": {
                "type": "string",
                "description": "One or two sentences for an AI agent: what the action does and returns.",
            },
            "params": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "description"],
                    "properties": {"name": {"type": "string"}, "description": {"type": "string"}},
                },
            },
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "name", "keep"],
                    "properties": {
                        "path": {"type": "string", "description": "A candidate path exactly as given."},
                        "name": {"type": "string", "description": "snake_case output field name."},
                        "keep": {"type": "boolean"},
                    },
                },
            },
        },
    },
}

PROMPT = """You are naming one action of a website's internal API so an AI agent can call it as a tool.
Call the label_action tool once.

- description: one or two plain sentences an agent reads to decide whether to call this action: what it does
  and what each result holds. No marketing words.
- params: a short description for each parameter listed below (the names are fixed).
- fields: for each candidate output path, a snake_case name and whether to keep it. Keep what a person would
  want from a result and ids that another action could take; drop tracking, analytics and layout values.
  Keep at most {max_fields}.

Action: {action} ({kind})
Request: {method} {url}
Parameters:
{params}
What the page showed for one example:
{visible}
Candidate output paths (relative to each result), with sample values:
{fields}
"""


@dataclass
class Labels:
    description: str
    params: dict[str, str] = field(default_factory=dict)  # param → description
    fields: dict[str, str] = field(default_factory=dict)  # output name → path, kept fields only, in order
    source: str = "rules"  # "llm" or "rules"
    reasons: list[str] = field(default_factory=list)


# ---------- deterministic ----------


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1_\2", text).lower())
    name = "_".join(words)[:40].strip("_")
    return name if name and name[0].isalpha() else ("f_" + name if name else "")


def default_name(path: str) -> str:
    segs = [s for s in re.split(r"[.\[\]'$*]+", path) if s and not s.isdigit()]
    return _slug(segs[-1]) if segs else "value"


def rule_labels(inferred: Inferred, flow: Flow) -> Labels:
    words = flow.action.replace("_", " ")
    candidates = list(inferred.output.get("fields", {}))
    kept: dict[str, str] = {}
    for path in candidates:
        samples = inferred.field_samples.get(path, [])
        if any(isinstance(s, dict | list) for s in samples):
            continue
        name = _unique(default_name(path), kept)
        kept[name] = path
        if len(kept) >= DEFAULT_KEEP:
            break
    if inferred.kind == "write":
        description = f"{words.capitalize()}. Changes data on {flow.domain}."
    elif kept:
        description = f"{words.capitalize()} on {flow.domain}. Returns {', '.join(kept)} for each result."
    else:
        description = f"{words.capitalize()} on {flow.domain}."
    params = {}
    for name, spec in inferred.params.items():
        params[name] = spec.get("description") or name.replace("_", " ")
    return Labels(description=description, params=params, fields=kept)


def _unique(name: str, taken: dict[str, str]) -> str:
    out, n = name, 2
    while out in taken:
        out, n = f"{name}_{n}", n + 1
    return out


# ---------- enforcing a model's answer ----------


def enforce(raw: dict[str, Any], inferred: Inferred, flow: Flow) -> Labels:
    """The model's answer as Labels, keeping only what labelling may change. Anything else is ignored."""
    fallback = rule_labels(inferred, flow)
    out = Labels(description=fallback.description, params=dict(fallback.params), source="llm")
    desc = raw.get("description")
    if isinstance(desc, str) and desc.strip():
        out.description = " ".join(desc.split())[:MAX_DESCRIPTION]
    else:
        out.reasons.append("the model gave no description; a templated one is used")
    for item in raw.get("params") or []:
        if not isinstance(item, dict):
            continue
        name, text = item.get("name"), item.get("description")
        if name in inferred.params and isinstance(text, str) and text.strip():
            out.params[name] = " ".join(text.split())[:200]
        elif name not in inferred.params:
            out.reasons.append(f"ignored a description for unknown param {name!r}")
    candidates = set(inferred.output.get("fields", {}))
    for item in raw.get("fields") or []:
        if not isinstance(item, dict) or not item.get("keep"):
            continue
        path = item.get("path")
        if path not in candidates:
            out.reasons.append(f"ignored unknown field path {path!r}")
            continue
        if path in out.fields.values():
            continue
        given = item.get("name")
        field_name = given if isinstance(given, str) else ""
        if not FIELD_NAME.match(field_name):
            field_name = _slug(field_name) or default_name(str(path))
        out.fields[_unique(field_name, out.fields)] = str(path)
        if len(out.fields) >= MAX_FIELDS:
            break
    if candidates and not out.fields:
        out.fields = fallback.fields
        out.reasons.append("the model kept no fields; the default fields are kept")
    return out


# ---------- the model ----------


def _prompt(inferred: Inferred, flow: Flow, visible: list[str]) -> str:
    main = inferred.steps[-1]["request"]
    params = (
        "\n".join(
            f"- {n} ({p['type']}{', values: ' + ', '.join(map(str, p['values'])) if p.get('values') else ''})"
            f"{', example: ' + json.dumps(p.get('example')) if p.get('example') is not None else ''}"
            for n, p in inferred.params.items()
        )
        or "(none)"
    )
    fields = (
        "\n".join(
            f"- {path}: {json.dumps(inferred.field_samples.get(path, [])[:3], ensure_ascii=False)[:200]}"
            for path in inferred.output.get("fields", {})
        )
        or "(none: the action returns no list)"
    )
    return PROMPT.format(
        max_fields=MAX_FIELDS,
        action=flow.action,
        kind=inferred.kind,
        method=main["method"],
        url=main["url"],
        params=params,
        visible="\n".join(f"- {v}" for v in visible[:10]) or "(nothing)",
        fields=fields,
    )


def _has_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


async def label(
    inferred: Inferred,
    flow: Flow,
    *,
    visible: list[str] | None = None,
    client: Any = None,
    use_llm: bool | None = None,
) -> Labels:
    """Labels for an inferred action. ``client`` is an ``anthropic.AsyncAnthropic`` (made when needed)."""
    if use_llm is None:
        use_llm = client is not None or _has_credentials()
    if not use_llm:
        return rule_labels(inferred, flow)
    model = os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
    try:
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic()
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": 8000,
            "tools": [LABEL_TOOL],
            "tool_choice": {"type": "auto"},
            "output_config": {"effort": "low"},
            "messages": [{"role": "user", "content": _prompt(inferred, flow, visible or [])}],
        }
        if model in FALLBACK_MODELS:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        response = await client.beta.messages.create(**kwargs)
    except Exception as e:  # no SDK, no credentials, network or API error: label by rules
        log.warning("labelling with %s failed: %s", model, type(e).__name__)
        out = rule_labels(inferred, flow)
        out.reasons.append(f"the model call failed ({type(e).__name__}); labelled by rules")
        return out
    if getattr(response, "stop_reason", None) == "refusal":
        out = rule_labels(inferred, flow)
        out.reasons.append("the model declined; labelled by rules")
        return out
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == LABEL_TOOL["name"]:
            raw = block.input if isinstance(block.input, dict) else {}
            return enforce(raw, inferred, flow)
    out = rule_labels(inferred, flow)
    out.reasons.append("the model didn't call label_action; labelled by rules")
    return out
