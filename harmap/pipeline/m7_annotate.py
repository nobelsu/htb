"""M7 annotate: draft action → full action (kind, safety flags, params, description, tier, examples).

Heuristics (docs/harmap-design.md): GET = read. A non-GET to anything that looks like order / pay /
checkout / purchase = ``spends`` + ``requires_confirmation``. Writes default to
``requires_confirmation: true`` and ``idempotent: false`` unless clearly idempotent (follow, set, PUT).
Descriptions come from the LLM when available, otherwise from templates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from harmap.llm import LLM
from harmap.pipeline.m1_load import Entry

SPEND_RE = re.compile(r"order|pay(ment)?|checkout|purchase|buy|billing", re.I)
IDEMPOTENT_RE = re.compile(r"follow|subscribe|(^|/)set|settings|preferences|like", re.I)


@dataclass
class Annotation:
    fields: dict  # action fields except name/steps/output
    warnings: list[str] = field(default_factory=list)
    described_by: str = "template"  # "llm" | "template"


def _humanize(task: str) -> str:
    return task.replace("_", " ").strip().capitalize()


def template_description(kind: str, task_description: str, task: str, output_fields: list[str]) -> str:
    what = (task_description or _humanize(task)).rstrip(".")
    if kind == "read":
        returns = f" Returns {', '.join(output_fields[:5])} for each result." if output_fields else ""
        return f"{what}.{returns} Read-only; safe to retry."
    return f"{what}. Changes state on the user's account: ask the user before calling."


def template_param_description(name: str, example: str | None) -> str:
    text = name.replace("_", " ")
    return f"{text[:1].upper()}{text[1:]}, as typed into the site" + (
        f', e.g. "{example}".' if example else "."
    )


def annotate(
    *,
    task: str,
    task_description: str,
    kind_hint: str,
    goal: Entry,
    params: list[str],
    real_inputs: dict[str, str],
    output_fields: list[str],
    has_unknowns: bool,
    llm: LLM,
) -> Annotation:
    warnings: list[str] = []
    kind = "read" if goal.method in ("GET", "HEAD") else "write"
    if kind != kind_hint:
        warnings.append(
            f"operator said {kind_hint!r} but the goal request is a {goal.method}: using {kind!r}"
        )

    spends = kind == "write" and bool(SPEND_RE.search(goal.path))
    if kind == "read":
        idempotent, confirm = True, False
    else:
        idempotent = goal.method == "PUT" or bool(IDEMPOTENT_RE.search(goal.path))
        confirm = True

    description = template_description(kind, task_description, task, output_fields)
    param_desc = {p: template_param_description(p, real_inputs.get(p)) for p in params}
    described_by = "template"
    summary = {
        "task": task,
        "operator_description": task_description,
        "kind": kind,
        "endpoint": f"{goal.method} {goal.path}",
        "params": {p: {"example": real_inputs.get(p)} for p in params},
        "returns": output_fields,
    }
    llm_text = llm.describe(summary)
    if llm_text:
        description = llm_text["description"]
        param_desc.update({k: v for k, v in llm_text["params"].items() if k in param_desc})
        described_by = "llm"

    action: dict = {
        "description": description,
        "kind": kind,
        "spends": spends,
        "idempotent": idempotent,
        "requires_confirmation": confirm or spends,
        "params": {
            p: {
                "type": "string",
                "required": True,
                "description": param_desc[p],
                **({"example": real_inputs[p]} if p in real_inputs else {}),
            }
            for p in params
        },
    }
    if has_unknowns:
        action["tier"] = "browser"
        warnings.append("goal request has unknown (likely JS-computed) values: tier set to 'browser'")
    if kind == "read":
        missing = [p for p in params if p not in real_inputs]
        if missing:
            warnings.append(f"no real-run input for {missing}: the example cannot cover every required param")
        action["examples"] = [
            {"params": {p: real_inputs[p] for p in params if p in real_inputs}, "min_items": 1}
        ]
    else:
        action["examples"] = []
    return Annotation(action, warnings, described_by)
