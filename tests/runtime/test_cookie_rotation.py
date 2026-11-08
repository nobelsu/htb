"""Cookie rotation: after a successful action or a passing sign-in check, the cookies the site refreshed are
saved back to the user's encrypted sign-in, mirroring the site (changed, new and deleted cookies)."""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import pytest

from runtime.run.confirm import Confirmer
from runtime.run.executor import Executor
from runtime.run.tiers import HttpSession, ImpersonateSession
from runtime.signin.session_store import ChainedSessionStore, EncryptedSessionStore, new_session_key
from runtime.signin.sessions import DevEnvSessionStore, InMemorySessionStore
from runtime.store.registry import Registry, StoredCard
from schema import validate_card
from schema.envelope import ResultCode as R
from tests.sites.fixture_site.app import CSRF, VALID_SID

DOMAIN = "fixture.test"
SIGNED_IN = {"sid": VALID_SID, "csrftoken": CSRF, "tracker": "t-1"}


def rotating_card(card: dict[str, Any]) -> dict[str, Any]:
    """The fixture card, but its sign-in check and list_orders hit endpoints that rotate the sign-in
    cookie."""
    card = copy.deepcopy(card)
    card["connect_providers"][0]["session_check"]["url"] = "{{base_url}}/api/rotating/me"
    orders = next(a for a in card["actions"] if a["name"] == "list_orders")
    orders["steps"][0]["request"]["url"] = "{{base_url}}/api/rotating/orders"
    return card


@pytest.fixture
def setup(
    tmp_path: Path, fixture_card: dict[str, Any]
) -> tuple[StoredCard, EncryptedSessionStore, Executor, str]:
    registry = Registry(tmp_path / "r.db")
    registry.save(rotating_card(fixture_card))
    stored = registry.get(DOMAIN)
    assert stored is not None
    store = EncryptedSessionStore(tmp_path / "r.db", new_session_key())
    provider = str(fixture_card["connect_providers"][0]["id"])
    store.put("default", DOMAIN, provider, SIGNED_IN)
    return stored, store, Executor(store, Confirmer("s")), provider


ROTATED_1 = {"sid": "sid-rotated-1", "csrftoken": CSRF, "seen": "1"}  # tracker deleted by the site


async def test_successful_action_saves_the_refreshed_cookies(setup: Any) -> None:
    stored, store, executor, provider = setup
    connected_at = store.record("default", DOMAIN, provider).connected_at

    assert (await executor.run(stored, "list_orders", {})).status == R.SUCCESS
    assert store.get("default", DOMAIN, provider) == ROTATED_1  # changed, new and deleted cookies mirrored
    record = store.record("default", DOMAIN, provider)
    assert record.status == "connected" and record.connected_at == connected_at  # not a new sign-in

    # The next run sends the rotated cookie, and saves the next one.
    assert (await executor.run(stored, "list_orders", {})).status == R.SUCCESS
    assert store.get("default", DOMAIN, provider)["sid"] == "sid-rotated-2"


async def test_failed_action_saves_nothing(setup: Any) -> None:
    stored, store, executor, provider = setup
    store.put("default", DOMAIN, provider, {"sid": "sid-wrong", "csrftoken": CSRF})
    assert (await executor.run(stored, "list_orders", {})).status != R.SUCCESS
    assert store.get("default", DOMAIN, provider) == {"sid": "sid-wrong", "csrftoken": CSRF}


async def test_passing_sign_in_check_saves_the_refreshed_cookies(setup: Any) -> None:
    stored, store, executor, provider = setup
    assert await executor.check_session(stored.card, provider, "default") is None
    assert store.get("default", DOMAIN, provider) == ROTATED_1


async def test_failing_sign_in_check_saves_nothing(setup: Any) -> None:
    stored, store, executor, provider = setup
    store.put("default", DOMAIN, provider, {"sid": "sid-wrong"})
    assert await executor.check_session(stored.card, provider, "default") == R.AUTH_EXPIRED
    assert store.get("default", DOMAIN, provider) == {"sid": "sid-wrong"}


async def test_login_flow_checks_with_their_own_cookies_save_nothing(setup: Any) -> None:
    stored, store, executor, provider = setup
    assert await executor.check_session(stored.card, provider, "default", cookies={"sid": VALID_SID}) is None
    assert store.get("default", DOMAIN, provider) == SIGNED_IN


