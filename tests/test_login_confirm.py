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
from runtime.connect import ConnectError, ConnectionManager
from runtime.executor import Executor
from runtime.gateway import Gateway
from runtime.identity import BrowserIdentity
from runtime.neko.manager import NekoConfig
from runtime.registry import Registry
from runtime.session_store import EncryptedSessionStore, new_session_key
from tests.conftest import LiveServer
from tests.fixture_site.app import CSRF, VALID_SID
from tests.test_gateway import API_KEY, act, connect, envelope

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

    IDENTITY = BrowserIdentity("Mozilla/5.0 (X11; Linux x86_64) Chrome/155.0.0.0", "155.0.1.2", "Linux", None, "en-US")

    async def identity(self, token: str) -> BrowserIdentity | None:
        return self.IDENTITY if token in self.sessions else None

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
    with pytest.raises(ConnectError, match="doesn't look like you've signed in"):
        await manager.confirm_browser_login(token)  # nothing changed yet

    neko.cookies_now = {"anon": "1", **SIGNED_IN}
    await asyncio.sleep(0.2)
    assert (await manager.poll("u", DOMAIN))["status"] == "pending"  # never auto-connects on a weak check
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
    with pytest.raises(ConnectError, match="doesn't look like you've signed in"):
        await manager.confirm_browser_login(token)  # the real check rejects it

    neko.cookies_now = dict(SIGNED_IN)
    for _ in range(40):  # the watcher notices by itself
        if (await manager.poll("u", DOMAIN))["status"] == "connected":
            break
        await asyncio.sleep(0.05)
    assert (await manager.poll("u", DOMAIN))["status"] == "connected"
    registry.close()


async def test_confirm_needs_a_started_browser(tmp_path: Path, fixture_card: dict[str, Any]) -> None:
    manager, registry = make_manager(tmp_path, fixture_card, FakeNeko())
    token = manager.start("u", DOMAIN)["url"].rsplit("/", 1)[1]
    with pytest.raises(ConnectError, match="start the sign-in first"):
        await manager.confirm_browser_login(token)
    with pytest.raises(ConnectError, match="unknown login link"):
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
            await page.wait_for_selector("#done-panel", state="visible")
            assert "You're signed in to fixture.test" in await page.inner_text("#done-panel")
            assert await page.is_hidden("#viewer")
            await browser.close()
        assert envelope(await client.call_tool("poll_connection", {"domain": DOMAIN}))["status"] == "connected"
        assert envelope(await client.call_tool(*act("list_orders", {})))["status"] == "SUCCESS"


async def test_poll_waits_for_the_sign_in(tmp_path: Path, fixture_card: dict[str, Any], site_url: str) -> None:
    neko = FakeNeko()
    manager, registry = make_manager(tmp_path, fixture_card, neko)
    token = manager.start("u", DOMAIN)["url"].rsplit("/", 1)[1]
    await manager.start_browser_login(token)
    quick = await manager.poll("u", DOMAIN)  # no wait: answers straight away
    assert quick["status"] == "pending"

    async def sign_in_later() -> None:
        await asyncio.sleep(0.6)
        neko.cookies_now = dict(SIGNED_IN)

    loop = asyncio.get_running_loop()
    task = asyncio.create_task(sign_in_later())
    started = loop.time()
    waited = await manager.poll("u", DOMAIN, wait_s=10)
    took = loop.time() - started
    await task
    assert waited["status"] == "connected" and waited["user_message"] == "You're signed in to fixture.test."
    assert 0.5 < took < 3  # returned as soon as the sign-in landed, not after the full wait

    timeout_started = loop.time()
    manager.start("v", DOMAIN)
    still = await manager.poll("v", DOMAIN, wait_s=0.5)
    assert still["status"] == "pending" and 0.4 < loop.time() - timeout_started < 2
    registry.close()


