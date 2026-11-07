"""Browser worker, encrypted sessions and the streamed login flow, end to end with real (headless) Chrome."""

import asyncio
import json
import os
import sqlite3
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
import websockets

from runtime.browser.connect import ConnectionManager
from runtime.browser.pool import BrowserConfig, BrowserPool
from runtime.browser.tier import BrowserTier
from runtime.confirm import Confirmer
from runtime.executor import Executor
from runtime.gateway import Gateway
from runtime.registry import Registry
from runtime.session_store import EncryptedSessionStore, new_session_key
from runtime.sessions import InMemorySessionStore
from runtime.tiers import ProfileKey, default_tiers
from schema import ResultCode
from tests.conftest import LiveServer
from tests.fixture_site.app import CSRF, LOGIN_PASSWORD, LOGIN_USER, VALID_SID
from tests.test_gateway import API_KEY, connect, envelope

R = ResultCode
DOMAIN = "fixture.test"
CHANNEL = os.environ.get("BROWSER_CHANNEL", "chrome")


async def _chrome_available() -> bool:
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel=CHANNEL, headless=True)
            await browser.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def chrome() -> None:
    if not asyncio.run(_chrome_available()):
        pytest.skip(f"browser channel {CHANNEL!r} is not available")


# ---- encrypted session store (no browser needed) ---------------------------