async def test_keepalive_rotates_idle_sign_ins(setup: Any, tmp_path: Path) -> None:
    from runtime.signin.connect import ConnectionManager

    stored, store, executor, provider = setup
    registry = Registry(tmp_path / "r.db")
    manager = ConnectionManager(registry, store, executor, "http://gw")
    assert (await manager.keepalive_once()) == {f"default/{DOMAIN}/{provider}": "ok"}
    assert store.get("default", DOMAIN, provider) == ROTATED_1


async def test_dev_cookies_from_env_are_not_rotated(setup: Any, tmp_path: Path) -> None:
    import json

    stored, _, _, provider = setup
    encrypted = EncryptedSessionStore(tmp_path / "empty.db", new_session_key())
    dev = DevEnvSessionStore(json.dumps({DOMAIN: {provider: SIGNED_IN}}))
    executor = Executor(ChainedSessionStore(encrypted, dev), Confirmer("s"))
    assert (await executor.run(stored, "list_orders", {})).status == R.SUCCESS
    assert dev.get("default", DOMAIN, provider) == SIGNED_IN
    assert encrypted.get("default", DOMAIN, provider) is None  # .env sign-ins stay in .env


async def test_cookie_values_are_never_logged(setup: Any, caplog: pytest.LogCaptureFixture) -> None:
    stored, _, executor, _ = setup
    with caplog.at_level(logging.DEBUG, logger="runtime"):
        assert (await executor.run(stored, "list_orders", {})).status == R.SUCCESS
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("runtime"))
    assert "saved a refreshed sign-in" in ours
    for secret in (VALID_SID, "sid-rotated-1", CSRF):
        assert secret not in ours


# ---- the store: only replace what we started from ----------------------------------------------------------


def test_refresh_only_replaces_what_the_run_started_from(tmp_path: Path) -> None:
    for store in (EncryptedSessionStore(tmp_path / "s.db", new_session_key()), InMemorySessionStore()):
        store.put("u", DOMAIN, "p", {"sid": "a"})
        assert store.refresh("u", DOMAIN, "p", {"sid": "a"}, {"sid": "b"})
        assert store.get("u", DOMAIN, "p") == {"sid": "b"}
        # The user signed in again (or another run saved first) while this run was going: keep theirs.
        assert not store.refresh("u", DOMAIN, "p", {"sid": "a"}, {"sid": "c"})
        assert store.get("u", DOMAIN, "p") == {"sid": "b"}
        # Disconnected meanwhile: don't bring the sign-in back.
        assert not store.refresh("u", DOMAIN, "gone", {"sid": "b"}, {"sid": "d"})
        assert store.get("u", DOMAIN, "gone") is None


async def test_unchanged_or_empty_jars_are_not_written(setup: Any) -> None:
    _, store, executor, provider = setup
    calls: list[Any] = []
    real = store.refresh
    store.refresh = lambda *a: calls.append(a) or real(*a)  # type: ignore[method-assign]
    executor._save_refreshed("default", DOMAIN, provider, SIGNED_IN, dict(SIGNED_IN))
    executor._save_refreshed(
        "default", DOMAIN, provider, SIGNED_IN, {}
    )  # we saw no cookies: not "signed out"
    assert calls == [] and store.get("default", DOMAIN, provider) == SIGNED_IN


# ---- tiers: only the site's own cookies are saved ----------------------------------------------------------


@pytest.mark.parametrize("session_cls", [HttpSession, ImpersonateSession])
async def test_site_cookies_leave_out_other_sites(session_cls: Any) -> None:
    session = session_cls("https://www.shop.test/app", {"sid": "a"})
    try:
        session._client.cookies.set("pref", "1", domain=".shop.test")  # a parent-domain cookie the site sends
        session._client.cookies.set(
            "other", "x", domain="tracker.example"
        )  # set during a cross-site redirect
        assert session.site_cookies == {"sid": "a", "pref": "1"}
    finally:
        await session.aclose()


async def test_a_redirect_loop_on_the_sign_in_check_means_signed_out(fixture_card: dict[str, Any]) -> None:
    """LinkedIn bounces an ended session between /feed/ and /feed/: the check says expired, not a network
    error (which never marks a sign-in expired), so the account page and the agent stop calling it signed
    in."""
    card = copy.deepcopy(fixture_card)
    card["connect_providers"][0]["session_check"]["url"] = "{{base_url}}/api/loop"
    report = validate_card(card)
    assert report.ok and report.card is not None
    store = InMemorySessionStore()
    store.put("u", report.card.domain, "shop", {"sid": "whatever"})
    assert await Executor(store, Confirmer("s")).check_session(report.card, "shop", "u") == R.AUTH_EXPIRED