async def test_poll_does_not_hang_when_the_link_was_never_opened(
    tmp_path: Path, fixture_card: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import runtime.connect as connect_module

    monkeypatch.setattr(connect_module, "UNOPENED_POLL_WAIT_S", 0.4)
    manager, registry = make_manager(tmp_path, fixture_card, FakeNeko())
    started = manager.start("u", DOMAIN)
    token = started["url"].rsplit("/", 1)[1]
    loop = asyncio.get_running_loop()

    t0 = loop.time()
    out = await manager.poll("u", DOMAIN, wait_s=30)
    assert loop.time() - t0 < 2  # didn't hold for 30 s
    assert out["status"] == "pending" and out["link_opened"] is False
    assert "can't see tool results" in out["hint"] and started["url"] in out["user_message"]

    manager.mark_opened(token)
    t0 = loop.time()
    out = await manager.poll("u", DOMAIN, wait_s=1)
    assert loop.time() - t0 >= 0.9  # opened: the full wait applies
    assert out["link_opened"] is True and "can't see tool results" not in out["hint"]
    registry.close()


# ---- the client shows the link itself (MCP URL elicitation) -------------------


@pytest.fixture
def strong_gateway(tmp_path: Path, fixture_card: dict[str, Any]) -> Iterator[tuple[str, FakeNeko]]:
    neko = FakeNeko()
    manager, registry = make_manager(tmp_path, fixture_card, neko)
    gateway = Gateway(registry, manager.executor, poll_interval_s=0.05, connections=manager)
    with LiveServer(gateway.asgi_app(API_KEY)) as server:
        manager.public_base_url = server.url
        yield server.url, neko
    registry.close()


async def _client_with_elicitation(base: str, on_url: Any) -> Any:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {API_KEY}"}, timeout=60)
    transport = streamable_http_client(f"{base}/mcp", http_client=http)
    return http, Client(transport, elicitation_callback=on_url, cache=None)  # type: ignore[arg-type]


async def test_client_shows_the_link_and_the_call_waits(strong_gateway: tuple[str, FakeNeko], site_url: str) -> None:
    import httpx
    import mcp_types as types

    base, neko = strong_gateway
    shown: list[tuple[str, str]] = []

    async def on_url(context: Any, params: Any) -> Any:
        assert params.mode == "url"
        shown.append((params.message, params.url))
        # Play the user: open the page, start the login browser, sign in a moment later.
        assert httpx.get(params.url).status_code == 200
        assert httpx.post(params.url + "/browser").status_code == 200

        async def sign_in() -> None:
            await asyncio.sleep(0.5)
            neko.cookies_now = dict(SIGNED_IN)

        asyncio.get_running_loop().create_task(sign_in())
        return types.ElicitResult(action="accept")

    http, client = await _client_with_elicitation(base, on_url)
    async with http, client:
        out = envelope(await client.call_tool("start_connection", {"domain": DOMAIN}))
        assert len(shown) == 1 and shown[0][0].startswith("Sign in to fixture.test")
        assert shown[0][1].startswith(f"{base}/connect/")
        assert out["status"] == "connected" and out["user_message"] == "You're signed in to fixture.test."
        assert envelope(await client.call_tool(*act("list_orders", {})))["status"] == "SUCCESS"


async def test_declining_the_link(strong_gateway: tuple[str, FakeNeko], monkeypatch: pytest.MonkeyPatch) -> None:
    """A person takes a moment to say no: that's a real decline."""
    import mcp_types as types

    import runtime.gateway

    monkeypatch.setattr(runtime.gateway, "HUMAN_REPLY_MIN_S", 0.2)
    base, _ = strong_gateway

    async def on_url(context: Any, params: Any) -> Any:
        await asyncio.sleep(0.3)
        return types.ElicitResult(action="decline")

    http, client = await _client_with_elicitation(base, on_url)
    async with http, client:
        out = envelope(await client.call_tool("start_connection", {"domain": DOMAIN}))
        assert out["status"] == "pending" and out["user_message"] == "Signing in to fixture.test was cancelled."
        assert "url" not in out


@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_app_that_answers_url_prompts_itself_gets_the_link_back(
    strong_gateway: tuple[str, FakeNeko], action: str
) -> None:
    """Claude Code's desktop app declares URL elicitation but answers it itself within milliseconds (it used to
    cancel, now it declines). No person answers that fast, so the agent shows the link instead."""
    import mcp_types as types

    base, _ = strong_gateway

    async def on_url(context: Any, params: Any) -> Any:
        return types.ElicitResult(action=action)  # type: ignore[arg-type]

    http, client = await _client_with_elicitation(base, on_url)
    async with http, client:
        out = envelope(await client.call_tool("start_connection", {"domain": DOMAIN}))
        assert out["status"] == "pending" and out["user_message"].startswith("**[Sign in to fixture.test →](")
        assert "couldn't show the sign-in link" in out["hint"] and "don't end your turn" in out["hint"]


async def test_clients_without_url_elicitation_get_the_link(strong_gateway: tuple[str, FakeNeko]) -> None:
    base, _ = strong_gateway
    async with connect(f"{base}/mcp", "auto") as client:  # no elicitation callback
        out = envelope(await client.call_tool("start_connection", {"domain": DOMAIN}))
        assert out["status"] == "pending" and out["user_message"].startswith("**[Sign in to fixture.test →](")


