"""Device identity: the browser a sign-in was made with is saved with it, and every tier presents it again."""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from runtime.confirm import Confirmer
from runtime.executor import Executor
from runtime.identity import DEFAULT_IDENTITY, BrowserIdentity, accept_language_header
from runtime.registry import StoredCard
from runtime.session_store import ChainedSessionStore, EncryptedSessionStore, new_session_key
from runtime.sessions import DevEnvSessionStore, InMemorySessionStore
from runtime.template import BuiltRequest
from runtime.tiers import HttpSession, ImpersonateSession
from schema import ResultCode, load_card
from tests.fixture_site.app import CSRF, VALID_SID, state
from tests.test_login import chrome  # noqa: F401  (fixture: skips without Chrome)

R = ResultCode
DOMAIN = "fixture.test"

# The login browser today: Neko's Chromium 155 on Linux (DevTools /json/version).
NEKO_VERSION = {
    "Browser": "Chrome/155.0.8059.79",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
}
LOGIN = BrowserIdentity.from_devtools(NEKO_VERSION, brand=None, accept_language="en-GB,en;q=0.9")


def chrome_at(major: int, brand: str | None = "Google Chrome") -> BrowserIdentity:
    return BrowserIdentity(f"Chrome/{major}", f"{major}.0.0.0", "Windows", brand, "en-US,en;q=0.9")


# ---- the identity itself ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("major", "expected"),
    [  # real Chrome headers
        (120, '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'),
        (122, '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"'),
        (124, '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'),
        (131, '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'),
        (152, '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"'),  # as curl_cffi's chrome152
    ],
)
def test_client_hints_match_real_chrome(major: int, expected: str) -> None:
    assert chrome_at(major).sec_ch_ua() == expected


def test_plain_chromium_has_no_google_brand() -> None:
    assert LOGIN.sec_ch_ua() == '"Chromium";v="155", "Not(A:Brand";v="24"'
    assert LOGIN.brands(full=True) == [("Chromium", "155.0.8059.79"), ("Not(A:Brand", "24.0.0.0")]


def test_identity_from_the_login_browser() -> None:
    assert LOGIN.full_version == "155.0.8059.79" and LOGIN.major == 155 and LOGIN.platform == "Linux"
    assert LOGIN.headers() == {
        "user-agent": NEKO_VERSION["User-Agent"],
        "sec-ch-ua": '"Chromium";v="155", "Not(A:Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
        "accept-language": "en-GB,en;q=0.9",
    }
    headless = {"Browser": "HeadlessChrome/150.0.1.2", "User-Agent": "Mozilla/5.0 (Macintosh) HeadlessChrome/150.0.0.0"}
    ident = BrowserIdentity.from_devtools(headless, brand="Google Chrome", accept_language="en-US")
    assert "HeadlessChrome" not in ident.user_agent and ident.platform == "macOS"
    with pytest.raises(ValueError):
        BrowserIdentity.from_devtools({"Browser": "Firefox/140", "User-Agent": "x"}, brand=None, accept_language="")


@pytest.mark.parametrize(
    ("ident", "target"),
    [
        (LOGIN, "chrome152"),  # newest fingerprint not newer than Chromium 155
        (chrome_at(140), "chrome136"),
        (chrome_at(90), "chrome99"),  # older than every fingerprint: the oldest
        (BrowserIdentity("Mac", "155.0.0.0", "macOS", "Google Chrome", "en"), "chrome152_macos"),
    ],
)
def test_closest_impersonate_fingerprint(ident: BrowserIdentity, target: str) -> None:
    assert ident.impersonate_target() == target


def test_accept_language_like_chrome() -> None:
    assert accept_language_header("en-US,en") == "en-US,en;q=0.9"
    assert accept_language_header("de-DE, de, en") == "de-DE,de;q=0.9,en;q=0.8"


def test_round_trips_as_json() -> None:
    assert BrowserIdentity.from_json(json.loads(json.dumps(LOGIN.to_json()))) == LOGIN


def test_default_identity_matches_the_impersonate_fingerprint() -> None:
    assert DEFAULT_IDENTITY.impersonate_target() == "chrome152"
    assert "Chrome/152.0.0.0" in DEFAULT_IDENTITY.user_agent and DEFAULT_IDENTITY.platform == "Windows"


