"""Heal: the runtime's ``Remapper``, done by replaying the person's recorded flow (``REMAPPER=flowmap``).

HealManager (runtime/healing.py) owns the CARD_SPEC Healing steps around this: the pre-check (blocks, logins
and rate limits cancel the heal), validating the changed actions against their examples, saving the new
version (healed writes go live, DECISIONS M2) and marking ``degraded`` when a heal fails. It also records
``remap_start`` / ``remap_done`` / ``heal_done`` events with timings in ``heal_events``.

What this does, for each broken action only: replay its flow with fresh markers (capture), re-infer it, keep
the agent-facing names it had (description, param descriptions, output field names, following renamed
fields where it can), and put it back into the card. Unchanged actions are left as they are.
``last_details`` holds per-action timings, confidence and a diff (old action → new) for the heal event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flowmap.assemble import AssembleError, build_action, merge
from flowmap.capture import Run, capture_flow
from flowmap.flows import Flow, FlowError, flow_path, load_flow
from flowmap.infer import InferError, Inferred, infer
from flowmap.label import Labels, rule_labels
from flowmap.minimise import Replayer, minimise
from runtime.healing import RemapError
from runtime.sessions import SessionStore
from schema import Card

log = logging.getLogger("flowmap.heal")
Capture = Callable[..., Awaitable[list[Run]]]


def keep_labels(old: dict[str, Any], inferred: Inferred, flow: Flow) -> tuple[Labels, list[str]]:
    """The old action's agent-facing names, carried onto the re-inferred action.

    An output field keeps its name when its path still exists. When it doesn't (the site renamed it), it moves
    to the one new path under the same parent that no other field uses, else to the one new path with the
    same last segment. A field with neither is dropped, and reported.
    """
    notes: list[str] = []
    params = {n: p.get("description") or n for n, p in old.get("params", {}).items() if n in inferred.params}
    candidates = list(inferred.output.get("fields", {}))
    old_fields: dict[str, str] = dict(old.get("output", {}).get("fields", {}) or {})
    used = {p for p in old_fields.values() if p in candidates}
    fields: dict[str, str] = {}
    for name, path in old_fields.items():
        if path in candidates:
            fields[name] = path
            continue
        parent, _, last = path.rpartition(".")
        free = [c for c in candidates if c not in used and c not in fields.values()]
        siblings = [c for c in free if c.rpartition(".")[0] == parent]
        same_last = [c for c in free if c.rpartition(".")[2] == last]
        moved = siblings[0] if len(siblings) == 1 else (same_last[0] if len(same_last) == 1 else None)
        if moved is None:
            notes.append(f"output field {name!r} ({path}) is gone and has no clear new path; dropped")
            continue
        fields[name] = moved
        notes.append(f"output field {name!r} moved: {path} -> {moved}")
    if candidates and not fields:
        fields = rule_labels(inferred, flow).fields
        notes.append("no old output field could be found again; default fields used")
    labels = Labels(
        description=old.get("description") or rule_labels(inferred, flow).description,
        params=params,
        fields=fields,
        source="kept",
    )
    return labels, notes


def _leaves(value: Any, path: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out.update(_leaves(v, f"{path}.{k}" if path else str(k)))
        return out or {path: {}}
    if isinstance(value, list):
        out = {}
        for i, v in enumerate(value):
            out.update(_leaves(v, f"{path}[{i}]"))
        return out or {path: []}
    return {path: value}


def diff_actions(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """What a heal changed in an action, one line per changed value."""
    a, b = _leaves(old), _leaves(new)

    def show(v: Any) -> str:
        text = json.dumps(v, ensure_ascii=False)
        return text if len(text) <= 80 else text[:77] + "..."

    lines = []
    for key in sorted(set(a) | set(b)):
        if key not in b:
            lines.append(f"- {key}: {show(a[key])}")
        elif key not in a:
            lines.append(f"+ {key}: {show(b[key])}")
        elif a[key] != b[key]:
            lines.append(f"~ {key}: {show(a[key])} -> {show(b[key])}")
    return lines


class FlowRemapper:
    def __init__(
        self,
        flows_dir: str | Path = "flows",
        captures_dir: str | Path = "captures",
        *,
        sessions: SessionStore | None = None,
        user: str = "default",
        headless: bool = True,
        replay: Replayer | None = None,
        pause_s: float = 2.0,
        capture: Capture | None = None,
    ) -> None:
        self.flows_dir = Path(flows_dir)
        self.captures_dir = Path(captures_dir)
        self.sessions = sessions
        self.user = user
        self.headless = headless
        self.replay = replay
        self.pause_s = pause_s
        self._capture: Capture = capture or capture_flow
        self._lock = asyncio.Lock()  # one browser re-capture at a time
        self.last_details: dict[str, Any] = {}

    async def remap(self, card: Card, actions: list[str], reason: str) -> dict[str, Any]:
        data = card.to_json_dict()
        details: dict[str, Any] = {}
        async with self._lock:
            for name in actions:
                data, details[name] = await self._remap_one(card, data, name)
        self.last_details = details
        return data

    async def _remap_one(
        self, card: Card, data: dict[str, Any], name: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        t0 = time.monotonic()
        path = flow_path(self.flows_dir, card.domain, name)
        try:
            flow = load_flow(path)
        except FlowError as e:
            raise RemapError(f"no usable flow for {name}: {e}") from e
        old = next((a for a in data["actions"] if a["name"] == name), None)
        if old is None:
            raise RemapError(f"{name} is not in the card")
        state = self._storage_state(card, old)
        try:
            runs = await self._capture(
                flow,
                root=self.captures_dir,
                headless=self.headless,
                storage_state=state,
                pause_s=self.pause_s,
            )
        except Exception as e:
            raise RemapError(f"replaying the flow for {name} failed: {type(e).__name__}: {e}") from e
        finally:
            if state is not None:
                state.unlink(missing_ok=True)
        t_capture = time.monotonic()
        failed = [r.error for r in runs if r.error]
        if failed and len(failed) == len(runs):
            raise RemapError(f"every replay of {name} failed: {failed[0]}")
        try:
            inferred = infer(flow, runs)
        except InferError as e:
            raise RemapError(f"re-inferring {name} failed: {e} ({'; '.join(e.reasons)})") from e
        t_infer = time.monotonic()
        labels, notes = keep_labels(old, inferred, flow)
        action = build_action(inferred, labels, flow)
        for key in ("tier", "cache_ttl_s", "rate_limit_per_min", "timeout_ms", "verify"):
            if key in old and key not in action:
                action[key] = old[key]
        if inferred.kind == "read" and not action["examples"]:
            action["examples"] = old.get("examples", [])
        try:
            new_card = merge(action, inferred, flow, data, source=f"flowmap heal {runs[0].session}")
        except AssembleError as e:
            raise RemapError(str(e)) from e
        minimised = None
        if self.replay is not None and inferred.examples:
            minimised = await minimise(
                new_card, name, inferred.examples[0]["params"], self.replay, pause_s=self.pause_s
            )
            if minimised.tier != "browser":
                new_card = minimised.card
        new_action = next(a for a in new_card["actions"] if a["name"] == name)
        t_end = time.monotonic()
        diff = diff_actions(old, new_action)
        info: dict[str, Any] = {
            "capture_ms": int((t_capture - t0) * 1000),
            "infer_ms": int((t_infer - t_capture) * 1000),
            "total_ms": int((t_end - t0) * 1000),
            "runs": len(runs),
            "confidence": inferred.confidence,
            "reasons": inferred.reasons + notes + (minimised.reasons if minimised else []),
            "diff": diff,
        }
        log.info("re-mapped %s.%s in %d ms (%d changes)", card.domain, name, info["total_ms"], len(diff))
        return new_card, info

    def _storage_state(self, card: Card, old: dict[str, Any]) -> Path | None:
        """The user's sign-in as a Playwright storage state (0600 temp file), for actions that need one."""
        provider = old.get("auth_provider") or card.defaults.auth_provider
        if provider is None or self.sessions is None:
            return None
        cookies = self.sessions.get(self.user, card.domain, provider)
        if not cookies:
            raise RemapError(f"no sign-in for {card.domain} to replay {old['name']} with")
        host = urlsplit(card.base_url).hostname or card.domain
        state = {
            "cookies": [{"name": k, "value": v, "domain": host, "path": "/"} for k, v in cookies.items()],
            "origins": [],
        }
        fd, name = tempfile.mkstemp(prefix="flowmap-state-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.chmod(name, 0o600)
        return Path(name)
