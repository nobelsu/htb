"""Login by extension capture, encrypted sessions, warm profiles and the browser tier.

The end-to-end tests load the real extension (/extension) into Playwright's Chromium as "the user's browser"
(branded Chrome no longer allows loading unpacked extensions from automation).
"""

import asyncio
import json
import os
import shutil
import sqlite3
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import BrowserContext, Page, async_playwright

from runtime.browser.pool import BrowserConfig, BrowserPool
from runtime.browser.tier import BrowserTier
from runtime.confirm import Confirmer
from runtime.connect import ConnectionManager
from runtime.executor import Executor
from runtime.extension import EXTENSION_DIR, add_origin, extension_id, manifest_extension_id
from runtime.gateway import Gateway
from runtime.registry import Registry, StoredCard
from runtime.session_store import EncryptedSessionStore, new_session_key
from runtime.sessions import InMemorySessionStore
from runtime.tiers import ProfileKey, default_tiers
from schema import ResultCode, load_card
from tests.conftest import LiveServer
from tests.fixture_site.app import CSRF, LOGIN_PASSWORD, LOGIN_USER, VALID_SID
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


def test_extension_id_and_origins(tmp_path: Path) -> None:
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text())
    assert manifest_extension_id() == extension_id(manifest["key"])
    assert len(manifest_extension_id()) == 32 and set(manifest_extension_id()) <= set("abcdefghijklmnop")
    copy = tmp_path / "manifest.json"
    copy.write_text(json.dumps(manifest))
    allowed = add_origin("https://gw.example.com:8443", copy)
    assert "https://gw.example.com/*" in allowed
    assert "https://gw.example.com/*" in json.loads(copy.read_text())["host_permissions"]
    with pytest.raises(ValueError):
        add_origin("gw.example.com", copy)


@pytest.fixture
def manager(tmp_path: Path, fixture_card: dict[str, Any]) -> Iterator[ConnectionManager]:
    db = tmp_path / "m.db"
    registry = Registry(db)
    registry.save(fixture_card)
    store = EncryptedSessionStore(db, new_session_key())
    yield ConnectionManager(registry, store, Executor(store, Confirmer("s")), "http://gw")
    registry.close()


async def test_import_verifies_before_storing(manager: ConnectionManager, site_url: str) -> None:
    from runtime.connect import CookieImportError

    started = manager.start("u", DOMAIN)
    token = started["url"].rsplit("/", 1)[1]
    assert manager.start("u", DOMAIN)["url"] == started["url"]  # one pending link at a time
    conn = manager.by_token(token)
    assert conn is not None and conn.cookie_origins(DOMAIN) == ["http://127.0.0.1/*"]

    with pytest.raises(CookieImportError, match="not logged in"):
        await manager.import_cookies(token, [{"name": "sid", "value": "sid-revoked"}])
    assert manager.poll("u", DOMAIN)["status"] == "pending"  # can retry
    with pytest.raises(CookieImportError, match="no cookies"):
        await manager.import_cookies(token, [])
    with pytest.raises(CookieImportError):
        await manager.import_cookies(token, "nope")

    await manager.import_cookies(token, [{"name": "sid", "value": VALID_SID}, {"name": "csrftoken", "value": CSRF}])
    polled = manager.poll("u", DOMAIN)
    assert polled["status"] == "connected" and "url" not in polled
    assert manager.store.get("u", DOMAIN, "shop") == {"sid": VALID_SID, "csrftoken": CSRF}
    with pytest.raises(CookieImportError, match="connected"):
        await manager.import_cookies(token, [{"name": "sid", "value": VALID_SID}])  # links are single-use


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


# ---- end to end: the user's browser with the extension ----------------------


@pytest.fixture
def login_gateway(tmp_path: Path, fixture_card: dict[str, Any], chrome: None) -> Iterator[tuple[str, Path]]:
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
        yield server.url, db
    registry.close()


@pytest.fixture
async def user_browser(tmp_path: Path, chromium: None) -> AsyncIterator[BrowserContext]:
    ext = tmp_path / "extension"
    shutil.copytree(EXTENSION_DIR, ext)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(tmp_path / "user-profile"),
            channel="chromium",
            headless=True,
            args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"],
        )
        worker = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker")
        assert worker.url.startswith(f"chrome-extension://{manifest_extension_id()}/")  # the pinned ID
        yield ctx
        await ctx.close()


async def log_in_normally(page: Page, site_url: str) -> None:
    await page.goto(f"{site_url}/login")
    await page.fill("#username", LOGIN_USER)
    await page.fill("#password", LOGIN_PASSWORD)
    await page.click("#submit")
    await page.wait_for_url("**/account")


