"""Sign-in plumbing without Docker: encrypted sessions, links, disconnect, warm profiles and the browser tier.
The real login browser (Neko) is covered end to end in tests/test_neko.py."""

import asyncio
import json
import os
import sqlite3
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import async_playwright

from runtime.browser.pool import BrowserConfig, BrowserPool
from runtime.browser.tier import BrowserTier
from runtime.confirm import Confirmer
from runtime.connect import ConnectionManager
from runtime.executor import Executor
from runtime.gateway import Gateway
from runtime.registry import Registry, StoredCard
from runtime.session_store import EncryptedSessionStore, new_session_key
from runtime.sessions import InMemorySessionStore
from runtime.tiers import ProfileKey, default_tiers
from schema import ResultCode, load_card
from tests.conftest import LiveServer
from tests.fixture_site.app import CSRF, VALID_SID
from tests.test_gateway import API_KEY, connect, envelope

R = ResultCode
DOMAIN = "fixture.test"
SERVER_CHANNEL = os.environ.get("BROWSER_CHANNEL", "chrome")


async def _can_launch(channel: str) -> bool:
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel=channel, headless=True)
            await browser.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def chrome() -> None:
    if not asyncio.run(_can_launch(SERVER_CHANNEL)):
        pytest.skip(f"browser channel {SERVER_CHANNEL!r} is not available")


@pytest.fixture(scope="session")
def chromium() -> None:
    if not asyncio.run(_can_launch("chromium")):
        pytest.skip("Playwright's Chromium is not installed (uv run playwright install chromium)")


# ---- pieces that need no browser -------------------------------------------


