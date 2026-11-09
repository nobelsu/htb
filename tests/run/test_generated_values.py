"""Values made fresh for each call (CARD_SPEC "Placeholders", ``gen``): idempotency keys, nonces, times."""

from __future__ import annotations

import copy
import re
from typing import Any

import pytest

from spider.card import ResultCode, validate_card
from spider.card.placeholders import PlaceholderError, parse_placeholder
from spider.run.confirm import Confirmer
from spider.run.executor import Executor
from spider.run.request.template import Context, resolve
from spider.store.cards import StoredCard
from spider.store.sessions import InMemorySessionStore
from tests.sites.fixture_site.app import state

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def test_gen_refs_are_checked() -> None:
    assert parse_placeholder("gen.uuid").namespace == "gen"
    assert parse_placeholder("gen.hex | urlencode").filters[0].name == "urlencode"
    for bad in ("gen.random", "gen", "gen.uuid.x"):
        with pytest.raises(PlaceholderError):
            parse_placeholder(bad)


def test_one_value_per_name_per_call() -> None:
    call = Context("https://s.test", {})
    first = resolve("{{gen.uuid}}", call)
    assert UUID.match(first) and resolve("id={{gen.uuid}}", call) == f"id={first}"  # the same within a call
    assert resolve("{{gen.hex}}", call) != first and len(resolve("{{gen.hex}}", call)) == 32
    assert isinstance(resolve("{{gen.now_ms}}", call), int)  # typed when it's the whole value
    assert resolve("{{gen.uuid}}", Context("https://s.test", {})) != first  # a new call, a new value


async def test_each_call_sends_a_new_value(fixture_card: dict[str, Any]) -> None:
    card = copy.deepcopy(fixture_card)
    search = next(a for a in card["actions"] if a["name"] == "search_products")
    search["steps"][0]["request"]["headers"] = {"idempotency-key": "{{gen.uuid}}", "x-nonce": "n-{{gen.hex}}"}
    report = validate_card(card)
    assert report.ok, report.errors
    assert report.card is not None
    stored = StoredCard(report.card.domain, 1, report.card, True, "2026-11-08T00:00:00Z", None)
    executor = Executor(InMemorySessionStore(), Confirmer("s"))
    keys = []
    for _ in range(2):
        env = await executor.run(stored, "search_products", {"query": "lamp"}, user="alice")
        assert env.status == ResultCode.SUCCESS
        sent = state.last_headers["GET /api/products"]
        assert UUID.match(sent["idempotency-key"]) and re.match(r"^n-[0-9a-f]{32}$", sent["x-nonce"])
        keys.append(sent["idempotency-key"])
    assert keys[0] != keys[1]