async def click_connect(ctx: BrowserContext, link: str) -> tuple[Page, Page]:
    page = await ctx.new_page()
    await page.goto(link)
    await page.wait_for_selector("#connect:enabled", timeout=10_000)  # the extension answered the ping
    async with ctx.expect_page() as approve_info:
        await page.click("#connect")
    approve = await approve_info.value
    await approve.wait_for_load_state()
    assert await approve.inner_text("#domain") == DOMAIN
    await approve.click("#allow")
    return page, approve


async def test_connect_with_extension_over_mcp(
    login_gateway: tuple[str, Path], user_browser: BrowserContext, site_url: str
) -> None:
    base, db = login_gateway
    async with connect(f"{base}/mcp", "auto") as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert {"start_connection", "poll_connection", "connection_status"} <= tools
        env = envelope(await client.call_tool("shop_list_orders", {}))
        assert env["status"] == "AUTH_REQUIRED" and "start_connection" in env["error"]["hint"]

        started = envelope(await client.call_tool("start_connection", {"domain": DOMAIN}))
        link = started["url"]
        assert started["status"] == "pending" and link.startswith(f"{base}/connect/")
        assert "extension" in started["instructions"]

        # The user logs in to the shop in their own browser, as usual, then connects.
        await log_in_normally(await user_browser.new_page(), site_url)
        page, approve = await click_connect(user_browser, link)
        await approve.wait_for_selector("#result.ok", timeout=15_000)
        await page.wait_for_selector("#status.connected", timeout=10_000)

        polled = envelope(await client.call_tool("poll_connection", {"domain": DOMAIN}))
        assert polled["status"] == "connected"
        status = envelope(await client.call_tool("connection_status", {"domain": DOMAIN}))
        assert status["providers"][0]["status"] == "connected"

        env = envelope(await client.call_tool("shop_list_orders", {}))  # http tier, encrypted cookie copy
        assert env["status"] == "SUCCESS" and env["meta"]["tier"] == "http"
        env = envelope(await client.call_tool("shop_browser_orders", {}))  # server Chrome, seeded with the cookies
        assert env["status"] == "SUCCESS" and env["meta"]["tier"] == "browser", env

    raw = sqlite3.connect(db).execute("SELECT cookies_enc FROM sessions").fetchone()[0]
    assert VALID_SID.encode() not in raw and CSRF.encode() not in raw


async def test_connect_while_logged_out_is_refused(
    login_gateway: tuple[str, Path], user_browser: BrowserContext, site_url: str
) -> None:
    base, _ = login_gateway
    async with connect(f"{base}/mcp", "auto") as client:
        link = envelope(await client.call_tool("start_connection", {"domain": DOMAIN}))["url"]
        page, approve = await click_connect(user_browser, link)  # never logged in to the shop
        await approve.wait_for_selector("#result.bad", timeout=15_000)
        assert "log in to fixture.test" in (await approve.inner_text("#result"))
        assert envelope(await client.call_tool("poll_connection", {"domain": DOMAIN}))["status"] == "pending"


async def test_page_without_extension_explains(login_gateway: tuple[str, Path], chromium: None) -> None:
    base, _ = login_gateway
    async with connect(f"{base}/mcp", "auto") as client:
        link = envelope(await client.call_tool("start_connection", {"domain": DOMAIN}))["url"]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chromium", headless=True)
        page = await browser.new_page()
        await page.goto(link)
        await page.wait_for_selector("#ext-missing:not([hidden])", timeout=10_000)
        assert await page.is_disabled("#connect")
        await browser.close()


async def test_link_routes_and_auth_boundaries(login_gateway: tuple[str, Path]) -> None:
    base, _ = login_gateway
    assert httpx.get(f"{base}/connect/not-a-token").status_code == 404
    assert httpx.get(f"{base}/connect/not-a-token/status").status_code == 404
    bad = httpx.post(f"{base}/connect/not-a-token/cookies", json={"cookies": []})
    assert bad.status_code == 400 and bad.json()["error"] == "unknown login link"
    assert httpx.post(f"{base}/connect/x/cookies", content=b"not json").status_code == 400
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert httpx.post(f"{base}/mcp", json=body).status_code == 401  # MCP still needs the Bearer key
    async with connect(f"{base}/mcp", "auto") as client:
        assert (await client.call_tool("start_connection", {"domain": "nope.test"})).is_error
        link = envelope(await client.call_tool("start_connection", {"domain": DOMAIN}))["url"]
    page = httpx.get(link)
    assert page.status_code == 200 and "Connect fixture.test" in page.text
    assert manifest_extension_id() in page.text
    assert httpx.get(link + "/status").json()["status"] == "pending"
