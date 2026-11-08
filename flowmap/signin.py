"""The card's sign-in (``connect_providers``) for a site mapped while signed in (DECISIONS M8).

A card for a site behind a sign-in needs a ``session_check``: a request that passes when the stored sign-in
still works and fails when it doesn't. A new site has none, so it is worked out from the action just mapped:

1. candidates, in order: each step of the mapped action that needs no earlier step's value, with the
   example's values written in (the action's own request, so it surely needs the sign-in if anything does),
   then the page the task started on;
2. each candidate is sent twice through the executor, with the requester's stored sign-in and with no
   cookies at all;
3. the first that passes signed in (and doesn't end on the login page) and is told apart signed out wins.
   The signed-out response gives the ``logged_out_signals``: its status (a 401 or 403) and the paths it was
   redirected to (the login page), each kept only if the signed-in response doesn't match it too.

Each is sent on the tier its action uses (the start page: the first action's), and the card's default tier is
set to the winner's, since the executor runs a ``session_check`` on the default tier.

When no candidate can tell the two apart the first one that passes signed in is used and the check is "weak":
Connect then asks the user to confirm their sign-in, and an expired sign-in shows up as failing actions rather
than in the keep-alive. Cookie values never go into the card: only ``params`` placeholders are filled in.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit

from runtime.executor import Executor
from runtime.signals import match_signal
from runtime.template import Context, MissingParam, evaluate, to_text
from runtime.tiers import HttpResponse
from schema import ResultCode, validate_card
from schema.card import Tier
from schema.placeholders import find_placeholders, is_valid_signal


class SignInCheckError(Exception):
    """No request passes with the requester's sign-in."""


@dataclass
class SignInCheck:
    provider: dict[str, Any]
    strong: bool
    tier: str = "http"
    tried: list[str] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        check = self.provider["session_check"]
        return {
            "check": f"{check['method']} {check['url']}",
            "strength": "strong" if self.strong else "weak",
            "tier": self.tier,
            "logged_out_signals": list(self.provider["logged_out_signals"]),
            "tried": self.tried,
        }


def placeholder_provider(provider: str, login_url: str, start_url: str) -> dict[str, Any]:
    """The provider a card is mapped with before its real check is known (validation needs one)."""
    return {
        "id": provider,
        "login_url": login_url,
        "session_check": _page_check(start_url, ""),
        "logged_out_signals": [],
    }


async def infer_sign_in(
    card: dict[str, Any],
    provider: str,
    *,
    login_url: str,
    start_url: str,
    user: str,
    executor: Executor,
) -> SignInCheck:
    cookies = executor.sessions.get(user, card["domain"], provider)
    if not cookies:
        raise SignInCheckError(f"{user} has no stored sign-in for {card['domain']}")
    weak: SignInCheck | None = None
    tried: list[str] = []
    login_path = urlsplit(login_url).path
    if login_path == urlsplit(start_url).path:
        login_path = ""  # no separate login page known (the start page stands in for it): nothing to guard
    for check, tier in candidates(card, provider, start_url):
        tried.append(f"{check['method']} {check['url']} ({tier})")
        entry = {"id": provider, "login_url": login_url, "session_check": check, "logged_out_signals": []}
        trial = _with_provider(card, entry)
        if trial is None:
            continue
        signed_in = await executor.session_response(
            trial, provider, user, dict(cookies), tier=cast(Tier, tier)
        )
        if not isinstance(signed_in, HttpResponse):
            continue
        if Executor.session_verdict(trial, trial.connect_providers[0], signed_in) is not None:
            continue
        if login_path not in ("", "/") and _path(signed_in.final_url).startswith(login_path):
            continue  # "passed" by landing on the login page: the sign-in didn't reach it
        signed_out = await executor.session_response(trial, provider, user, {}, tier=cast(Tier, tier))
        if signed_out == ResultCode.AUTH_EXPIRED:  # it needs a session cookie that isn't there
            return SignInCheck(entry, strong=True, tier=tier, tried=tried)
        if not isinstance(signed_out, HttpResponse):
            continue
        entry["logged_out_signals"] = signals_between(signed_in, signed_out, login_url)
        trial = _with_provider(card, entry)
        if trial is None:
            continue
        verdict = Executor.session_verdict(trial, trial.connect_providers[0], signed_out)
        if verdict == ResultCode.AUTH_EXPIRED:
            return SignInCheck(entry, strong=True, tier=tier, tried=tried)
        if weak is None:
            weak = SignInCheck(entry, strong=False, tier=tier)
    if weak is None:
        raise SignInCheckError("no request passed with the sign-in: " + "; ".join(tried))
    weak.tried = tried
    return weak


