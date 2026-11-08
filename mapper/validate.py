"""Validate a card: static save-time rules, then (optionally) live runs of its examples.

Static: Nobel's ``schema.validate_card`` (the spec's save-time rules), ``tools/check_card.py`` when it
exists, and ``mapper.checks`` (no live secrets in the card).

Live (opt-in, ``--live``): every read action's ``examples`` run through the executor with the capture's
session. Writes never run live. This sends real requests to the site with the operator's session, so it
is off by default (rate limits, ban risk).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from mapper import checks

CHECK_CARD = Path(__file__).resolve().parent.parent / "tools" / "check_card.py"


# ==== EXECUTOR HOOK ===============================================================================
# The mapper never replays HTTP itself (mapper/CLAUDE.md). Live validation goes through this protocol;
# ``RuntimeExecutor`` adapts Nobel's ``runtime.executor.Executor`` to it.


class Executor(Protocol):
    def run_action(
        self,
        card: dict,
        action: str,
        params: dict,
        *,
        session_cookies: dict[str, str] | None = None,
    ) -> dict:
        """Run one action and return the spec's result envelope as a dict."""
        ...


class RuntimeExecutor:
    """``Executor`` over ``runtime.executor.Executor`` (http tier), with an in-memory session."""

    http_only = True  # no browser pool here: browser-tier actions are left to the gateway

    def run_action(self, card, action, params, *, session_cookies=None) -> dict:
        import asyncio
        import secrets

        from mapper.bundle import iso_now
        from runtime.confirm import Confirmer
        from runtime.executor import Executor as RuntimeExec
        from runtime.registry import StoredCard
        from runtime.sessions import InMemorySessionStore
        from schema import load_card

        parsed = load_card(card)
        act = parsed.action(action)
        if act is None:
            raise KeyError(action)
        sessions = InMemorySessionStore()
        provider = act.effective_auth_provider(parsed.defaults)
        if provider and session_cookies:
            sessions.put("default", parsed.domain, provider, session_cookies)
        stored = StoredCard(parsed.domain, 0, parsed, False, iso_now(), "mapper live validation")
        envelope = asyncio.run(
            RuntimeExec(sessions, Confirmer(secrets.token_hex(16))).run(stored, action, params)
        )
        return envelope.model_dump(mode="json")


# ==================================================================================================


@dataclass
class LiveResult:
    action: str
    params: dict
    status: str
    items: int
    min_items: int
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "SKIPPED" or (self.status == "SUCCESS" and self.items >= self.min_items)


@dataclass
class Validation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    live: list[LiveResult] = field(default_factory=list)
    ran: list[str] = field(default_factory=list)  # which checkers ran

    @property
    def ok(self) -> bool:
        return not self.errors and all(r.ok for r in self.live)


def run_check_card(card: dict) -> list[str] | None:
    """``tools/check_card.py`` errors, or None when the tool isn't there."""
    if not CHECK_CARD.exists():
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(card, fh)
        path = fh.name
    try:
        out = subprocess.run(
            [sys.executable, str(CHECK_CARD), path], capture_output=True, text=True, timeout=60
        )
    finally:
        Path(path).unlink(missing_ok=True)
    lines = [ln for ln in (out.stdout + out.stderr).splitlines() if ln.strip()]
    if out.returncode != 0 and not lines:
        lines = [f"exited with {out.returncode}"]
    return [] if lines == ["OK: no issues"] else lines


def static_validate(card: dict, secrets: set[str] | None = None) -> Validation:
    from schema import validate_card

    result = Validation()
    report = validate_card(card)
    result.ran.append("schema.validate_card")
    result.errors += [f"schema: {e}" for e in report.errors]
    result.warnings += [f"schema: {w}" for w in report.warnings]
    tool = run_check_card(card)
    if tool is not None:
        result.ran.append("tools/check_card.py")
        result.errors += [f"check_card: {e}" for e in tool]
    if secrets:
        result.ran.append("mapper.checks (secrets)")
        result.errors += [f"mapper: {e}" for e in checks.leaked_secrets(card, secrets)]
    return result


def live_validate(card: dict, executor: Executor, session_cookies: dict[str, str] | None) -> list[LiveResult]:
    results = []
    for action in card["actions"]:
        if action["kind"] != "read":
            continue
        tier = action.get("tier") or (card.get("defaults") or {}).get("tier")
        if tier == "browser" and getattr(executor, "http_only", False):  # the gateway's browser pool runs these
            for ex in action.get("examples", []):
                results.append(
                    LiveResult(
                        action["name"], ex["params"], "SKIPPED", 0, ex.get("min_items", 1),
                        "needs the browser tier: test it through the gateway",
                    )
                )
            continue
        for ex in action.get("examples", []):
            try:
                env = executor.run_action(card, action["name"], ex["params"], session_cookies=session_cookies)
            except Exception as e:  # report, don't crash the mapping run
                results.append(
                    LiveResult(action["name"], ex["params"], "CRASH", 0, ex.get("min_items", 1), str(e))
                )
                continue
            error = env.get("error") or {}
            results.append(
                LiveResult(
                    action["name"],
                    ex["params"],
                    env.get("status", "?"),
                    len(env.get("data") or []),
                    ex.get("min_items", 1),
                    error.get("message", "") if isinstance(error, dict) else "",
                )
            )
    return results


def load_state_cookies(path: str | Path) -> dict[str, str]:
    state = json.loads(Path(path).read_text())
    return {c["name"]: c["value"] for c in state.get("cookies", [])}


def print_validation(v: Validation, out=sys.stdout) -> None:
    print(f"checks run: {', '.join(v.ran)}", file=out)
    for e in v.errors:
        print(f"ERROR   {e}", file=out)
    for w in v.warnings:
        print(f"warning {w}", file=out)
    for r in v.live:
        mark = "ok  " if r.ok else "FAIL"
        print(
            f"live {mark} {r.action}({r.params}) -> {r.status}, {r.items} item(s) {r.message}".rstrip(),
            file=out,
        )
    print("VALID" if v.ok else "INVALID", file=out)


def main(args: argparse.Namespace) -> int:
    card = json.loads(Path(args.card).read_text())
    v = static_validate(card)
    if getattr(args, "live", False):
        cookies = load_state_cookies(args.state) if args.state else None
        v.live = live_validate(card, RuntimeExecutor(), cookies)
    print_validation(v)
    return 0 if v.ok else 1
