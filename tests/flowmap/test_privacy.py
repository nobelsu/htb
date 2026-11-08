"""No personal data in a shared card's examples (flowmap/privacy.py), against the fixture shop."""

from __future__ import annotations

import copy
from typing import Any

from flowmap.privacy import by_rule, public_examples
from runtime.run.confirm import Confirmer
from runtime.run.executor import Executor
from runtime.signin.sessions import InMemorySessionStore


class Checker:
    """Stands in for Claude's check: ``verdicts`` is param -> replacement for the values it calls personal."""

    def __init__(self, verdicts: dict[str, str], fixed: list[dict[str, str]] | None = None) -> None:
        self.verdicts = verdicts
        self.fixed = fixed or []
        self.calls: list[dict[str, Any]] = []
        self.beta = self
        self.messages = self

    async def create(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        values = [{"param": k, "personal": True, "replacement": v} for k, v in self.verdicts.items()]
        answer = {"values": values, "fixed": self.fixed}
        return {"content": [{"type": "tool_use", "name": "check_examples", "input": answer}]}


def card_with(fixture_card: dict[str, Any], query: str) -> dict[str, Any]:
    card = copy.deepcopy(fixture_card)
    card["actions"] = [a for a in card["actions"] if a["name"] in ("search_products", "get_product")]
    card["connect_providers"] = []
    search = card["actions"][0]
    search["examples"] = [{"params": {"query": query}, "min_items": 1}]
    return card


def executor() -> Executor:
    return Executor(InMemorySessionStore(), Confirmer("s"))


def test_emails_and_phone_numbers_are_always_personal() -> None:
    assert by_rule("alice@example.com") and by_rule("+44 20 7946 0958")
    assert not by_rule("robotics") and not by_rule("SKU-001") and not by_rule("2026")


async def test_a_personal_example_is_swapped_for_a_public_one_that_works(
    fixture_card: dict[str, Any],
) -> None:
    card = card_with(fixture_card, "alice-smith-1234")
    client = Checker({"query": "lamp"})  # "a person's id": the shop's products stand in
    out, report = await public_examples(
        card, {"search_products"}, user="u", executor=executor(), client=client
    )
    search = next(a for a in out["actions"] if a["name"] == "search_products")
    assert search["examples"] == [{"params": {"query": "lamp"}, "min_items": 1}]
    assert report.replaced == {"search_products": {"query": "lamp"}} and not report.dropped
    assert "alice-smith-1234" in client.calls[0]["messages"][0]["content"]  # Claude saw what to check
    assert "alice-smith-1234" not in str(out)


async def test_without_a_working_stand_in_the_action_is_not_saved(fixture_card: dict[str, Any]) -> None:
    card = card_with(fixture_card, "alice@example.com")  # personal by rule; no model to suggest a stand-in
    out, report = await public_examples(
        card, {"search_products"}, user="u", executor=executor(), use_llm=False
    )
    assert "search_products" in report.dropped and [a["name"] for a in out["actions"]] == ["get_product"]

    card = card_with(fixture_card, "alice-smith-1234")
    client = Checker({"query": "zzz-nothing-matches"})  # a stand-in that finds nothing: not good enough
    out, report = await public_examples(
        card, {"search_products"}, user="u", executor=executor(), client=client
    )
    assert "stand-in failed" in report.dropped["search_products"]


async def test_public_examples_are_left_alone(fixture_card: dict[str, Any]) -> None:
    card = card_with(fixture_card, "lamp")
    out, report = await public_examples(
        card, {"search_products"}, user="u", executor=executor(), client=Checker({})
    )
    assert out == card and not report.replaced and not report.dropped


def baked_in(fixture_card: dict[str, Any]) -> dict[str, Any]:
    """get_product with its SKU fixed in the request, as if recorded as "open my item"."""
    card = card_with(fixture_card, "lamp")
    product = next(a for a in card["actions"] if a["name"] == "get_product")
    product["steps"][0]["request"]["query"] = {"sku": "SKU-001"}
    product["params"] = {}
    product["examples"] = [{"params": {}, "min_items": 1}]
    return card


async def test_the_recorders_own_fixed_value_becomes_an_input(fixture_card: dict[str, Any]) -> None:
    card = baked_in(fixture_card)
    client = Checker({}, fixed=[{"value": "SKU-001", "param": "sku", "replacement": "SKU-002"}])
    out, report = await public_examples(card, {"get_product"}, user="u", executor=executor(), client=client)
    product = next(a for a in out["actions"] if a["name"] == "get_product")
    assert product["steps"][0]["request"]["query"] == {"sku": "{{params.sku}}"}
    assert product["params"]["sku"]["required"] and product["params"]["sku"]["example"] == "SKU-002"
    assert product["examples"] == [{"params": {"sku": "SKU-002"}, "min_items": 1}]
    assert report.replaced == {"get_product": {"sku": "SKU-002"}}
    assert '"SKU-001"' in client.calls[0]["messages"][0]["content"]  # Claude was shown the fixed values
    assert "SKU-001" not in str(product)


async def test_a_fixed_value_without_a_working_stand_in_drops_the_action(
    fixture_card: dict[str, Any],
) -> None:
    card = baked_in(fixture_card)
    client = Checker({}, fixed=[{"value": "SKU-001", "param": "sku", "replacement": "SKU-404"}])
    out, report = await public_examples(card, {"get_product"}, user="u", executor=executor(), client=client)
    assert "get_product" in report.dropped and "get_product" not in [a["name"] for a in out["actions"]]
