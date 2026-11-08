"""'Log in here': a Neko login browser in Docker, streamed over WebRTC through the gateway's proxy.

Needs Docker and the Neko image (docker pull ghcr.io/m1k1o/neko/chromium:latest); skipped otherwise.
"""

import asyncio
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import Page, async_playwright

from runtime.confirm import Confirmer
from runtime.connect import ConnectionManager
from runtime.executor import Executor
from runtime.gateway import Gateway
from runtime.neko.manager import LABEL, NekoConfig, NekoManager
from runtime.registry import Registry
from runtime.session_store import EncryptedSessionStore, new_session_key
from tests.conftest import LiveServer
from tests.fixture_site.app import LOGIN_PASSWORD, LOGIN_USER, state
from tests.test_gateway import API_KEY, act, connect, envelope

pytestmark = pytest.mark.xdist_group("docker")  # login-browser containers share a port range

DOMAIN = "fixture.test"
IMAGE = NekoConfig().image


@pytest.fixture(scope="module")
def neko_image() -> None:
    try:
        ok = (
            subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=20).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        pytest.skip(f"Docker or the Neko image is not available (docker pull {IMAGE})")


@pytest.fixture
def neko_gateway(
    tmp_path: Path, fixture_card: dict[str, Any], neko_image: None
) -> Iterator[tuple[str, Path, EncryptedSessionStore]]:
    db = tmp_path / "gw.db"
    registry = Registry(db)
    registry.save(fixture_card)
    store = EncryptedSessionStore(db, new_session_key())
    executor = Executor(store, Confirmer("s"))
    neko = NekoManager(NekoConfig(udp_ports=(52100, 52109)))
    manager = ConnectionManager(registry, store, executor, "http://placeholder", neko=neko, check_every_s=0.5)
    gateway = Gateway(registry, executor, poll_interval_s=0.05, connections=manager)
    with LiveServer(gateway.asgi_app(API_KEY)) as server:
        manager.public_base_url = server.url
        yield server.url, db, store
    registry.close()


def login_containers() -> list[str]:
    out = subprocess.run(["docker", "ps", "-q", "--filter", f"label={LABEL}"], capture_output=True, text=True)
    return out.stdout.split()