# ---- what sites see, per tier -------------------------------------------------------------------------------


async def _echoed(session: Any, site_url: str, headers: dict[str, str] | None = None) -> dict[str, str]:
    try:
        resp = await session.send(BuiltRequest("GET", f"{site_url}/api/echo", headers or {}), 5)
    finally:
        await session.aclose()
    seen: dict[str, str] = httpx.Response(200, text=resp.text).json()["headers"]
    return seen


@pytest.mark.parametrize("session_cls", [HttpSession, ImpersonateSession])
async def test_tiers_present_the_identity(session_cls: Any, site_url: str) -> None:
    seen = await _echoed(session_cls(site_url, {}, LOGIN), site_url)
    for name, value in LOGIN.headers().items():
        assert seen[name] == value, name


@pytest.mark.parametrize("session_cls", [HttpSession, ImpersonateSession])
async def test_public_requests_look_like_chrome_not_python(session_cls: Any, site_url: str) -> None:
    seen = await _echoed(session_cls(site_url, {}), site_url)
    assert "python" not in seen["user-agent"].lower()
    for name, value in DEFAULT_IDENTITY.headers().items():
        assert seen[name] == value, name


async def test_a_cards_own_header_still_wins(site_url: str) -> None:
    seen = await _echoed(HttpSession(site_url, {}, LOGIN), site_url, {"accept-language": "fr-FR"})
    assert seen["accept-language"] == "fr-FR" and seen["user-agent"] == LOGIN.user_agent


def test_impersonate_uses_the_closest_fingerprint() -> None:
    assert ImpersonateSession("https://x.test", {}, LOGIN).target == "chrome152"
    assert ImpersonateSession("https://x.test", {}, chrome_at(140)).target == "chrome136"


# ---- the executor uses the sign-in's identity ----------------------------------------------------------


def _signed_in_card(fixture_card: dict[str, Any]) -> StoredCard:
    card = copy.deepcopy(fixture_card)
    flow = next(a for a in card["actions"] if a["name"] == "guarded_flow")
    flow["auth_provider"] = card["connect_providers"][0]["id"]  # a signed-in flow that escalates to impersonate
    return StoredCard(DOMAIN, 1, load_card(card), True, "now", None)


def _provider(stored: StoredCard) -> str:
    return stored.card.connect_providers[0].id


async def test_signed_in_runs_present_the_sign_ins_identity_on_every_tier(
    fixture_card: dict[str, Any], site_url: str
) -> None:
    stored = _signed_in_card(fixture_card)
    store = InMemorySessionStore()
    store.put("default", DOMAIN, _provider(stored), {"sid": VALID_SID, "csrftoken": CSRF}, LOGIN)
    env = await Executor(store, Confirmer("s")).run(stored, "guarded_flow", {})
    assert env.status == R.SUCCESS and env.meta.tier == "impersonate"
    # Step 1 on http, step 2 retried on impersonate after a block: both as the login browser.
    for request in ("GET /api/flow/start", "GET /api/guarded/flow-finish"):
        assert state.last_headers[request]["user-agent"] == LOGIN.user_agent, request
        assert state.last_headers[request]["sec-ch-ua"] == LOGIN.sec_ch_ua(), request


async def test_sign_in_checks_present_the_identity(fixture_card: dict[str, Any], site_url: str) -> None:
    stored = StoredCard(DOMAIN, 1, load_card(fixture_card), True, "now", None)
    store = InMemorySessionStore()
    store.put("default", DOMAIN, _provider(stored), {"sid": VALID_SID, "csrftoken": CSRF}, LOGIN)
    assert await Executor(store, Confirmer("s")).check_session(stored.card, _provider(stored), "default") is None
    assert state.last_headers["GET /api/me"]["user-agent"] == LOGIN.user_agent


