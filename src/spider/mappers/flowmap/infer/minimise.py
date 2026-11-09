"""Minimise: replay a read action with the runtime's own executor to find the cheapest tier and drop the
headers it doesn't need.

1. **Tier:** http, then impersonate. The first that returns SUCCESS with at least ``min_items`` items wins.
   If neither does, the action needs the browser tier (which needs the gateway's browser pool, so it isn't
   replayed here).
2. **Headers:** on that tier, each step's headers are dropped one at a time; a header stays only if the
   action stops working (or returns fewer items) without it.

Writes are never replayed (a replay would really add to a cart): they keep every header and the tier the
caller gives. Every replay waits ``pause_s`` first, and a RATE_LIMITED or BLOCKED answer stops minimising
at once, keeping what is left.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from spider.card.envelope import Envelope, ResultCode
from spider.card.model import Card, Tier

TRY_TIERS: tuple[Tier, ...] = ("http", "impersonate")


class Replayer:
    """Runs one action of a card dict on one tier; the runtime executor in practice."""

    async def __call__(self, card: dict[str, Any], action: str, args: dict[str, Any], tier: Tier) -> Envelope:
        raise NotImplementedError


@dataclass
class Minimised:
    card: dict[str, Any]
    tier: Tier
    dropped: list[str] = field(default_factory=list)  # "step_id: header"
    kept: list[str] = field(default_factory=list)
    replays: int = 0
    reasons: list[str] = field(default_factory=list)


def executor_replayer(make_executor: Callable[[Tier], Any], user: str = "default") -> Replayer:
    """A replayer over the runtime executor. ``make_executor(tier)`` returns an Executor whose ``tiers`` hold
    only that tier, so the ladder can't quietly escalate."""

    class _ExecutorReplayer(Replayer):
        async def __call__(
            self, card: dict[str, Any], action: str, args: dict[str, Any], tier: Tier
        ) -> Envelope:
            parsed = Card.model_validate(card)
            return await make_executor(tier).run_internal(parsed, 0, action, args, user)  # type: ignore[no-any-return]

    return _ExecutorReplayer()


def _action(card: dict[str, Any], name: str) -> dict[str, Any]:
    return next(a for a in card["actions"] if a["name"] == name)


def _with_tier(card: dict[str, Any], name: str, tier: Tier) -> dict[str, Any]:
    out = copy.deepcopy(card)
    _action(out, name)["tier"] = tier
    return out


def _items(env: Envelope) -> int:
    return len(env.data or [])


async def minimise(
    card: dict[str, Any],
    action: str,
    args: dict[str, Any],
    replay: Replayer,
    *,
    min_items: int = 1,
    pause_s: float = 1.0,
    write_tier: Tier = "http",
) -> Minimised:
    act = _action(card, action)
    if act["kind"] == "write":
        out = _with_tier(card, action, write_tier)
        kept = [f"{s['id']}: {h}" for s in act["steps"] for h in s["request"].get("headers", {})]
        return Minimised(out, write_tier, kept=kept, reasons=["a write isn't replayed: every header kept"])

    result = Minimised(card, "browser")
    stop: list[ResultCode] = []

    async def works(candidate: dict[str, Any], tier: Tier, want: int) -> bool:
        if stop:
            return False
        if result.replays and pause_s:
            await asyncio.sleep(pause_s)
        result.replays += 1
        env = await replay(candidate, action, args, tier)
        if env.status in (ResultCode.RATE_LIMITED, ResultCode.BLOCKED) and tier == result.tier:
            stop.append(env.status)
            result.reasons.append(
                f"stopped minimising: the site answered {env.status} (headers left as they were)"
            )
            return False
        return env.status == ResultCode.SUCCESS and _items(env) >= want

    baseline = 0
    for tier in TRY_TIERS:
        candidate = _with_tier(card, action, tier)
        if result.replays and pause_s:
            await asyncio.sleep(pause_s)
        result.replays += 1
        env = await replay(candidate, action, args, tier)
        if env.status == ResultCode.SUCCESS and _items(env) >= min_items:
            result.tier, result.card, baseline = tier, candidate, _items(env)
            break
        detail = env.error.message if env.error else ""
        result.reasons.append(f"{tier} tier: {env.status}{' (' + detail + ')' if detail else ''}")
    if result.tier == "browser":
        result.card = _with_tier(card, action, "browser")
        result.reasons.append("neither http nor impersonate worked: the action needs the browser tier")
        return result

    current = result.card
    for step_index, step in enumerate(_action(current, action)["steps"]):
        for header in list(step["request"].get("headers", {})):
            trial = copy.deepcopy(current)
            trial_step = _action(trial, action)["steps"][step_index]
            del trial_step["request"]["headers"][header]
            if not trial_step["request"]["headers"]:
                del trial_step["request"]["headers"]
            if await works(trial, result.tier, baseline):
                current = trial
                result.dropped.append(f"{step['id']}: {header}")
            else:
                result.kept.append(f"{step['id']}: {header}")
            if stop:
                return _finish(result, current)
    return _finish(result, current)


def _finish(result: Minimised, card: dict[str, Any]) -> Minimised:
    result.card = card
    return result