async def field_fractions(site_url: str) -> dict[str, tuple[float, float]]:
    """Where the login form's controls are on a 1280x800 kiosk screen, as fractions of the screen."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chromium", headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.goto(f"{site_url}/login")
        out = {}
        for sel in ("#username", "#password", "#submit"):
            b = await page.locator(sel).bounding_box()
            assert b is not None
            out[sel] = ((b["x"] + b["width"] / 2) / 1280, (b["y"] + b["height"] / 2) / 800)
        await browser.close()
        return out


async def click_in_stream(page: Page, fx: float, fy: float) -> None:
    video = page.frame_locator("#frame").locator("video")
    box = await video.bounding_box()
    assert box is not None
    vw, vh = await video.evaluate("v => [v.videoWidth, v.videoHeight]")
    scale = min(box["width"] / vw, box["height"] / vh)  # object-fit: contain
    left = box["x"] + (box["width"] - vw * scale) / 2
    top = box["y"] + (box["height"] - vh * scale) / 2
    await page.mouse.click(left + fx * vw * scale, top + fy * vh * scale)


async def _launch_user_browser(pw: Any, engine: str) -> Any:
    if engine == "firefox":  # Firefox refuses WebRTC to loopback, so this also checks the advertised address
        return await pw.firefox.launch(headless=True, firefox_user_prefs={"media.autoplay.default": 0})
    return await pw.chromium.launch(
        channel="chromium", headless=True, args=["--autoplay-policy=no-user-gesture-required"]
    )


@pytest.mark.parametrize("engine", ["chromium", "firefox"])
async def test_log_in_here_with_neko(
    neko_gateway: tuple[str, Path, EncryptedSessionStore], site_url: str, engine: str
) -> None:
    base, _, store = neko_gateway
    spots = await field_fractions(site_url)
    async with connect(f"{base}/mcp", "auto") as client:
        link = envelope(await client.call_tool("start_connection", {"domain": DOMAIN}))["url"]
        token = link.rsplit("/", 1)[1]
        async with async_playwright() as pw:
            try:
                browser = await _launch_user_browser(pw, engine)
            except Exception:
                pytest.skip(f"Playwright's {engine} is not installed (uv run playwright install {engine})")
            page = await browser.new_page(viewport={"width": 1400, "height": 1100})
            await page.goto(link)
            await page.click("#browser")
            await page.wait_for_selector("#viewer iframe[src*='/neko/']", timeout=60_000)
            frame = page.frame_locator("#frame")
            await frame.locator("video").wait_for(timeout=30_000)
            await page.wait_for_function(
                "() => { const v = document.getElementById('frame').contentDocument.querySelector('video');"
                " return v && v.readyState >= 2 && v.videoWidth > 0 }",
                timeout=30_000,
            )
            assert len(login_containers()) == 1
            stream_size = await frame.locator("video").evaluate("v => [v.videoWidth, v.videoHeight]")
            # Playback-clock speed: ~1.0 when frames keep arriving (works in every engine, unlike frame
            # counters).
            fps = await frame.locator("video").evaluate(
                "async v => { const t = v.currentTime; await new Promise(r => setTimeout(r, 2000));"
                " return Math.round((v.currentTime - t) / 2 * 100) / 100 }"
            )
            print(f"{engine}: playback clock {fps}x")
            assert stream_size == [1920, 1200] and fps >= 0.8

            await click_in_stream(page, *spots["#username"])
            await asyncio.sleep(0.5)
            await page.keyboard.type(LOGIN_USER, delay=40)
            await click_in_stream(page, *spots["#password"])
            await asyncio.sleep(0.3)
            await page.keyboard.type(LOGIN_PASSWORD, delay=40)
            await click_in_stream(page, *spots["#submit"])

            await page.wait_for_selector("#done-panel", state="visible", timeout=30_000)
            await browser.close()
        print(f"{engine}: stream {stream_size[0]}x{stream_size[1]}, playback {fps}x")

        assert (
            envelope(await client.call_tool("poll_connection", {"domain": DOMAIN}))["status"] == "connected"
        )
        # Device identity: what we saved is exactly what the login browser sent the site when the user signed
        # in (the site is on 127.0.0.1 inside the container, a secure context, so client hints are sent too).
        identity = store.identity("default", DOMAIN, "shop")
        assert identity is not None and identity.platform == "Linux" and identity.brand is None
        sent_at_login = state.last_headers["POST /login"]
        for name, value in identity.headers().items():
            assert sent_at_login.get(name) == value, (name, sent_at_login.get(name), value)
        env = envelope(await client.call_tool(*act("list_orders", {})))
        assert env["status"] == "SUCCESS", env
        later = state.last_headers[
            "GET /api/account/orders"
        ]  # and our server's request presents the same browser
        assert {k: later.get(k) for k in identity.headers()} == identity.headers()

    for _ in range(40):  # the login browser is removed once connected
        if not login_containers():
            break
        await asyncio.sleep(0.25)
    assert login_containers() == []
    assert httpx.get(f"{base}/neko/{token}/").status_code == 404


def test_cookie_matching_rules() -> None:
    from runtime.neko.manager import _applies

    url = "https://www.linkedin.com/feed/"
    assert _applies({"domain": ".linkedin.com", "path": "/"}, url)
    assert _applies({"domain": "www.linkedin.com", "path": "/feed"}, url)
    assert not _applies({"domain": "linkedin.com", "path": "/"}, url)  # host-only cookie for another host
    assert not _applies({"domain": ".evil.com", "path": "/"}, url)
    assert not _applies({"domain": ".linkedin.com", "path": "/jobs"}, url)
    assert not _applies({"domain": "127.0.0.1", "path": "/", "secure": True}, "http://127.0.0.1:8900/")
    assert _applies({"domain": "127.0.0.1", "path": "/"}, "http://127.0.0.1:8900/")


def test_a_start_page_with_percent_signs_keeps_the_config_valid(tmp_path: Path) -> None:
    """LinkedIn's login page has %2F in its address; supervisord reads % as its own placeholder, so it must
    be written as %% (unescaped, the login browser never starts)."""
    import configparser

    from runtime.neko.manager import NekoConfig, chromium_conf

    url = "https://www.linkedin.com/uas/login?session_redirect=%2Fsearch%2Fresults%2F&skipRedirect=true"
    conf = chromium_conf(NekoConfig(), url, ["0.0.0.0:9223=127.0.0.1:9222"], None, tmp_path)
    parser = configparser.ConfigParser(defaults={"ENV_USER": "neko", "ENV_DISPLAY": ":99.0"})
    parser.read_string(conf)
    assert url in parser.get("program:chromium", "command")  # supervisord's reading gives the URL back
