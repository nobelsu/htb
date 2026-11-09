"""Residential proxies: a sign-in's requests and its login browser leave through one sticky proxy session;
public
actions go direct; credentials are never logged."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from spider.card import ResultCode, load_card
from spider.run.confirm import Confirmer
from spider.run.executor import Executor
from spider.run.request.template import BuiltRequest
from spider.run.tiers.http import HttpSession, ImpersonateSession
from spider.signin.proxy import ProxyConfig, ProxyConfigError, is_local, session_id
from spider.signin.sessions.base import InMemorySessionStore
from spider.store.registry import StoredCard
from tests.signin.fake_proxy import FakeProxy
from tests.sites.fixture_site.app import CSRF, VALID_SID

R = ResultCode
DOMAIN = "fixture.test"
SECRET = "pa55-w0rd"


@pytest.fixture
async def proxy() -> AsyncIterator[FakeProxy]:
    p = await FakeProxy().start()
    yield p
    await p.stop()


def config(proxy: FakeProxy, template: str | None = None) -> ProxyConfig:
    return ProxyConfig(
        template or f"http://cust-session-{{session}}:{SECRET}@127.0.0.1:{proxy.port}", local=True
    )


# ---- the configuration ------------------------------------------------------------------------------------


def test_one_sticky_session_per_sign_in() -> None:
    cfg = ProxyConfig("http://cust-session-{session}:pw@gate.example.com:7000")
    a = cfg.for_sign_in("alice", "shop.example", "shop", "https://shop.example")
    assert a is not None and a.username == f"cust-session-{session_id('alice', 'shop.example', 'shop')}"
    assert a.password == "pw" and (a.host, a.port, a.scheme) == ("gate.example.com", 7000, "http")
    again = cfg.for_sign_in("alice", "shop.example", "shop", "https://shop.example")
    assert again == a  # same sign-in: same session, so the same IP
    others = {
        cfg.for_sign_in("bob", "shop.example", "shop", "https://shop.example"),
        cfg.for_sign_in("alice", "other.example", "shop", "https://other.example"),
        cfg.for_sign_in("alice", "shop.example", "sso", "https://shop.example"),
    }
    assert a not in others and len(others) == 3
    assert session_id("alice", "x", None).isalnum()


def test_credentials_never_show_in_text() -> None:
    spec = ProxyConfig("http://cust-{session}:pw-SECRET@gate.example.com:7000").for_sign_in(
        "u", "d", "p", "https://d"
    )
    assert spec is not None
    for text in (repr(spec), str(spec), spec.redacted, f"{spec}"):
        assert "pw-SECRET" not in text and "gate.example.com" in text
    assert "pw-SECRET" not in ProxyConfig("http://u:pw-SECRET@g.example.com:1").redacted


@pytest.mark.parametrize(
    "bad", ["", "gate.example.com:7000", "ftp://u:p@g.example.com:21", "http://u:p@g.example.com"]
)
def test_bad_templates_are_refused(bad: str) -> None:
    with pytest.raises(ProxyConfigError):
        ProxyConfig(bad)


def test_local_sites_go_direct_and_templates_without_session_arent_sticky() -> None:
    cfg = ProxyConfig("http://u-{session}:p@gate.example.com:7000")
    assert cfg.for_sign_in("u", "d", "p", "http://127.0.0.1:8900") is None
    assert cfg.for_sign_in("u", "d", "p", "http://localhost:3000") is None
    assert is_local("[::1]") and not is_local("example.com")
    assert cfg.sticky and not ProxyConfig("http://u:p@gate.example.com:7000").sticky


# ---- tiers ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("session_cls", [HttpSession, ImpersonateSession])
async def test_tiers_go_through_the_proxy(session_cls: Any, proxy: FakeProxy, site_url: str) -> None:
    spec = config(proxy).for_sign_in("alice", DOMAIN, "shop", site_url)
    session = session_cls(site_url, {}, None, spec)
    try:
        resp = await session.send(BuiltRequest("GET", f"{site_url}/api/echo", {}), 5)
    finally:
        await session.aclose()
    assert resp.status == 200
    assert [(s.username, s.password) for s in proxy.seen] == [(spec.username, SECRET)]
    assert "/api/echo" in proxy.paths()[0]


# ---- the executor: signed-in requests through the proxy, public ones direct --------------------------------


def stored_card(fixture_card: dict[str, Any]) -> StoredCard:
    return StoredCard(DOMAIN, 1, load_card(fixture_card), True, "now", None)


async def test_signed_in_requests_use_the_sign_ins_session_and_public_ones_go_direct(
    proxy: FakeProxy, fixture_card: dict[str, Any], site_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    stored = stored_card(fixture_card)
    store = InMemorySessionStore()
    store.put("alice", DOMAIN, "shop", {"sid": VALID_SID, "csrftoken": CSRF})
    executor = Executor(store, Confirmer("s"), proxies=config(proxy))
    with caplog.at_level(logging.DEBUG, logger="spider"):
        assert (await executor.run(stored, "list_orders", {}, user="alice")).status == R.SUCCESS
        assert (
            await executor.run(stored, "search_products", {"query": "lamp"}, user="alice")
        ).status == R.SUCCESS
        assert await executor.check_session(stored.card, "shop", "alice") is None
    expected = f"cust-session-{session_id('alice', DOMAIN, 'shop')}"
    assert [(p, s.username) for p, s in zip(proxy.paths(), proxy.seen, strict=False)] == [
        ("/api/account/orders", expected),  # signed in: through alice's session
        ("/api/me", expected),  # the sign-in check too
    ]  # the public search never touched the proxy
    assert SECRET not in caplog.text


async def test_each_user_gets_their_own_session(
    proxy: FakeProxy, fixture_card: dict[str, Any], site_url: str
) -> None:
    stored = stored_card(fixture_card)
    store = InMemorySessionStore()
    for user in ("alice", "bob"):
        store.put(user, DOMAIN, "shop", {"sid": VALID_SID, "csrftoken": CSRF})
    executor = Executor(store, Confirmer("s"), proxies=config(proxy))
    for user in ("alice", "bob"):
        assert (await executor.run(stored, "list_orders", {}, user=user)).status == R.SUCCESS
    assert [s.username for s in proxy.seen] == [
        f"cust-session-{session_id(u, DOMAIN, 'shop')}" for u in ("alice", "bob")
    ]


async def test_escalation_keeps_the_proxy(
    proxy: FakeProxy, fixture_card: dict[str, Any], site_url: str
) -> None:
    import copy

    card = copy.deepcopy(fixture_card)
    flow = next(a for a in card["actions"] if a["name"] == "guarded_flow")
    flow["auth_provider"] = "shop"  # a signed-in flow that escalates from http to impersonate
    store = InMemorySessionStore()
    store.put("alice", DOMAIN, "shop", {"sid": VALID_SID, "csrftoken": CSRF})
    env = await Executor(store, Confirmer("s"), proxies=config(proxy)).run(
        stored_card(card), "guarded_flow", {}, user="alice"
    )
    assert env.status == R.SUCCESS and env.meta.tier == "impersonate"
    assert proxy.paths() == ["/api/flow/start", "/api/guarded/flow-finish", "/api/guarded/flow-finish"]
    assert len({s.username for s in proxy.seen}) == 1


# ---- the login browser's helper: adds the credentials Chromium can't send ----------------------------------


async def _helper(proxy: FakeProxy) -> Any:
    from spider.signin.neko.proxy_auth import serve

    return await serve(
        0, {"host": "127.0.0.1", "port": proxy.port, "username": "cust-session-abc", "password": SECRET}
    )


async def test_login_browser_helper_adds_credentials(proxy: FakeProxy, site_url: str) -> None:
    helper = await _helper(proxy)
    port = helper.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{port}"
        ) as client:  # no credentials, like Chromium
            for _ in range(2):  # a second request on the same client is authenticated too
                assert (await client.get(f"{site_url}/api/echo")).status_code == 200
        # A CONNECT tunnel (what https sites use)
        host, site_port = site_url.split("//")[1].split(":")
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT {host}:{site_port} HTTP/1.1\r\nHost: {host}:{site_port}\r\n\r\n".encode())
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        writer.write(f"GET /api/echo HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        assert (await reader.read()).startswith(b"HTTP/1.1 200")
        writer.close()
    finally:
        helper.close()
    assert [(s.method, s.username, s.password) for s in proxy.seen] == [
        ("GET", "cust-session-abc", SECRET),
        ("GET", "cust-session-abc", SECRET),
        ("CONNECT", "cust-session-abc", SECRET),
    ]


def test_login_browser_is_told_to_use_the_helper(tmp_path: Path) -> None:
    from spider.signin.neko.manager import NekoConfig, chromium_conf

    spec = ProxyConfig("http://cust-{session}:S3CRET-pw@gate.example.com:7000").for_sign_in(
        "u", "d", "p", "https://d"
    )
    assert spec is not None
    conf = chromium_conf(NekoConfig(), "https://d/login", ["0.0.0.0:9223=127.0.0.1:9222"], spec, tmp_path)
    assert "--proxy-server=http://127.0.0.1:" in conf and "proxy_auth.py" in conf
    assert "S3CRET-pw" not in conf  # the password is only in the private upstream file
    upstream = json.loads((tmp_path / "upstream.json").read_text())
    assert upstream == {
        "host": "gate.example.com",
        "port": 7000,
        "username": spec.username,
        "password": "S3CRET-pw",
    }
    assert (tmp_path / "upstream.json").stat().st_mode & 0o077 == 0
    plain = chromium_conf(
        NekoConfig(), "https://d/login", ["0.0.0.0:9223=127.0.0.1:9222"], None, tmp_path / "x"
    )
    assert "--proxy-server" not in plain and "proxy_auth" not in plain


# ---- the browser tier (real Chrome) and the login browser (real Neko in Docker) ----------------------------

from tests.signin.test_flow import chrome  # noqa: E402,F401  (fixture: skips without Chrome)
from tests.signin.test_neko import neko_image  # noqa: E402,F401  (fixture: skips without Docker and the image)


async def test_browser_tier_goes_through_the_proxy(
    tmp_path: Path, proxy: FakeProxy, site_url: str, chrome: None
) -> None:
    from spider.run.tiers.browser import BrowserSession
    from spider.run.tiers.browser_pool import BrowserConfig, BrowserPool
    from spider.run.tiers.http import ProfileKey
    from tests.signin.test_flow import SERVER_CHANNEL

    spec = config(proxy).for_sign_in("alice", DOMAIN, "shop", site_url)
    pool = BrowserPool(BrowserConfig(profiles_dir=tmp_path / "profiles", channel=SERVER_CHANNEL, idle_s=0))
    session = BrowserSession(pool, ProfileKey("alice", DOMAIN, "shop"), site_url, {}, None, spec)
    try:
        await session.prepare()
        resp = await session.send(BuiltRequest("GET", f"{site_url}/api/echo", {}), 10)
        await session.aclose()
    finally:
        await pool.close()
    assert resp.status == 200
    signed = [
        s for s in proxy.seen if s.username is not None
    ]  # Chrome answers the proxy's 407 with credentials
    assert any(p.endswith("/api/echo") for p in proxy.paths())
    assert signed and all((s.username, s.password) == (spec.username, SECRET) for s in signed)


@pytest.mark.xdist_group("docker")  # login-browser containers share a port range
async def test_login_browser_goes_through_the_proxy(
    proxy: FakeProxy, site_url: str, neko_image: None
) -> None:
    from spider.signin.neko.manager import NekoConfig, NekoManager

    spec = config(proxy).for_sign_in("alice", DOMAIN, "shop", site_url)
    neko = NekoManager(NekoConfig(udp_ports=(52110, 52119)))
    await neko.launch("proxytest01", f"{site_url}/login", [site_url], spec)
    try:
        for _ in range(120):  # Chromium opens the login page at start
            if any(
                p.endswith("/login") and s.username for p, s in zip(proxy.paths(), proxy.seen, strict=False)
            ):
                break
            await asyncio.sleep(0.25)
    finally:
        session = neko.get("proxytest01")
        await neko.close()
    login = [
        s for p, s in zip(proxy.paths(), proxy.seen, strict=False) if p.endswith("/login") and s.username
    ]
    assert login and (login[0].username, login[0].password) == (spec.username, SECRET)
    assert session is not None and not session.workdir.exists()  # its credentials file is gone
