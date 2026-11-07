"""Assemble: turn an inferred, labelled action into a card, check it, and save it.

``build_action`` makes the card action (safety flags are set here, never by the labeller: a write always has
``requires_confirmation: true``). ``merge`` puts it into the domain's card (replacing an action of the same
name) or starts a new card with a ``tool_prefix``. ``finalise`` validates with ``validate_card``, replays a
fresh example of a read with the executor (it must return at least ``min_items``), and saves with
``registry.save()``. ``map_action`` runs the whole chain for one flow: infer, label, merge, minimise,
finalise.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from flowmap import MAPPER_VERSION
from flowmap.capture import Run
from flowmap.flows import Flow
from flowmap.infer import Inferred, infer, registrable
from flowmap.label import Labels, label
from flowmap.minimise import Minimised, Replayer, minimise
from schema.envelope import ResultCode
from schema.validate import validate_card

IDEMPOTENT_WRITES = {"PUT", "DELETE"}


class AssembleError(Exception):
    def __init__(self, message: str, reasons: list[str] | None = None) -> None:
        super().__init__(message)
        self.reasons = reasons or []


def tool_prefix_for(domain: str) -> str:
    """A short site name for ``tool_prefix``: the first label of the registrable domain."""
    name = registrable(domain).split(".")[0]
    prefix = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:20]
    return prefix or "site"


def build_action(inferred: Inferred, labels: Labels, flow: Flow) -> dict[str, Any]:
    method = inferred.steps[-1]["request"]["method"]
    write = inferred.kind == "write"
    params = copy.deepcopy(inferred.params)
    for name, text in labels.params.items():
        if name in params:
            params[name]["description"] = text
    output: dict[str, Any] = {"from": inferred.output.get("from")}
    if output["from"] is not None:
        output["fields"] = dict(labels.fields)
        for key in ("limit", "next_params"):
            if key in inferred.output:
                output[key] = copy.deepcopy(inferred.output[key])
        if "next_params" in output:  # CARD_SPEC: output.limit equals page_size
            output["limit"] = output["next_params"]["page_size"]
    action: dict[str, Any] = {
        "name": flow.action,
        "description": labels.description,
        "kind": inferred.kind,
        "spends": False,
        "idempotent": (not write) or method in IDEMPOTENT_WRITES,
        "requires_confirmation": write,  # never false on a write
        "params": params,
        "steps": copy.deepcopy(inferred.steps),
        "output": output,
        "examples": copy.deepcopy(inferred.examples) if not write else [],
    }
    if flow.auth_provider:
        action["auth_provider"] = flow.auth_provider
    return action


def _absolute(action: dict[str, Any], base_url: str) -> dict[str, Any]:
    """The action with ``{{base_url}}`` written out (for an action on another origin than the card's)."""
    raw = json.dumps(action).replace("{{base_url}}", base_url)
    return json.loads(raw)  # type: ignore[no-any-return]


def merge(
    action: dict[str, Any],
    inferred: Inferred,
    flow: Flow,
    existing: dict[str, Any] | None,
    *,
    source: str,
    mapped_at: str | None = None,
) -> dict[str, Any]:
    provenance = {
        "mapped_at": mapped_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "source": source,
        "mapper_version": MAPPER_VERSION,
    }
    if existing is None:
        card: dict[str, Any] = {
            "schema_version": "0.4",
            "domain": flow.domain,
            "tool_prefix": tool_prefix_for(flow.domain),
            "base_url": inferred.base_url,
            "blocked_signals": [],
            "changed_signals": list(inferred.changed_signals),
            "provenance": provenance,
            "connect_providers": [],
            "defaults": {"tier": "http"},
            "actions": [],
        }
    else:
        card = copy.deepcopy(existing)
        card["provenance"] = provenance
        signals = card.setdefault("changed_signals", [])
        signals += [s for s in inferred.changed_signals if s not in signals]
        if card["base_url"].rstrip("/") != inferred.base_url.rstrip("/"):
            action = _absolute(action, inferred.base_url)
    if flow.auth_provider and not any(
        p["id"] == flow.auth_provider for p in card.get("connect_providers", [])
    ):
        raise AssembleError(
            f"the flow signs in with {flow.auth_provider!r}, which the card has no connect provider for",
            ["add the provider (login_url and session_check) to the card first"],
        )
    actions = [a for a in card["actions"] if a["name"] != action["name"]]
    card["actions"] = [*actions, action]
    return card


@dataclass
class Finalised:
    version: int | None
    card: dict[str, Any]
    replay_status: str | None = None
    replay_items: int | None = None
    reasons: list[str] = field(default_factory=list)


async def finalise(
    card: dict[str, Any],
    action_name: str,
    *,
    registry: Any = None,
    replay: Replayer | None = None,
    note: str | None = None,
    min_items: int = 1,
) -> Finalised:
    """Validate, replay a fresh example (reads only), then save. Raises AssembleError when a check fails."""
    action = next(a for a in card["actions"] if a["name"] == action_name)
    if action["kind"] == "write" and not action["requires_confirmation"]:
        raise AssembleError("a write must have requires_confirmation: true")
    report = validate_card(card)
    if not report.ok:
        raise AssembleError("the card isn't valid", list(report.errors))
    out = Finalised(version=None, card=card)
    if replay is not None and action["kind"] == "read" and action["examples"]:
        example = action["examples"][-1]  # the last one: minimise used the first
        tier = action.get("tier") or card.get("defaults", {}).get("tier", "http")
        env = await replay(card, action_name, example["params"], tier)
        out.replay_status, out.replay_items = str(env.status), len(env.data or [])
        if env.status != ResultCode.SUCCESS or out.replay_items < min_items:
            detail = env.error.message if env.error else f"{out.replay_items} items"
            raise AssembleError(
                f"replaying example {json.dumps(example['params'])} gave {env.status}",
                [detail, f"needs SUCCESS with at least {min_items} items"],
            )
    if registry is not None:
        out.version = registry.save(card, note=note)
    return out


@dataclass
class Mapped:
    inferred: Inferred
    labels: Labels
    minimised: Minimised | None
    final: Finalised

    def report(self) -> dict[str, Any]:
        return {
            "action": self.inferred.action,
            "kind": self.inferred.kind,
            "confidence": self.inferred.confidence,
            "reasons": self.inferred.reasons
            + self.labels.reasons
            + (self.minimised.reasons if self.minimised else [])
            + self.final.reasons,
            "labels": self.labels.source,
            "tier": self.minimised.tier if self.minimised else None,
            "dropped_headers": self.minimised.dropped if self.minimised else [],
            "replays": self.minimised.replays if self.minimised else 0,
            "replay": {"status": self.final.replay_status, "items": self.final.replay_items},
            "version": self.final.version,
        }


async def map_action(
    flow: Flow,
    runs: list[Run],
    *,
    existing: dict[str, Any] | None = None,
    replay: Replayer | None = None,
    registry: Any = None,
    llm_client: Any = None,
    use_llm: bool | None = None,
    pause_s: float = 1.0,
    note: str | None = None,
) -> Mapped:
    """Infer → label → merge → minimise → validate, replay and save one action."""
    inferred = infer(flow, runs)
    visible = next((r.visible for r in runs if r.kind == "real" and r.visible), [])
    labels = await label(inferred, flow, visible=visible, client=llm_client, use_llm=use_llm)
    action = build_action(inferred, labels, flow)
    session = runs[0].session if runs else "manual"
    card = merge(action, inferred, flow, existing, source=f"flowmap capture {session}")
    minimised = None
    if replay is not None:
        args = inferred.examples[0]["params"] if inferred.examples else {}
        minimised = await minimise(card, flow.action, args, replay, pause_s=pause_s)
        card = minimised.card
        if minimised.tier == "browser":  # the browser tier needs the gateway's pool: no replay here
            replay = None
    final = await finalise(card, flow.action, registry=registry, replay=replay, note=note)
    return Mapped(inferred, labels, minimised, final)