async def test_sign_ins_without_an_identity_use_the_default(fixture_card: dict[str, Any], site_url: str) -> None:
    stored = StoredCard(DOMAIN, 1, load_card(fixture_card), True, "now", None)
    store = InMemorySessionStore()
    store.put("default", DOMAIN, _provider(stored), {"sid": VALID_SID, "csrftoken": CSRF})
    assert (await Executor(store, Confirmer("s")).run(stored, "list_orders", {})).status == R.SUCCESS
    assert state.last_headers["GET /api/account/orders"]["user-agent"] == DEFAULT_IDENTITY.user_agent


# ---- storage ----------------------------------------------------------------------------------------------


def test_the_encrypted_store_keeps_the_identity(tmp_path: Path) -> None:
    store = EncryptedSessionStore(tmp_path / "s.db", new_session_key())
    store.put("u", DOMAIN, "p", {"sid": "a"}, LOGIN)
    assert store.identity("u", DOMAIN, "p") == LOGIN
    assert store.refresh("u", DOMAIN, "p", {"sid": "a"}, {"sid": "b"})  # cookie rotation keeps it
    assert store.identity("u", DOMAIN, "p") == LOGIN
    store.put("u", DOMAIN, "p", {"sid": "c"})  # a new sign-in without one replaces it
    assert store.identity("u", DOMAIN, "p") is None
    assert store.identity("u", DOMAIN, "missing") is None


def test_databases_from_before_device_identity_still_open(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE sessions (user TEXT NOT NULL, domain TEXT NOT NULL, provider TEXT NOT NULL, "
            "cookies_enc BLOB NOT NULL, status TEXT NOT NULL, connected_at TEXT NOT NULL, checked_at TEXT, "
            "PRIMARY KEY (user, domain, provider))"
        )
    key = new_session_key()
    store = EncryptedSessionStore(db, key)
    store.put("u", DOMAIN, "p", {"sid": "a"}, LOGIN)
    assert EncryptedSessionStore(db, key).identity("u", DOMAIN, "p") == LOGIN


def test_chained_store_reads_the_identity_from_the_session_it_uses(tmp_path: Path) -> None:
    encrypted = EncryptedSessionStore(tmp_path / "s.db", new_session_key())
    dev = DevEnvSessionStore(json.dumps({DOMAIN: {"p": {"sid": "dev"}}}))
    chained = ChainedSessionStore(encrypted, dev)
    assert chained.identity("u", DOMAIN, "p") is None  # .env sign-ins have none: the default is used
    encrypted.put("u", DOMAIN, "p", {"sid": "a"}, LOGIN)
    assert chained.identity("u", DOMAIN, "p") == LOGIN


# ---- the login flow saves the login browser's identity ------------------------------------------------------


async def test_connecting_saves_the_login_browsers_identity(tmp_path: Path, fixture_card: dict[str, Any]) -> None:
    from tests.test_login_confirm import SIGNED_IN, FakeNeko, make_manager

    neko = FakeNeko()
    manager, _ = make_manager(tmp_path, fixture_card, neko)
    link = manager.start("default", DOMAIN)
    token = link["url"].rsplit("/", 1)[1]
    await manager.start_browser_login(token)
    neko.cookies_now = dict(SIGNED_IN)
    out = await manager.poll("default", DOMAIN, wait_s=5)
    assert out["status"] == "connected"
    provider = fixture_card["connect_providers"][0]["id"]
    assert manager.store.identity("default", DOMAIN, provider) == FakeNeko.IDENTITY


# ---- the browser tier (real Chrome) --------------------------------------------------------------------------


async def test_browser_tier_presents_the_identity(tmp_path: Path, site_url: str, chrome: None) -> None:
    from runtime.browser.pool import BrowserConfig, BrowserPool
    from runtime.browser.tier import BrowserSession
    from runtime.tiers import ProfileKey
    from tests.test_login import SERVER_CHANNEL

    pool = BrowserPool(BrowserConfig(profiles_dir=tmp_path / "profiles", channel=SERVER_CHANNEL, idle_s=0))
    session = BrowserSession(pool, ProfileKey("u", DOMAIN, None), site_url, {}, LOGIN)
    try:
        await session.prepare()
        seen = await _echoed(session, site_url)  # fetch() in the page; 127.0.0.1 counts as secure: hints are sent
        for name, value in LOGIN.headers().items():
            assert seen[name] == value, name
    finally:
        await pool.close()
