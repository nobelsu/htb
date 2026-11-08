"""flowmap's browser is set up like the gateway's (DECISIONS M8): a user's stored sign-in, its device identity
and proxy, and no automation flag; heal re-captures with the sign-in of the user whose call broke."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from flowmap.browser import BrowserSetup, SignInMissing, new_page
from flowmap.heal import FlowRemapper
from runtime.jobs.healing import RemapError
from runtime.signin.identity import BrowserIdentity
from runtime.signin.proxy import ProxyConfig
from runtime.signin.sessions import InMemorySessionStore
from schema import Card
from tests.conftest import LiveServer

EXAMPLE = Path(__file__).parents[2] / "action_map.example.json"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0"
IDENTITY = BrowserIdentity(
    UA + " Safari/537.36",
    "150.0.7000.1",
    "Windows",
    "Google Chrome",
    "en-GB,en;q=0.9",
)


def store() -> InMemorySessionStore:
    s = InMemorySessionStore()
    s.put("alice", "site.test", "site", {"sid": "alice-secret", "pref": "x"}, IDENTITY)
    return s


def test_setup_from_a_stored_sign_in() -> None:
    proxies = ProxyConfig("http://u-{session}:pw@proxy.example:7000")
    setup = BrowserSetup.for_sign_in(
        store(), "alice", "site.test", "site", "https://www.site.test", proxies=proxies
    )
    assert setup.cookies == {"sid": "alice-secret", "pref": "x"}
    assert setup.identity == IDENTITY and setup.channel == "chrome"
    assert setup.proxy is not None and setup.proxy.server == "http://proxy.example:7000"
    shown = repr(setup)
    assert "alice-secret" not in shown and "pw" not in shown and "cookies=2" in shown
    with pytest.raises(SignInMissing, match="bob has no sign-in"):
        BrowserSetup.for_sign_in(store(), "bob", "site.test", "site", "https://www.site.test")
    args = setup._launch_args()
    assert args["ignore_default_args"] == ["--enable-automation"] and args["channel"] == "chrome"


async def echo(request: Request) -> JSONResponse:
    return JSONResponse({"cookies": dict(request.cookies), "headers": dict(request.headers)})


async def home(request: Request) -> HTMLResponse:
    return HTMLResponse("<!doctype html><title>t</title><p>hi</p>")


@pytest.fixture(scope="module")
def site() -> Iterator[str]:
    with LiveServer(Starlette(routes=[Route("/", home), Route("/echo", echo)])) as server:
        yield server.url


@pytest.mark.browser
async def test_pages_carry_the_sign_in_and_identity(site: str) -> None:
    pytest.importorskip("playwright.async_api")
    from playwright.async_api import async_playwright

    setup = BrowserSetup.for_sign_in(store(), "alice", "site.test", "site", site, channel="chromium")
    async with async_playwright() as p:
        browser = await setup.launch(p)
        try:
            context = await browser.new_context()
            page = await new_page(context)
            await page.goto(site + "/")
            seen: dict[str, Any] = await page.evaluate("fetch('/echo').then(r => r.json())")
            navigator = await page.evaluate("[navigator.userAgent, navigator.webdriver]")
        finally:
            await browser.close()
    assert seen["cookies"] == {"sid": "alice-secret", "pref": "x"}
    headers = seen["headers"]
    assert headers["user-agent"] == IDENTITY.user_agent
    assert '"Google Chrome";v="150"' in headers.get("sec-ch-ua", "")
    assert headers["accept-language"].startswith("en-GB")
    assert navigator == [IDENTITY.user_agent, False]  # no automation flag for sign-in pages to spot


def test_heal_uses_the_sign_in_of_the_user_whose_call_broke() -> None:
    card = Card.model_validate(json.loads(EXAMPLE.read_text()))  # linkedin.com, signed in via "linkedin"
    sessions = InMemorySessionStore()
    sessions.put("alice", "linkedin.com", "linkedin", {"li_at": "alice-secret"}, IDENTITY)
    remapper = FlowRemapper(sessions=sessions, user="default", channel="chromium")
    old = card.to_json_dict()["actions"][0]
    setup = remapper._setup(card, old, "alice")
    assert setup.cookies == {"li_at": "alice-secret"} and setup.identity == IDENTITY
    with pytest.raises(RemapError, match="default has no sign-in"):
        remapper._setup(card, old, "default")
    public = remapper._setup(
        card.model_copy(update={"defaults": card.defaults.model_copy(update={"auth_provider": None})}),
        {**old, "auth_provider": None},
        "anyone",
    )
    assert public.cookies == {} and public.channel == "chromium"