def candidates(card: dict[str, Any], provider: str, start_url: str) -> list[tuple[dict[str, Any], str]]:
    """Session checks to try, each with the tier to send it on: the signed-in actions' steps (example values
    filled in), then the start page."""
    out: list[tuple[dict[str, Any], str]] = []
    defaults = card.get("defaults") or {}
    page_tier: str | None = None
    for action in card.get("actions", []):
        if (
            action.get("kind") != "read"
            or (action.get("auth_provider") or defaults.get("auth_provider")) != provider
        ):
            continue
        tier = action.get("tier") or defaults.get("tier") or "http"
        if tier == "browser":
            continue  # the browser tier needs the gateway's pool
        page_tier = page_tier or tier
        params = {
            name: spec["default"] for name, spec in action.get("params", {}).items() if "default" in spec
        }
        if action.get("examples"):
            params.update(action["examples"][0]["params"])
        for step in action.get("steps", []):
            request = step["request"]
            text = repr(request)
            if "steps." in text or "escape_restli" in text:
                continue  # needs an earlier step's value, or Rest.li encoding a filled-in value would lose
            try:
                filled = {key: _fill(request.get(key), params) for key in ("url", "query", "headers", "body")}
            except Exception:
                continue
            check = {"method": request["method"], **{k: v for k, v in filled.items() if v not in (None, {})}}
            check["expect"] = copy.deepcopy(step["expect"])
            out.append((check, tier))
    out.append((_page_check(start_url, card["base_url"]), page_tier or defaults.get("tier") or "http"))
    return out


def signals_between(signed_in: HttpResponse, signed_out: HttpResponse, login_url: str) -> list[str]:
    """What the signed-out response shows that the signed-in one doesn't: its status, then the paths it was
    sent to (the login page first)."""
    found: list[str] = []
    if signed_out.status != signed_in.status:
        found.append(f"status:{signed_out.status}")
    login_path = urlsplit(login_url).path
    paths = [_path(loc) for _, loc in signed_out.hops if loc] + [_path(signed_out.final_url)]
    paths.sort(key=lambda p: p != login_path)
    found += [f"redirect:{p}" for p in dict.fromkeys(paths) if p != "/"]
    return [s for s in found if is_valid_signal(s) and match_signal([s], signed_in) is None]


def _path(location: str) -> str:
    return urlsplit(location).path or "/"


def _page_check(start_url: str, base_url: str) -> dict[str, Any]:
    parts, base = urlsplit(start_url), urlsplit(base_url)
    if (parts.scheme, parts.netloc) == (base.scheme, base.netloc):
        url = "{{base_url}}" + (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    else:
        url = start_url
    return {"method": "GET", "url": url, "expect": {"status": [200], "content_type": "html"}}


def _with_provider(card: dict[str, Any], provider: dict[str, Any]) -> Any:
    trial = copy.deepcopy(card)
    trial["connect_providers"] = [provider]
    report = validate_card(trial)
    return report.card if report.ok else None


def _fill(value: Any, params: dict[str, Any]) -> Any:
    """``value`` with its ``{{params.X}}`` placeholders filled in and every other placeholder left as it is.
    Keys whose optional param has no value are dropped, as the executor does."""
    if isinstance(value, str):
        phs = [ph for ph in find_placeholders(value) if ph.namespace == "params"]
        if not phs:
            return value
        ctx = Context("", params)
        if len(phs) == 1 and phs[0].raw == value:
            return evaluate(phs[0], ctx)
        for ph in phs:
            value = value.replace(ph.raw, to_text(evaluate(ph, ctx)), 1)
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            try:
                out[k] = _fill(v, params)
            except MissingParam:
                continue
        return out
    if isinstance(value, list):
        items = []
        for v in value:
            try:
                items.append(_fill(v, params))
            except MissingParam:
                continue
        return items
    return value
