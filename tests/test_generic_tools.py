"""The default tool set: find_actions + run_action (plus sign-in and heal tools), over real MCP."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from runtime.catalog import find_actions
from runtime.confirm import Confirmer
from runtime.executor import Executor
from runtime.gateway import Gateway
from runtime.registry import Registry
from runtime.sessions import InMemorySessionStore
from schema import load_card
from runtime.registry import StoredCard
from tests.conftest import LiveServer
from tests.test_gateway import API_KEY, connect, envelope

DOMAIN = "fixture.test"


# ---- the catalog ------------------------------------------------------------


@pytest.fixture
def cards(fixture_card: dict[str, Any]) -> dict[str, StoredCard]:
    return {DOMAIN: StoredCard(DOMAIN, 1, load_card(fixture_card), True, "now", None)}


def test_no_arguments_lists_sites(cards: dict[str, StoredCard]) -> None:
    out = find_actions(cards)
    assert out["sites"][0]["site"] == DOMAIN and out["sites"][0]["needs_sign_in"] is True
    assert out["sites"][0]["actions"] == len(cards[DOMAIN].card.actions)


def test_site_lists_all_its_actions(cards: dict[str, StoredCard]) -> None:
    out = find_actions(cards, site=DOMAIN)
    assert len(out["actions"]) == len(cards[DOMAIN].card.actions)
    search = next(a for a in out["actions"] if a["action"] == "search_products")
    assert search["params"]["required"] == ["query"] and search["kind"] == "read"
    assert search["needs_sign_in"] is False


def test_query_ranks_matching_actions(cards: dict[str, StoredCard]) -> None:
    orders = find_actions(cards, query="list my orders")["actions"]
    assert orders[0]["action"] in ("list_orders", "browser_orders") and orders[0]["needs_sign_in"] is True
    assert orders[0]["site"] == DOMAIN
    products = find_actions(cards, query="search products")["actions"]
    assert products[0]["action"] == "search_products"
    cart = find_actions(cards, query="add to cart")["actions"]
    assert cart[0]["action"] == "add_to_cart" and cart[0]["requires_confirmation"] is True
    assert len(find_actions(cards, query="product")["actions"]) <= 10


def test_no_match_and_unknown_site(cards: dict[str, StoredCard]) -> None:
    out = find_actions(cards, query="zebra photosynthesis")
    assert out["actions"] == [] and "no arguments" in out["hint"]
    assert "unknown site" in find_actions(cards, site="nope.test")["error"]


# ---- over MCP -----------------------------------------------------------------


@pytest.fixture
def generic_url(tmp_path: Path, fixture_card: dict[str, Any]) -> Iterator[str]:
    registry = Registry(tmp_path / "g.db")
    registry.save(fixture_card)
    gateway = Gateway(registry, Executor(InMemorySessionStore(), Confirmer("s")), poll_interval_s=0.05)
    with LiveServer(gateway.asgi_app(API_KEY)) as server:
        yield f"{server.url}/mcp"
    registry.close()


async def test_generic_tool_list(generic_url: str) -> None:
    async with connect(generic_url, "auto") as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"find_actions", "run_action"} <= names
        assert not any(n.startswith("shop_") for n in names)


async def test_find_then_run(generic_url: str, site_url: str) -> None:
    async with connect(generic_url, "auto") as client:
        found = envelope(await client.call_tool("find_actions", {"query": "search products"}))
        top = found["actions"][0]
        env = envelope(
            await client.call_tool(
                "run_action", {"site": top["site"], "action": top["action"], "params": {"query": "lamp"}}
            )
        )
        assert env["status"] == "SUCCESS" and env["data"][0]["name"] == "Desk lamp"
        assert env["next_params"] == {"query": "lamp", "page": 2}


async def test_confirmation_through_run_action(generic_url: str, site_url: str) -> None:
    async with connect(generic_url, "auto") as client:
        args = {"site": DOMAIN, "action": "add_to_cart", "params": {"sku": "SKU-004"}}
        first = envelope(await client.call_tool("run_action", args))
        assert first["status"] == "NEEDS_CONFIRMATION"
        done = envelope(await client.call_tool("run_action", {**args, "confirmation_id": first["confirmation"]["id"]}))
        assert done["status"] == "SUCCESS"
        bad = envelope(await client.call_tool("run_action", {**args, "confirmation_id": "c_forged"}))
        assert bad["status"] == "BAD_PARAMS"


async def test_sign_in_needed_through_run_action(generic_url: str) -> None:
    async with connect(generic_url, "auto") as client:
        env = envelope(await client.call_tool("run_action", {"site": DOMAIN, "action": "list_orders"}))
        assert env["status"] == "AUTH_REQUIRED"


async def test_run_action_errors(generic_url: str) -> None:
    async with connect(generic_url, "auto") as client:
        unknown_site = await client.call_tool("run_action", {"site": "nope.test", "action": "x"})
        assert unknown_site.is_error and "find_actions" in unknown_site.content[0].text  # type: ignore[union-attr]
        unknown_action = await client.call_tool("run_action", {"site": DOMAIN, "action": "nope"})
        assert unknown_action.is_error
        bad_params = await client.call_tool("run_action", {"site": DOMAIN, "action": "search_products", "params": "x"})
        assert bad_params.is_error
        missing = envelope(await client.call_tool("run_action", {"site": DOMAIN, "action": "search_products"}))
        assert missing["status"] == "BAD_PARAMS"  # params default to {}, and query is required
