"""The Margin pages: the home page, sign-in modes, changing your password, and the shared design rules."""

from __future__ import annotations

import html
import re

import httpx
import pytest

from runtime.ui import short_name
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
    """No emoji or arrow symbols in the copy (icons are SVG); every page has the spider panel and one heading."""
    async with httpx.AsyncClient(follow_redirects=False) as http:
        pages = [await http.get(f"{env.base}{path}") for path in ("/", "/signup", "/account/login")]
        await account_session(env, http)
        pages.append(await http.get(f"{env.base}/account"))
    for page in pages:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", page.text, flags=re.S)
        visible = html.unescape(re.sub(r"<[^>]+>", " ", text))
        assert not re.search("[←-⇿✀-➿\U0001f300-\U0001faff]", visible), page.url
        assert page.text.count('<canvas id="web"') == 1 and page.text.count("<h1") == 1, page.url
        assert "Linen" not in visible and "#F5EDE2" in page.text  # the Linen palette


def test_short_name() -> None:
    assert short_name("www.github.com") == "github" and short_name("fixture.test") == "fixture"


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


# ---- form fixes (asked: invite code, lines, cursor colour, errors) ----------------------------------------------


async def test_invite_code_is_a_plain_field_password_managers_skip(env: Env) -> None:
    async with httpx.AsyncClient() as http:
        for path in ("/signup", "/account/login?with=signup"):
            text = (await http.get(f"{env.base}{path}")).text
            code = re.search(r'<input class="field" name="code"[^>]*>', text)
            assert code is not None, path
            tag = code.group(0)
            assert 'type="text"' in tag and 'autocomplete="off"' in tag and "data-1p-ignore" in tag
            assert 'data-lpignore="true"' in tag and 'spellcheck="false"' in tag


async def test_fields_have_no_focus_outline_and_no_custom_cursor(env: Env) -> None:
    async with httpx.AsyncClient() as http:
        text = (await http.get(f"{env.base}/signup")).text
    assert ".field:focus,.field:focus-visible{outline:none}" in text  # the outline made a second line
    assert "cursor" not in text.split("<style>")[1].split("</style>")[0].replace("cursor:pointer", "")


async def test_form_errors_use_the_page_not_browser_bubbles(env: Env, chromium: None) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page(reduced_motion="reduce")
            await page.goto(f"{env.base}/signup")
            assert await page.evaluate("document.querySelector('form').noValidate")
            await page.click("button.bar")
            assert await page.text_content(".fld:has([name=code]) .ferr") == "Fill in your invite code."
            assert await page.get_attribute("input[name=code]", "aria-invalid") == "true"
            assert page.url == f"{env.base}/signup"  # nothing was sent
            await page.fill("input[name=code]", "x")
            assert await page.get_attribute("input[name=code]", "aria-invalid") is None
            await page.fill("input[name=name]", "bob")
            await page.fill("input[name=email]", "not-an-email")
            await page.fill("input[name=password]", "long enough password")
            await page.click("button.bar")
            assert await page.text_content(".fld:has([name=email]) .ferr") == "That email doesn't look right."
            assert await page.locator(".ferr").count() == 1  # the code's error went once it was filled in
            await page.fill("input[name=email]", "bob@example.com")
            await page.click("button.bar")  # now the server answers, in the same place
            await page.wait_for_load_state()
            assert await page.text_content(".notice") == "That invite code isn't right."
        finally:
            await browser.close()