def test_session_store_encrypts(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    store = EncryptedSessionStore(db, new_session_key())
    store.put("u", DOMAIN, "shop", {"sid": "secret-sid-value", "csrftoken": "c"})
    assert store.get("u", DOMAIN, "shop") == {"sid": "secret-sid-value", "csrftoken": "c"}
    raw = sqlite3.connect(db).execute("SELECT cookies_enc FROM sessions").fetchone()[0]
    assert b"secret-sid-value" not in raw
    record = store.record("u", DOMAIN, "shop")
    assert record is not None and record.cookie_names == ["csrftoken", "sid"] and record.status == "connected"
    assert EncryptedSessionStore(db, new_session_key()).get("u", DOMAIN, "shop") is None  # wrong key
    with pytest.raises(ValueError, match="SESSION_KEY"):
        EncryptedSessionStore(db, "")


@pytest.fixture
def manager(tmp_path: Path, fixture_card: dict[str, Any]) -> Iterator[ConnectionManager]:
    db = tmp_path / "m.db"
    registry = Registry(db)
    registry.save(fixture_card)
    store = EncryptedSessionStore(db, new_session_key())
    yield ConnectionManager(registry, store, Executor(store, Confirmer("s")), "http://gw")
    registry.close()


async def test_links_expire(manager: ConnectionManager) -> None:
    manager.timeout_s = -1
    started = manager.start("u", DOMAIN)
    assert started["status"] == "expired" and "url" not in started


async def test_keepalive_marks_expired_sessions(manager: ConnectionManager, site_url: str) -> None:
    manager.store.put("good", DOMAIN, "shop", {"sid": VALID_SID})
    manager.store.put("bad", DOMAIN, "shop", {"sid": "sid-revoked"})
    results = await manager.keepalive_once()
    assert results == {"bad/fixture.test/shop": "expired", "good/fixture.test/shop": "ok"}
    assert manager.status("bad", DOMAIN)["providers"][0]["status"] == "expired"


# ---- server-side browser pool and tier --------------------------------------


@pytest.fixture
async def pool(tmp_path: Path, chrome: None) -> AsyncIterator[BrowserPool]:
    p = BrowserPool(BrowserConfig(profiles_dir=tmp_path / "profiles", channel=SERVER_CHANNEL, idle_s=0))
    yield p
    await p.close()


async def test_pool_is_warm_and_reaps_idle(pool: BrowserPool) -> None:
    key = ProfileKey("u", DOMAIN, None)
    async with pool.lease(key) as ctx:
        ua = await (await ctx.new_page()).evaluate("navigator.userAgent")
        assert "HeadlessChrome" not in ua and "Chrome/" in ua
        assert await pool.close_idle() == []  # leased: never reaped
    assert pool.is_warm(key)
    assert await pool.close_idle() == [key]
    assert not pool.is_warm(key) and pool.profile_dir(key).exists()


async def test_browser_tier_runs_fetch_in_page(pool: BrowserPool, fixture_card: dict[str, Any], site_url: str) -> None:
    tiers = default_tiers()
    tiers["browser"] = BrowserTier(pool)
    ex = Executor(InMemorySessionStore(), Confirmer("s"), tiers=tiers)
    stored = StoredCard(DOMAIN, 1, load_card(fixture_card), True, "now", None)
    env = await ex.run(stored, "browser_tier_search", {"query": "lamp"})
    assert env.status == R.SUCCESS and env.meta.tier == "browser"
    env = await ex.run(stored, "challenge", {})
    assert env.status == R.BLOCKED and env.error is not None
    assert "tiers tried: http, impersonate, browser" in env.error.message


# ---- the connect page and gateway boundaries ----------------------------------


@pytest.fixture
def login_gateway(
    tmp_path: Path, fixture_card: dict[str, Any], chrome: None
) -> Iterator[tuple[str, Path, EncryptedSessionStore]]:
    db = tmp_path / "gw.db"
    registry = Registry(db)
    registry.save(fixture_card)
    store = EncryptedSessionStore(db, new_session_key())
    pool = BrowserPool(BrowserConfig(profiles_dir=tmp_path / "profiles", channel=SERVER_CHANNEL))
    tiers = default_tiers()
    tiers["browser"] = BrowserTier(pool)
    executor = Executor(store, Confirmer("s"), tiers=tiers)
    manager = ConnectionManager(registry, store, executor, "http://placeholder", pool=pool)
    gateway = Gateway(registry, executor, poll_interval_s=0.05, connections=manager, pool=pool)
    with LiveServer(gateway.asgi_app(API_KEY)) as server:
        manager.public_base_url = server.url
        yield server.url, db, store
    registry.close()


async def test_connect_page_offers_log_in(
    login_gateway: tuple[str, Path, EncryptedSessionStore], chromium: None
) -> None:
    base, _, _ = login_gateway
    async with connect(f"{base}/mcp", "auto") as client:
        link = envelope(await client.call_tool("start_connection", {"domain": DOMAIN}))["url"]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chromium", headless=True)
        page = await browser.new_page()
        await page.goto(link)
        assert await page.is_visible("#browser") and await page.is_enabled("#browser")
        assert await page.locator("#connect").count() == 0  # no extension path any more
        assert await page.is_hidden("#done-panel")
        await browser.close()


async def test_link_routes_and_auth_boundaries(login_gateway: tuple[str, Path, EncryptedSessionStore]) -> None:
    base, _, _ = login_gateway
    assert httpx.get(f"{base}/connect/not-a-token").status_code == 404
    assert httpx.get(f"{base}/connect/not-a-token/status").status_code == 404
    assert httpx.post(f"{base}/connect/not-a-token/done").json()["error"] == "unknown login link"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert httpx.post(f"{base}/mcp", json=body).status_code == 401  # MCP still needs the Bearer key
    async with connect(f"{base}/mcp", "auto") as client:
        assert (await client.call_tool("start_connection", {"domain": "nope.test"})).is_error
        link = envelope(await client.call_tool("start_connection", {"domain": DOMAIN}))["url"]
    page = httpx.get(link)
    assert page.status_code == 200 and "Sign in to fixture.test" in page.text
    assert "extension" not in page.text.lower()
    token = link.rsplit("/", 1)[1]
    assert httpx.get(f"{base}/neko/{token}/").status_code == 404  # no login browser started
    assert httpx.post(f"{link}/browser").status_code == 400  # this gateway has no Neko
    assert httpx.get(link + "/status").json()["status"] == "pending"


# ---- disconnect -------------------------------------------------------------


async def test_disconnect_forgets_session_and_cancels_link(manager: ConnectionManager, site_url: str) -> None:
    manager.store.put("u", DOMAIN, "shop", {"sid": VALID_SID, "csrftoken": CSRF})
    started = manager.start("u", DOMAIN)
    assert started["user_message"].startswith("**[Sign in to fixture.test →](http://gw/connect/")
    assert manager.status("u", DOMAIN)["user_message"] == "Waiting for you to finish signing in to fixture.test."
    out = await manager.disconnect("u", DOMAIN)
    assert out["disconnected"] is True
    assert out["user_message"] == "You're signed out of fixture.test. To use it again, you'll need to sign in."
    assert out["details"]["providers"] == [
        {"provider": "shop", "was_connected": True, "session_deleted": True, "browser_profile_deleted": False}
    ]
    assert manager.store.get("u", DOMAIN, "shop") is None
    assert manager.status("u", DOMAIN)["providers"][0]["status"] == "not_connected"
    assert manager.status("u", DOMAIN)["user_message"] == "You're not signed in to fixture.test."
    assert (await manager.poll("u", DOMAIN))["user_message"].startswith("That sign-in link for fixture.test has expired")
    conn = manager.by_token(started["url"].rsplit("/", 1)[1])
    assert conn is not None and conn.status == "expired" and "disconnected" in conn.message
    stored = manager.registry.get(DOMAIN)
    assert stored is not None
    env = await manager.executor.run(stored, "list_orders", {}, user="u")
    assert env.status == R.AUTH_REQUIRED
    again = await manager.disconnect("u", DOMAIN)  # idempotent
    assert again["details"]["providers"][0]["was_connected"] is False
    assert again["user_message"] == "You weren't signed in to fixture.test, so there was nothing to sign out of."


async def test_disconnect_errors(manager: ConnectionManager) -> None:
    assert "unknown domain" in (await manager.disconnect("u", "nope.test"))["error"]
    assert "no provider" in (await manager.disconnect("u", DOMAIN, "github"))["error"]


async def test_pool_delete_profile(pool: BrowserPool) -> None:
    key = ProfileKey("u", DOMAIN, "shop")
    async with pool.lease(key):
        pass
    assert pool.is_warm(key) and pool.profile_dir(key).exists()
    assert await pool.delete_profile(key) is True
    assert not pool.is_warm(key) and not pool.profile_dir(key).exists()
    assert await pool.delete_profile(key) is False


async def test_disconnect_over_mcp(
    login_gateway: tuple[str, Path, EncryptedSessionStore], site_url: str
) -> None:
    base, db, store = login_gateway
    async with connect(f"{base}/mcp", "auto") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert tools["disconnect"].annotations is not None and tools["disconnect"].annotations.destructive_hint
        assert "browser profile" not in (tools["disconnect"].description or "")  # no internals in descriptions
        store_rows = sqlite3.connect(db).execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        assert store_rows == 0
        envelope(await client.call_tool("start_connection", {"domain": DOMAIN}))  # a pending link to cancel
        store.put("default", DOMAIN, "shop", {"sid": VALID_SID, "csrftoken": CSRF})  # as if signed in
        assert envelope(await client.call_tool("shop_browser_orders", {}))["status"] == "SUCCESS"  # warms the profile

        out = envelope(await client.call_tool("disconnect", {"domain": DOMAIN}))
        assert out["details"]["providers"][0]["was_connected"] is True
        assert out["details"]["providers"][0]["browser_profile_deleted"] is True
        assert out["user_message"].startswith("You're signed out of fixture.test.")
        assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert envelope(await client.call_tool("shop_list_orders", {}))["status"] == "AUTH_REQUIRED"
        status = envelope(await client.call_tool("connection_status", {"domain": DOMAIN}))
        assert status["providers"][0]["status"] == "not_connected" and not status["providers"][0]["browser_warm"]
