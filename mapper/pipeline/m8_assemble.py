"""M8 assemble: actions → card (site-level fields), ready for validation.

* ``schema_version``, ``domain``, ``tool_prefix``, ``base_url``, ``provenance``.
* ``blocked_signals``: ``status:403`` plus ``body_contains:`` for any challenge page seen in capture.
* ``connect_providers``: when any action sends session cookies, one **placeholder** provider (see
  docs/mapper-design.md "Open items": Nobel's hosted-login flow owns the real values). Actions that send
  session cookies inherit it through ``defaults.auth_provider``; the others get ``auth_provider: null``.
* ``defaults``: tier, auth, timeouts and limits, plus any header every step of every action sends with
  the same template.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from mapper import __version__, placeholders
from mapper.bundle import iso_now
from mapper.domains import tool_prefix
from mapper.pipeline.m1_load import Entry
from mapper.pipeline.m5_steps import StepPlan

DEFAULTS = {"tier": "http", "timeout_ms": 10_000, "cache_ttl_s": 120, "rate_limit_per_min": 20}

CHALLENGE_MARKERS = (
    "captcha",
    "cf-chl",
    "challenge-platform",
    "px-captcha",
    "Please verify you are a human",
    "Checking your browser",
)

PLACEHOLDER_PROVIDER_NOTE = (
    "HIGH PRIORITY (Nobel): connect_providers is a placeholder. login_url, session_check and "
    "logged_out_signals belong to the hosted-login flow (docs/mapper-design.md, Open items)."
)


@dataclass
class MappedAction:
    """One action ready for the card, plus what M8 needs to know about it."""

    action: dict
    plans: list[StepPlan]
    uses_session: bool
    start_url: str
    entries: list[Entry] = field(default_factory=list)  # every M1-kept entry, for challenge detection


def challenge_signals(entries: list[Entry]) -> list[str]:
    found = []
    for e in entries:
        body = (e.response_body or "")[:200_000].lower()
        for marker in CHALLENGE_MARKERS:
            if marker.lower() in body and f"body_contains:{marker}" not in found:
                found.append(f"body_contains:{marker}")
    return found


def _is_plain_get(plan: StepPlan) -> bool:
    req = plan.request
    text = repr(req)
    return req["method"] == "GET" and "{{params." not in text and "{{steps." not in text


def placeholder_provider(provider_id: str, mapped: list[MappedAction], base_url: str) -> dict:
    """Smallest schema-valid provider: Nobel's login flow replaces these values."""
    check = None
    for m in mapped:
        for plan in m.plans:
            if m.uses_session and _is_plain_get(plan) and plan.draft.entry.response_kind == "json":
                check = {**copy.deepcopy(plan.request), "expect": copy.deepcopy(plan.expect)}
                break
        if check:
            break
    if check is None:
        check = {
            "method": "GET",
            "url": placeholders.BASE_URL + "/",
            "query": {},
            "headers": {},
            "body": None,
            "expect": {"status": [200], "content_type": "any", "paths": []},
        }
    start = mapped[0].start_url if mapped else base_url + "/"
    return {"id": provider_id, "login_url": start, "session_check": check, "logged_out_signals": []}


def _hoist_headers(actions: list[dict]) -> dict[str, str]:
    steps = [s for a in actions for s in a["steps"]]
    if not steps:
        return {}
    common = dict(steps[0]["request"]["headers"])
    for s in steps[1:]:
        common = {k: v for k, v in common.items() if s["request"]["headers"].get(k) == v}
    for s in steps:
        for k in common:
            s["request"]["headers"].pop(k, None)
    return common


def assemble(domain: str, base_url: str, mapped: list[MappedAction]) -> tuple[dict, list[str]]:
    """Returns ``(card, notes)``. ``notes`` go to the mapping report."""
    notes: list[str] = []
    prefix = tool_prefix(domain)
    actions = [copy.deepcopy(m.action) for m in mapped]
    uses_session = any(m.uses_session for m in mapped)
    for m, a in zip(mapped, actions, strict=True):
        if uses_session and not m.uses_session:
            a["auth_provider"] = None
    defaults = {  # key order as in action_map.example.json (the registry keeps it)
        "tier": DEFAULTS["tier"],
        "auth_provider": prefix if uses_session else None,
        "headers": _hoist_headers(actions),
        "timeout_ms": DEFAULTS["timeout_ms"],
        "cache_ttl_s": DEFAULTS["cache_ttl_s"],
        "rate_limit_per_min": DEFAULTS["rate_limit_per_min"],
    }
    if all(a.get("tier") == "browser" for a in actions) and actions:
        defaults["tier"] = "browser"
        for a in actions:
            a.pop("tier", None)

    blocked = ["status:403", *challenge_signals([e for m in mapped for e in m.entries])]
    card = {
        "schema_version": "0.4",
        "domain": domain,
        "tool_prefix": prefix,
        "base_url": base_url,
        "blocked_signals": blocked,
        "changed_signals": [],
        "provenance": {"mapped_at": iso_now(), "source": "network_capture", "mapper_version": __version__},
        "connect_providers": [placeholder_provider(prefix, mapped, base_url)] if uses_session else [],
        "defaults": defaults,
        "actions": actions,
    }
    if uses_session:
        notes.append(PLACEHOLDER_PROVIDER_NOTE)
    return card, notes
