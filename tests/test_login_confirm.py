"""The connected-too-early guard: when a site's session_check can't tell signed-in from signed-out, the user
confirms with "I've signed in" instead of us auto-connecting. Uses a fake login browser (no Docker needed)."""

import asyncio
import copy
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import async_playwright

from runtime.confirm import Confirmer
from runtime.connect import ConnectionManager, CookieImportError
from runtime.executor import Executor
from runtime.gateway import Gateway
from runtime.neko.manager import NekoConfig
from runtime.registry import Registry
from runtime.session_store import EncryptedSessionStore, new_session_key
from tests.conftest import LiveServer
from tests.fixture_site.app import CSRF, VALID_SID
from tests.test_gateway import API_KEY, connect, envelope

DOMAIN = "fixture.test"
SIGNED_IN = {"sid": VALID_SID, "csrftoken": CSRF}


@dataclass
class FakeSession:
    token: str
    http_port: int = 9  # nothing listens: the viewer iframe just fails to load
    upstream: str = "http://127.0.0.1:9"

    def viewer_path(self) -> str:
        return f"/neko/{self.token}/?embed=1"


@dataclass
class FakeNeko:
    """Stands in for NekoManager: `cookies` is what the login browser currently holds."""

    cookies_now: dict[str, str] = field(default_factory=lambda: {"anon": "1"})
    sessions: dict[str, FakeSession] = field(default_factory=dict)
    config: NekoConfig = field(default_factory=NekoConfig)

    async def available(self) -> bool:
        return True

    async def cleanup_stale(self) -> int:
        return 0

    async def launch(self, token: str, start_url: str, site_urls: list[str]) -> FakeSession:
        return self.sessions.setdefault(token, FakeSession(token))

    def get(self, token: str) -> FakeSession | None:
        return self.sessions.get(token)

    async def cookies(self, token: str, base_url: str) -> dict[str, str]:
        return dict(self.cookies_now)

    async def stop(self, token: str) -> None:
        self.sessions.pop(token, None)

    async def close(self) -> None:
        self.sessions.clear()


def weak_card(card: dict[str, Any]) -> dict[str, Any]:
    """The fixture card, but with a session_check that also passes when signed out (a public search)."""
    card = copy.deepcopy(card)
    check = card["connect_providers"][0]["session_check"]
    check["url"] = "{{base_url}}/api/products"
    check["query"] = {"q": "lamp"}
    check["expect"] = {"status": [200], "content_type": "json", "paths": ["$.results"]}
    return card


def make_manager(tmp_path: Path, card: dict[str, Any], neko: FakeNeko) -> tuple[ConnectionManager, Registry]:
    db = tmp_path / "c.db"
    registry = Registry(db)
    registry.save(card)
    store = EncryptedSessionStore(db, new_session_key())
    manager = ConnectionManager(
        registry, store, Executor(store, Confirmer("s")), "http://gw", neko=neko, check_every_s=0.05  # type: ignore[arg-type]
    )
    return manager, registry


async def test_weak_check_waits_for_the_user(tmp_path: Path, fixture_card: dict[str, Any], site_url: str) -> None:
    neko = FakeNeko()
    manager, registry = make_manager(tmp_path, weak_card(fixture_card), neko)
    token = manager.start("u", DOMAIN)["url"].rsplit("/", 1)[1]
    started = await manager.start_browser_login(token)
    assert started["confirm"] is True

    neko.cookies_now = {"anon": "1"}
    await asyncio.sleep(0.2)  # the watcher records the first cookies
    with pytest.raises(CookieImportError, match="doesn't look like you've signed in"):
        await manager.confirm_browser_login(token)  # nothing changed yet

    neko.cookies_now = {"anon": "1", **SIGNED_IN}
    await asyncio.sleep(0.2)
    assert manager.poll("u", DOMAIN)["status"] == "pending"  # never auto-connects on a weak check
    conn = await manager.confirm_browser_login(token)
    assert conn.status == "connected"
    assert manager.store.get("u", DOMAIN, "shop") == {"anon": "1", **SIGNED_IN}
    assert neko.sessions == {}  # the login browser was stopped
    registry.close()


async def test_strong_check_auto_connects_and_confirm_verifies(
    tmp_path: Path, fixture_card: dict[str, Any], site_url: str
) -> None:
    neko = FakeNeko()
    manager, registry = make_manager(tmp_path, fixture_card, neko)
    token = manager.start("u", DOMAIN)["url"].rsplit("/", 1)[1]
    assert (await manager.start_browser_login(token))["confirm"] is False

    neko.cookies_now = {"sid": "sid-revoked"}
    with pytest.raises(CookieImportError, match="doesn't look like you've signed in"):
        await manager.confirm_browser_login(token)  # the real check rejects it

    neko.cookies_now = dict(SIGNED_IN)
    for _ in range(40):  # the watcher notices by itself
        if manager.poll("u", DOMAIN)["status"] == "connected":
            break
        await asyncio.sleep(0.05)
    assert manager.poll("u", DOMAIN)["status"] == "connected"
    registry.close()


async def test_confirm_needs_a_started_browser(tmp_path: Path, fixture_card: dict[str, Any]) -> None:
    manager, registry = make_manager(tmp_path, fixture_card, FakeNeko())
    token = manager.start("u", DOMAIN)["url"].rsplit("/", 1)[1]
    with pytest.raises(CookieImportError, match="start the sign-in first"):
        await manager.confirm_browser_login(token)
    with pytest.raises(CookieImportError, match="unknown login link"):
        await manager.confirm_browser_login("nope")
    registry.close()


@pytest.fixture
def weak_gateway(tmp_path: Path, fixture_card: dict[str, Any]) -> Iterator[tuple[str, FakeNeko]]:
    neko = FakeNeko()
    manager, registry = make_manager(tmp_path, weak_card(fixture_card), neko)
    gateway = Gateway(registry, manager.executor, poll_interval_s=0.05, connections=manager)
    with LiveServer(gateway.asgi_app(API_KEY)) as server:
        manager.public_base_url = server.url
        yield server.url, neko
    registry.close()


async def test_signed_in_button_on_the_page(weak_gateway: tuple[str, FakeNeko], site_url: str) -> None:
    base, neko = weak_gateway
    async with connect(f"{base}/mcp", "auto") as client:
        link = envelope(await client.call_tool("start_connection", {"domain": DOMAIN}))["url"]
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(channel="chromium", headless=True)
            except Exception:
                pytest.skip("Playwright's Chromium is not installed")
            page = await browser.new_page()
            await page.goto(link)
            await page.click("#browser")
            await page.wait_for_selector("#done", state="visible")
            assert "once you've finished signing in" in await page.inner_text("#done-hint")
            await asyncio.sleep(0.3)  # the watcher records the first cookies

            await page.click("#done")
            await page.wait_for_selector("#status.bad")
            assert "doesn't look like you've signed in" in await page.inner_text("#status")

            neko.cookies_now = {"anon": "1", **SIGNED_IN}
            await page.click("#done")
            await page.wait_for_selector("#status.connected")
            await browser.close()
        assert envelope(await client.call_tool("poll_connection", {"domain": DOMAIN}))["status"] == "connected"
        assert envelope(await client.call_tool("shop_list_orders", {}))["status"] == "SUCCESS"
