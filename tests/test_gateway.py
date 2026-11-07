"""End-to-end: real MCP clients over Streamable HTTP -> gateway -> executor -> fixture site."""

import copy
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx
import httpx2
import mcp_types as types
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from runtime.confirm import Confirmer
from runtime.executor import Executor
from runtime.gateway import Gateway
from runtime.registry import Registry
from runtime.sessions import InMemorySessionStore
from tests.conftest import LiveServer

API_KEY = "test-key"


@pytest.fixture
def registry(tmp_path: Path, fixture_card: dict[str, Any]) -> Iterator[Registry]:
    reg = Registry(tmp_path / "gw.db")
    reg.save(fixture_card)
    yield reg
    reg.close()


@pytest.fixture
def gateway(registry: Registry) -> Gateway:
    return Gateway(registry, Executor(InMemorySessionStore(), Confirmer("s")), poll_interval_s=0.05)


@pytest.fixture
def mcp_url(gateway: Gateway) -> Iterator[str]:
    with LiveServer(gateway.asgi_app(API_KEY)) as server:
        yield f"{server.url}/mcp"


@asynccontextmanager
async def connect(url: str, mode: str, message_handler: Any = None) -> AsyncIterator[Client]:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {API_KEY}"}, timeout=30)
    async with http:
        transport = streamable_http_client(url, http_client=http)
        async with Client(transport, mode=mode, message_handler=message_handler, cache=None) as client:  # type: ignore[arg-type]
            yield client


def envelope(result: types.CallToolResult) -> dict[str, Any]:
    first = result.content[0]
    assert isinstance(first, types.TextContent)
    data: dict[str, Any] = json.loads(first.text)
    assert result.structured_content == data
    return data


