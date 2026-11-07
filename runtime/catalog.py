"""The action catalog behind the generic `find_actions` tool: every active card's actions, searchable."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from runtime.registry import StoredCard
from schema.card import Action, Card
from schema.params import input_schema, tool_description

MAX_RESULTS = 10
_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _needs_sign_in(card: Card, action: Action) -> bool:
    return action.effective_auth_provider(card.defaults) is not None


def describe_action(card: Card, action: Action) -> dict[str, Any]:
    """What an agent needs to call `run_action` for this action."""
    return {
        "site": card.domain,
        "action": action.name,
        "description": tool_description(action),
        "kind": action.kind,
        "needs_sign_in": _needs_sign_in(card, action),
        "requires_confirmation": action.requires_confirmation,
        "params": input_schema(action.params, requires_confirmation=False),
    }


def describe_site(card: Card) -> dict[str, Any]:
    return {
        "site": card.domain,
        "actions": len(card.actions),
        "needs_sign_in": any(_needs_sign_in(card, a) for a in card.actions),
        "examples": [a.name for a in card.actions[:6]],
    }


def _score(card: Card, action: Action, query_words: set[str]) -> int:
    name_words = _words(action.name.replace("_", " "))
    site_words = _words(card.domain)
    text_words = _words(action.description) | _words(" ".join(action.params))
    score = 0
    for word in query_words:
        if word in name_words:
            score += 3
        elif word in site_words:
            score += 2
        elif word in text_words:
            score += 1
        elif any(w.startswith(word) or word.startswith(w) for w in name_words | text_words if len(w) > 3):
            score += 1  # plural/singular and partial words ("orders" ~ "order")
    return score


def find_actions(
    cards: Mapping[str, StoredCard], query: str | None = None, site: str | None = None
) -> dict[str, Any]:
    """No arguments: the sites. `site` only: all its actions. `query` (and maybe `site`): best matches."""
    if site is not None and site not in cards:
        known = ", ".join(sorted(cards)) or "none"
        return {"error": f"unknown site {site!r}; known sites: {known}"}
    if not query and site is None:
        return {"sites": [describe_site(s.card) for _, s in sorted(cards.items())]}
    pool = [cards[site]] if site is not None else [s for _, s in sorted(cards.items())]
    if not query:
        return {"actions": [describe_action(s.card, a) for s in pool for a in s.card.actions]}
    query_words = _words(query)
    scored = [
        (score, s.card.domain, a.name, s.card, a)
        for s in pool
        for a in s.card.actions
        if (score := _score(s.card, a, query_words)) > 0
    ]
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    found = [describe_action(card, action) for _, _, _, card, action in scored[:MAX_RESULTS]]
    out: dict[str, Any] = {"actions": found}
    if not found:
        out["hint"] = "nothing matched; call find_actions with no arguments to see the available sites"
    return out
