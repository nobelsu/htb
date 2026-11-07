"""The Spine pages: the home page, sign-in modes, changing your password, and the shared design rules."""

from __future__ import annotations

import html
import re

import httpx
import pytest

from runtime.ui import short_name, spine_size
from tests.test_oauth import PASSWORD, Env, account_session, env  # noqa: F401  (fixture)


async def test_home_page_is_public_but_only_at_the_root(env: Env) -> None:
    async with httpx.AsyncClient(follow_redirects=False) as http:
        home = await http.get(f"{env.base}/")
        assert home.status_code == 200 and "Your agent, signed in." in home.text
        assert 'href="/signup"' in home.text  # sign-up is open in this fixture
        assert (await http.get(f"{env.base}/anything-else")).status_code == 401  # "/" exempts only "/"
        assert (await http.post(f"{env.base}/mcp", json={})).status_code == 401


@pytest.mark.parametrize(
    ("query", "fields"),
    [("", ["login", "password"]), ("?with=key", ["key"]), ("?with=signup", ["code", "name", "email", "password"])],
)
async def test_sign_in_page_switches_how_you_sign_in(env: Env, query: str, fields: list[str]) -> None:
    async with httpx.AsyncClient(follow_redirects=False) as http:
        page = await http.get(f"{env.base}/account/login{query}")
    names = re.findall(r'<input class="field" name="([a-z]+)"', page.text)
    assert page.status_code == 200 and names == fields


async def test_change_password(env: Env) -> None:
    async with httpx.AsyncClient(follow_redirects=False) as http:
        await account_session(env, http)
        assert (await http.get(f"{env.base}/account/password")).status_code == 200
        wrong = await http.post(f"{env.base}/account/password", data={"current": "nope-nope", "new": "brand-new-pw1"})
        assert wrong.status_code == 401 and "current password" in html.unescape(wrong.text)
        short = await http.post(f"{env.base}/account/password", data={"current": PASSWORD, "new": "short"})
        assert short.status_code == 400
        done = await http.post(f"{env.base}/account/password", data={"current": PASSWORD, "new": "brand-new-pw1"})
        assert done.status_code == 302 and done.headers["location"] == "/account?changed=password"
        assert "Password changed." in (await http.get(f"{env.base}/account?changed=password")).text
    assert env.keys.check_login("alice", "brand-new-pw1") == "alice"
    assert env.keys.check_login("alice", PASSWORD) is None


async def test_pages_follow_the_design_rules(env: Env) -> None:
    """Black and white, no emoji or arrow symbols in the copy (icons are SVG), one spine per page."""
    async with httpx.AsyncClient(follow_redirects=False) as http:
        pages = [await http.get(f"{env.base}{path}") for path in ("/", "/signup", "/account/login")]
        await account_session(env, http)
        pages.append(await http.get(f"{env.base}/account"))
    for page in pages:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", page.text, flags=re.S)
        visible = html.unescape(re.sub(r"<[^>]+>", " ", text))
        assert not re.search("[←-⇿✀-➿\U0001f300-\U0001faff]", visible), page.url
        assert page.text.count('class="spine"') == 1


def test_spine_word_fits() -> None:
    assert short_name("www.github.com") == "github" and short_name("fixture.test") == "fixture"
    assert spine_size("Join") == 200 and spine_size("Account") == 180 and spine_size("Password") == 140


# ---- motion ---------------------------------------------------------------------------------------------------

from tests.test_login import chromium  # noqa: E402,F401  (fixture: skips without Playwright's Chromium)


async def test_every_page_has_motion_that_respects_reduce_motion(env: Env) -> None:
    async with httpx.AsyncClient(follow_redirects=False) as http:
        for path in ("/", "/signup", "/account/login"):
            text = (await http.get(f"{env.base}{path}")).text
            assert "@media (prefers-reduced-motion:reduce)" in text and "@keyframes rise" in text
            assert 'matchMedia("(prefers-reduced-motion: reduce)").matches) return;' in text


@pytest.mark.parametrize("motion", ["no-preference", "reduce"])
async def test_disconnect_folds_the_row_then_disconnects(env: Env, chromium: None, motion: str) -> None:
    from playwright.async_api import async_playwright

    env.store.put("alice", "fixture.test", env.provider, {"sid": "x"})
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page(reduced_motion=motion)  # type: ignore[arg-type]
            await page.goto(f"{env.base}/account/login")
            await page.fill("input[name=login]", "alice")
            await page.fill("input[name=password]", PASSWORD)
            await page.click("button.bar")
            await page.wait_for_url(f"{env.base}/account")
            row = page.locator(".row", has_text="fixture.test")
            await row.locator("button.sq").click()
            if motion == "no-preference":
                await page.wait_for_selector(".big.struck")  # struck through first, then folded away
            await page.wait_for_url(f"{env.base}/account")
            await page.wait_for_function("!document.body.innerText.includes('fixture.test')")
        finally:
            await browser.close()
    assert env.store.get("alice", "fixture.test", env.provider) is None