def test_requires_bearer_key(mcp_url: str) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert httpx.post(mcp_url, json=body).status_code == 401
    bad = httpx.post(mcp_url, json=body, headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401 and bad.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_list_and_call(mcp_url: str, mode: str) -> None:
    async with connect(mcp_url, mode) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert "shop_search_products" in tools and "shop_add_to_cart" in tools
        search = tools["shop_search_products"]
        assert search.description and search.description.startswith("Search the shop's products")
        assert search.input_schema["required"] == ["query"]
        assert search.input_schema["properties"]["page"]["default"] == 1
        cart = tools["shop_add_to_cart"]
        assert "_confirmation_id" in cart.input_schema["properties"]
        assert cart.description and "explicit approval" in cart.description
        assert "explicitly approved" in cart.input_schema["properties"]["_confirmation_id"]["description"]

        env = envelope(await client.call_tool("shop_search_products", {"query": "lamp"}))
        assert env["status"] == "SUCCESS"
        assert env["data"][0]["name"] == "Desk lamp"
        assert env["next_params"] == {"query": "lamp", "page": 2}
        assert env["heal"] is None and env["meta"]["map_version"] == 1


async def test_every_code_reachable_over_mcp(mcp_url: str, site_url: str) -> None:
    calls: list[tuple[str, dict[str, Any], str]] = [
        ("shop_search_products", {"query": "lamp"}, "SUCCESS"),
        ("shop_search_products", {"query": "zzz"}, "EMPTY"),
        ("shop_search_products", {}, "BAD_PARAMS"),
        ("shop_list_orders", {}, "AUTH_REQUIRED"),
        ("shop_add_to_cart", {"sku": "SKU-001"}, "NEEDS_CONFIRMATION"),
        ("shop_rate_limited", {}, "RATE_LIMITED"),
        ("shop_challenge", {}, "BLOCKED"),
        ("shop_flaky_status", {"key": "mcp", "fail": 5}, "ERROR"),
        ("shop_changed_structure", {}, "PAGE_CHANGED"),
    ]
    async with connect(mcp_url, "auto") as client:
        for tool, args, expected in calls:
            env = envelope(await client.call_tool(tool, args))
            assert env["status"] == expected, (tool, env)

        # Writes: confirm, then the three write outcomes.
        for sku, expected in [("SKU-002", "SUCCESS"), ("FAIL-1", "UNKNOWN_OUTCOME"), ("GHOST-1", "VERIFY_FAILED")]:
            first = envelope(await client.call_tool("shop_add_to_cart", {"sku": sku}))
            conf_id = first["confirmation"]["id"]
            env = envelope(await client.call_tool("shop_add_to_cart", {"sku": sku, "_confirmation_id": conf_id}))
            assert env["status"] == expected, (sku, env)

        # AUTH_EXPIRED needs a session; the gateway uses the executor's store.
        httpx.post(f"{site_url}/_admin/break")
        env = envelope(await client.call_tool("shop_get_item", {"id": "p1"}))
        assert env["status"] == "PAGE_CHANGED" and "same HTTP 404" in env["error"]["message"]


async def test_auth_expired_over_mcp(gateway: Gateway, mcp_url: str) -> None:
    store = gateway.executor.sessions
    assert isinstance(store, InMemorySessionStore)
    store.put("default", "fixture.test", "shop", {"sid": "sid-revoked", "csrftoken": "x"})
    async with connect(mcp_url, "auto") as client:
        env = envelope(await client.call_tool("shop_list_orders", {}))
        assert env["status"] == "AUTH_EXPIRED"


async def test_unknown_tool(mcp_url: str) -> None:
    async with connect(mcp_url, "auto") as client:
        result = await client.call_tool("shop_nope", {})
        assert result.is_error


def changed_card(registry: Registry) -> dict[str, Any]:
    stored = registry.get("fixture.test")
    assert stored is not None
    card = copy.deepcopy(stored.card.to_json_dict())
    new_action = copy.deepcopy(card["actions"][0])
    new_action["name"] = "find_products"
    new_action["description"] = "Find products (renamed)."
    card["actions"].append(new_action)
    return card


async def test_card_change_notifies_legacy_client(registry: Registry, mcp_url: str) -> None:
    got = anyio.Event()

    async def on_message(message: Any) -> None:
        if isinstance(message, types.ToolListChangedNotification) or (
            getattr(message, "method", None) == "notifications/tools/list_changed"
        ):
            got.set()

    async with connect(mcp_url, "legacy", message_handler=on_message) as client:
        before = {t.name for t in (await client.list_tools()).tools}
        assert "shop_find_products" not in before
        registry.save(changed_card(registry))
        with anyio.fail_after(5):
            await got.wait()
        after = {t.name for t in (await client.list_tools()).tools}
        assert "shop_find_products" in after


async def test_card_change_notifies_modern_client(registry: Registry, mcp_url: str, tmp_path: Path) -> None:
    async with connect(mcp_url, "auto") as client:
        assert client.session.protocol_version >= "2026"
        async with client.listen(tools_list_changed=True) as sub:
            # Simulate the mapper writing from another process.
            other = Registry(tmp_path / "gw.db")
            other.save(changed_card(other))
            other.close()
            with anyio.fail_after(5):
                event = await sub.__anext__()
            assert type(event).__name__ == "ToolsListChanged"
        tools = {t.name for t in (await client.list_tools()).tools}
        assert "shop_find_products" in tools


async def test_rollback_notifies_and_restores(registry: Registry, gateway: Gateway) -> None:
    registry.save(changed_card(registry))
    assert await gateway.reload_if_changed()
    assert "shop_find_products" in gateway.tool_names()
    registry.rollback("fixture.test", 1)
    assert await gateway.reload_if_changed()
    assert "shop_find_products" not in gateway.tool_names()
    assert not await gateway.reload_if_changed()


async def test_break_and_heal_over_mcp(registry: Registry, tmp_path: Path, site_url: str) -> None:
    from runtime.healing import HealConfig, HealManager
    from runtime.state import StateStore
    from tests.fixture_site.remapper import FixtureRemapper

    executor = Executor(InMemorySessionStore(), Confirmer("s"))
    healer = HealManager(registry, StateStore(tmp_path / "gw.db"), executor, FixtureRemapper(), HealConfig(poll_s=0.05))
    gw = Gateway(registry, executor, poll_interval_s=0.05, healer=healer)
    with LiveServer(gw.asgi_app(API_KEY)) as server:
        async with connect(f"{server.url}/mcp", "auto") as client:
            assert "heal_status" in {t.name for t in (await client.list_tools()).tools}
            httpx.post(f"{site_url}/_admin/break")
            env = envelope(await client.call_tool("shop_get_item", {"id": "p1"}))
            assert env["status"] == "PAGE_CHANGED" and env["heal"]["retry_after_s"] == 30
            job_id = env["heal"]["job_id"]
            with anyio.fail_after(10):
                while True:
                    status = envelope(await client.call_tool("heal_status", {"job_id": job_id}))
                    if status["status"] not in ("queued", "running"):
                        break
                    await anyio.sleep(0.05)
            assert status["status"] == "succeeded" and status["new_version"] == 2
            env = envelope(await client.call_tool("shop_get_item", {"id": "p1"}))
            assert env["status"] == "SUCCESS" and env["meta"]["map_version"] == 2
            missing = await client.call_tool("heal_status", {"job_id": "h_nope"})
            assert missing.is_error


def test_instructions_ask_for_plain_language() -> None:
    from runtime.gateway import INSTRUCTIONS

    assert "plain terms" in INSTRUCTIONS and "user_message" in INSTRUCTIONS
    assert "Don't mention tool names, status codes, sessions, cookies, browser profiles" in INSTRUCTIONS