def test_session_store_encrypts(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    key = new_session_key()
    store = EncryptedSessionStore(db, key)
    store.put("u", DOMAIN, "shop", {"sid": "secret-sid-value", "csrftoken": "c"})
    assert store.get("u", DOMAIN, "shop") == {"sid": "secret-sid-value", "csrftoken": "c"}
    raw = sqlite3.connect(db).execute("SELECT cookies_enc FROM sessions").fetchone()[0]
    assert b"secret-sid-value" not in raw
    record = store.record("u", DOMAIN, "shop")
    assert record is not None and record.cookie_names == ["csrftoken", "sid"] and record.status == "connected"
    assert EncryptedSessionStore(db, new_session_key()).get("u", DOMAIN, "shop") is None  # wrong key
    with pytest.raises(ValueError, match="SESSION_KEY"):
        EncryptedSessionStore(db, "")


async def test_keepalive_marks_expired_sessions(tmp_path: Path, fixture_card: dict[str, Any]) -> None:
    db = tmp_path / "k.db"
    registry = Registry(db)
    registry.save(fixture_card)
    store = EncryptedSessionStore(db, new_session_key())
    executor = Executor(store, Confirmer("s"))
    pool = BrowserPool(BrowserConfig(profiles_dir=tmp_path / "profiles"))  # never started
    manager = ConnectionManager(registry, pool, store, executor, "http://gw")
    store.put("good", DOMAIN, "shop", {"sid": VALID_SID})
    store.put("bad", DOMAIN, "shop", {"sid": "sid-revoked"})
    results = await manager.keepalive_once()
    assert results == {"bad/fixture.test/shop": "expired", "good/fixture.test/shop": "ok"}
    bad = store.record("bad", DOMAIN, "shop")
    assert bad is not None and bad.status == "expired"
    assert manager.status("bad", DOMAIN)["providers"][0]["status"] == "expired"
    registry.close()


# ---- browser pool and tier -------------------------------------------------


@pytest.fixture
async def pool(tmp_path: Path, chrome: None) -> AsyncIterator[BrowserPool]:
    p = BrowserPool(BrowserConfig(profiles_dir=tmp_path / "profiles", channel=CHANNEL, idle_s=0))
    yield p
    await p.close()


async def test_pool_is_warm_and_reaps_idle(pool: BrowserPool) -> None:
    key = ProfileKey("u", DOMAIN, None)
    async with pool.lease(key) as ctx:
        ua = await (await ctx.new_page()).evaluate("navigator.userAgent")
        assert "HeadlessChrome" not in ua and "Chrome/" in ua
        assert pool.is_warm(key)
        assert await pool.close_idle() == []  # leased: never reaped
    assert pool.is_warm(key)  # stays warm after use
    assert await pool.close_idle() == [key]
    assert not pool.is_warm(key)
    assert pool.profile_dir(key).exists()  # the profile (the session) survives on disk


async def test_browser_tier_runs_fetch_in_page(
    pool: BrowserPool, fixture_card: dict[str, Any], tmp_path: Path, site_url: str
) -> None:
    from schema import load_card
    from runtime.registry import StoredCard

    tiers = default_tiers()
    tiers["browser"] = BrowserTier(pool)
    ex = Executor(InMemorySessionStore(), Confirmer("s"), tiers=tiers)
    stored = StoredCard(DOMAIN, 1, load_card(fixture_card), True, "now", None)
    env = await ex.run(stored, "browser_tier_search", {"query": "lamp"})
    assert env.status == R.SUCCESS and env.meta.tier == "browser"
    assert env.data is not None and env.data[0]["name"] == "Desk lamp"
    # The ladder now tops out at the browser tier.
    env = await ex.run(stored, "challenge", {})
    assert env.status == R.BLOCKED and env.meta.tier == "browser"
    assert env.error is not None and "tiers tried: http, impersonate, browser" in env.error.message


# ---- the whole login flow over MCP -----------------------------------------


@pytest.fixture
def login_gateway(tmp_path: Path, fixture_card: dict[str, Any], chrome: None) -> Iterator[tuple[str, Path]]:
    db = tmp_path / "gw.db"
    registry = Registry(db)
    registry.save(fixture_card)
    store = EncryptedSessionStore(db, new_session_key())
    pool = BrowserPool(BrowserConfig(profiles_dir=tmp_path / "profiles", channel=CHANNEL))
    tiers = default_tiers()
    tiers["browser"] = BrowserTier(pool)
    executor = Executor(store, Confirmer("s"), tiers=tiers)
    manager = ConnectionManager(registry, pool, store, executor, "http://placeholder", check_every_s=0.3)
    gateway = Gateway(registry, executor, poll_interval_s=0.05, connections=manager, pool=pool)
    with LiveServer(gateway.asgi_app(API_KEY)) as server:
        manager.public_base_url = server.url
        yield server.url, db
    registry.close()


async def _login_through_stream(link: str) -> list[str]:
    """Act as the user: open the streamed page's socket, focus the form with Tab, type, submit."""
    statuses: list[str] = []
    frames = 0
    async with websockets.connect(link.replace("http://", "ws://") + "/ws") as ws:
        async def recv_until(pred: Any, timeout: float = 20) -> None:
            nonlocal frames
            with anyio.fail_after(timeout):
                while True:
                    msg = json.loads(await ws.recv())
                    if msg["t"] == "frame":
                        frames += 1
                    else:
                        statuses.append(msg["status"])
                    if pred():
                        return

        await recv_until(lambda: frames >= 1)  # the login page is on screen
        await ws.send(json.dumps({"t": "click", "x": 640, "y": 700}))  # click empty page area
        for message in (
            {"t": "key", "key": "Tab"},
            {"t": "text", "text": LOGIN_USER},
            {"t": "key", "key": "Tab"},
            {"t": "text", "text": LOGIN_PASSWORD},
            {"t": "key", "key": "Enter"},
        ):
            await ws.send(json.dumps(message))
        await recv_until(lambda: "connected" in statuses)
    return statuses


async def test_login_flow_over_mcp(login_gateway: tuple[str, Path], site_url: str) -> None:
    base, db = login_gateway
    async with connect(f"{base}/mcp", "auto") as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert {"start_connection", "poll_connection", "connection_status"} <= tools

        env = envelope(await client.call_tool("shop_list_orders", {}))
        assert env["status"] == "AUTH_REQUIRED" and "start_connection" in env["error"]["hint"]

        started = envelope(await client.call_tool("start_connection", {"domain": DOMAIN}))
        assert started["status"] == "pending" and started["provider"] == "shop"
        link = started["url"]
        assert link.startswith(f"{base}/connect/")
        again = envelope(await client.call_tool("start_connection", {"domain": DOMAIN}))
        assert again["url"] == link  # one pending login at a time

        page = httpx.get(link)  # no Bearer header: the token in the link is the credential
        assert page.status_code == 200 and "Log in to fixture.test" in page.text
        assert envelope(await client.call_tool("poll_connection", {"domain": DOMAIN}))["status"] == "pending"

        statuses = await _login_through_stream(link)
        assert statuses[0] == "pending" and statuses[-1] == "connected"

        polled = envelope(await client.call_tool("poll_connection", {"domain": DOMAIN}))
        assert polled["status"] == "connected" and "url" not in polled
        status = envelope(await client.call_tool("connection_status", {"domain": DOMAIN}))
        assert status["providers"][0]["status"] == "connected" and status["providers"][0]["browser_warm"]

        # http tier, from the encrypted cookie copy (x-csrf comes from session.cookies.csrftoken)
        env = envelope(await client.call_tool("shop_list_orders", {}))
        assert env["status"] == "SUCCESS" and env["meta"]["tier"] == "http"
        # browser-only endpoint: http and impersonate are challenged, the logged-in profile gets through
        env = envelope(await client.call_tool("shop_browser_orders", {}))
        assert env["status"] == "SUCCESS" and env["meta"]["tier"] == "browser", env
        assert env["data"] == [{"id": "o1", "total": 30}, {"id": "o2", "total": 12}]

    raw = sqlite3.connect(db).execute("SELECT cookies_enc FROM sessions").fetchone()[0]
    assert VALID_SID.encode() not in raw and CSRF.encode() not in raw


async def test_login_links_and_auth_boundaries(login_gateway: tuple[str, Path]) -> None:
    base, _ = login_gateway
    assert httpx.get(f"{base}/connect/not-a-token").status_code == 404
    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(base.replace("http://", "ws://") + "/mcp"):
            pass  # non-exempt websocket paths are refused
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert httpx.post(f"{base}/mcp", json=body).status_code == 401  # MCP still needs the Bearer key
    async with connect(f"{base}/mcp", "auto") as client:
        result = await client.call_tool("start_connection", {"domain": "nope.test"})
        assert result.is_error
