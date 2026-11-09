"""``mapping_report.md``: per action, the goal request, every field with its provenance, unknowns,
warnings, validation and the LLM calls made. Cookie values and csrf/auth/token values are always
``[redacted]``; only templates (which hold placeholders, not values) are shown in full.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING, Any

from harmap import __version__
from harmap.bundle import iso_now
from harmap.checks import SENSITIVE_NAME
from harmap.pipeline.m4_provenance import Label, as_text

if TYPE_CHECKING:
    from harmap.llm import LLMCall
    from harmap.pipeline import DomainResult, TaskResult

REDACTED = "[redacted]"


def _cell(value: Any, limit: int = 70) -> str:
    text = value if isinstance(value, str) else as_text(value)
    text = text.replace("\n", " ").replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _code(value: Any, limit: int = 70) -> str:
    text = _cell(value, limit)
    return f"`{text}`" if text else "``"


def redacted_value(lab: Label, secrets: set[str]) -> str:
    sensitive = (
        lab.field.location == "cookie"
        or lab.provenance in ("session_cookie", "dependency")
        or SENSITIVE_NAME.search(lab.address)
        or any(s in as_text(lab.field.value) for s in secrets)
    )
    return REDACTED if sensitive else _code(lab.field.value, 50)


def evidence(lab: Label, step_ids: dict[int, str]) -> str:
    notes = f" ({'; '.join(lab.notes)})" if lab.notes else ""
    if lab.provenance == "param" and any(n.startswith("pagination:") for n in lab.notes):
        return f"{'; '.join(lab.notes)} → filter `{lab.filter}`" if lab.filter else "; ".join(lab.notes)
    if lab.provenance == "param":
        filt = f"`{lab.filter}`" if lab.filter else "none"
        return (
            f"marker for `{', '.join(lab.params)}` found {lab.encoding} in m1 and m2 → filter {filt}{notes}"
        )
    if lab.provenance == "dependency" and lab.source is not None:
        src = lab.source
        where = step_ids.get(id(src.entry), src.entry.label)
        check = "confirmed on m2" if src.confirmed else "**not confirmed on m2**"
        expr = _cell(src.extract["expr"], 60)
        return f"{src.extract['type']} `{expr}` on {src.kind} of step `{where}`, {check}"
    if lab.provenance == "session_cookie":
        if lab.field.location == "cookie":
            return "session cookie: sent by the executor's cookie jar, not templated"
        strip = " after stripping quotes" if lab.strip_quotes else ""
        return f"equals session cookie `{lab.cookie}`{strip}"
    if lab.provenance == "constant":
        return "same in m1 and m2" + notes
    return "; ".join(lab.notes) or lab.provenance


def template_of(lab: Label, request: dict) -> str:
    """The template this field became (looked up in the rendered request)."""
    loc, _, rest = lab.address.partition(".")
    if lab.provenance == "omitted":
        return "(omitted)"
    if loc == "cookie":
        return "(cookie jar)"
    if loc == "header":
        return _code(request["headers"].get(rest.split("[")[0], "(hoisted to defaults)"))
    if loc == "query":
        name = rest.split(".", 1)[0].split("[")[0]
        return _code(request["query"].get(name, ""))
    if loc == "url":
        return _code(request["url"])
    if loc == "body":
        return _code(request["body"])
    return ""


def _section_task(tr: TaskResult, secrets: set[str]) -> list[str]:
    out: list[str] = []
    a = tr.mapped.action
    g = tr.goal.entry
    out.append(f"## Action `{a['name']}` ({a['kind']})")
    out.append("")
    out.append(f"> {a['description']}")
    out.append("")
    out.append(f"**Goal request:** `{g.method} {g.path}`. {tr.goal.reason}.")
    if tr.goal.tie:
        alts = ", ".join(f"`{e.method} {e.path}`" for e in tr.goal.candidates)
        out.append(f"**Tie:** several endpoints were plausible ({alts}); picked by {tr.goal_picked_by}.")
    runs = []
    for run, b in tr.runs.items():
        vals = ", ".join(f"{k}=`{v}`" for k, v in b.values.items())
        runs.append(f"{run} ({vals})" if vals else run)
    out.append(f"**Runs used:** {'; '.join(runs)}.")
    m1 = tr.runs["m1"]
    dropped = Counter(r.split(" (")[0] for _, r in m1.dropped)
    drop_txt = ", ".join(f"{r} ×{n}" for r, n in dropped.most_common()) or "none"
    out.append(
        f"**M1 filter (m1):** kept {len(m1.entries)} of {len(m1.entries) + len(m1.dropped)} entries; "
        f"dropped: {drop_txt}."
    )
    out.append("")

    step_ids = {id(p.draft.entry): p.id for p in tr.plans}
    for i, plan in enumerate(tr.plans, 1):
        e = plan.draft.entry
        goal_tag = " (goal)" if plan.draft.is_goal else ""
        out.append(f"### Step {i} `{plan.id}`{goal_tag}: `{e.method} {e.path}`")
        out.append("")
        out.append("| Field | Provenance | m1 value | Template | Evidence |")
        out.append("|---|---|---|---|---|")
        omitted = [
            lab.address.removeprefix("header.") for lab in plan.draft.labels if lab.provenance == "omitted"
        ]
        for lab in plan.draft.labels:
            if lab.provenance == "omitted":
                continue
            out.append(
                f"| `{_cell(lab.address, 50)}` | {lab.provenance} | {redacted_value(lab, secrets)} "
                f"| {template_of(lab, plan.request)} | {evidence(lab, step_ids)} |"
            )
        out.append("")
        if omitted:
            out.append(f"Omitted (browser-managed, set by the executor's tier): {', '.join(omitted)}.")
        if plan.extract:
            ex = ", ".join(f"`{k}` = {v['type']} `{_cell(v['expr'], 60)}`" for k, v in plan.extract.items())
            out.append(f"Extracts: {ex}.")
        if plan.expect:
            ex = plan.expect
            out.append(f"Expect: status {ex['status']}, {ex['content_type']}, paths {ex['paths']}.")
        for w in plan.warnings:
            out.append(f"- ⚠ {w}")
        out.append("")

    o = a["output"]
    out.append("### Output")
    out.append("")
    if o["from"] is None:
        out.append("No output (`output.from: null`).")
    else:
        source = {"llm": "picked by the LLM", "fallback": "first item's scalar leaves (no LLM)"}.get(
            tr.output.source, tr.output.source
        )
        out.append(f"From `{o['from']}` (`{tr.output.items_path}[*]`), limit {o['limit']}, fields {source}:")
        out.append("")
        for name, path in o["fields"].items():
            out.append(f"- `{name}` ← `{path}`")
    out.append("")

    warnings = tr.warnings
    unknowns = [lab for lab in tr.prov.goal.labels if lab.provenance == "unknown"]
    out.append("### Unknowns and warnings")
    out.append("")
    if not unknowns and not warnings:
        out.append("None.")
    for lab in unknowns:
        out.append(f"- **unknown** `{lab.address}`: {'; '.join(lab.notes)}")
    for w in warnings:
        out.append(f"- ⚠ {w}")
    out.append("")
    return out


def _section_recon(r) -> list[str]:
    out = ["## Site recon (M0, informational)", ""]
    if not r.platforms and not r.json_ld_types and not r.embedded:
        out += ["No known platform markers or embedded data.", ""]
        return out
    for platform, evidence in r.platforms.items():
        out.append(f"- **{platform}**: {'; '.join(evidence)}")
    if r.json_ld_types:
        out.append(f"- JSON-LD types: {', '.join(r.json_ld_types)}")
    for emb in r.embedded:
        out.append(
            f'- Embedded JSON on `{emb.page}`: `<script id="{emb.script_id}">` with keys {emb.top_keys}.'
        )
        out.append(f"  Suggested extract: `{json.dumps(emb.extract)}`")
    out.append("")
    return out


def _section_llm(calls: list[LLMCall], enabled: bool) -> list[str]:
    out = ["## LLM calls", ""]
    if not enabled:
        out.append("None: LLM steps are off (no `ANTHROPIC_API_KEY`), deterministic fallbacks used.")
    elif not calls:
        out.append("None needed.")
    else:
        out.append("| Purpose | Result | Detail |")
        out.append("|---|---|---|")
        for c in calls:
            out.append(f"| {c.purpose} | {'ok' if c.ok else 'failed → fallback'} | {_cell(c.detail)} |")
    out.append("")
    return out


def render(result: DomainResult) -> str:
    card = result.card
    v = result.validation
    out = [f"# Mapping report: {card['domain']}", ""]
    out.append(f"Mapped {iso_now()} by mapper {__version__} from `{result.captures_dir}`. Card: `card.json`.")
    out.append("")
    for note in result.notes:
        out.append(f"> **⚠ {note}**")
        out.append("")

    out += [
        "## Summary",
        "",
        "| Action | Kind | Goal request | Steps | Tier | Output fields |",
        "|---|---|---|---|---|---|",
    ]
    defaults = card["defaults"]
    for tr in result.tasks:
        a = tr.mapped.action
        g = tr.goal.entry
        tier = a.get("tier", defaults["tier"])
        steps = " → ".join(p.id for p in tr.plans)
        out.append(
            f"| `{a['name']}` | {a['kind']} | `{g.method} {g.path}` | {steps} | {tier} "
            f"| {len(a['output']['fields'])} |"
        )
    out.append("")

    out += ["## Validation", "", f"Checks run: {', '.join(v.ran)}.", ""]
    out.append("**VALID**" if v.ok else "**INVALID**")
    out.append("")
    for e in v.errors:
        out.append(f"- ❌ {e}")
    for w in v.warnings:
        out.append(f"- ⚠ {w}")
    if v.live:
        out.append("")
        out.append("Live runs through the executor:")
        for r in v.live:
            out.append(
                f"- {'✅' if r.ok else '❌'} `{r.action}({r.params})` → {r.status}, "
                f"{r.items} item(s) {r.message}"
            )
    elif result.live_requested:
        out.append("- live validation requested but no read examples ran")
    else:
        out.append("- live validation not run (pass `--live` to run examples through the executor)")
    out.append("")

    if result.recon is not None:
        out += _section_recon(result.recon)
    for tr in result.tasks:
        out += _section_task(tr, result.secrets)
    out += _section_llm(result.llm.calls, bool(result.llm.enabled))
    return "\n".join(out).rstrip() + "\n"
