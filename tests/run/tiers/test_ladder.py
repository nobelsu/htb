"""The impersonate tier and BLOCKED escalation (CARD_SPEC.md "Escalating on BLOCKED")."""

import asyncio
from typing import Any

import httpx
import pytest

from spider.card import Envelope, ResultCode, load_card
from spider.run.confirm import Confirmer
from spider.run.executor import Executor
from spider.run.request.template import BuiltRequest
from spider.run.tiers.http import HttpTier, ImpersonateSession
from spider.signin.sessions.base import InMemorySessionStore
from spider.store.registry import StoredCard
from tests.conftest import hits

R = ResultCode


@pytest.fixture
def stored(fixture_card: dict[str, Any]) -> StoredCard:
    return StoredCard("fixture.test", 1, load_card(fixture_card), True, "now", None)


@pytest.fixture
def executor() -> Executor:
    return Executor(InMemorySessionStore(), Confirmer("s"))


async def confirmed(ex: Executor, stored: StoredCard, action: str, **args: Any) -> Envelope:
    first = await ex.run(stored, action, args)
    assert first.confirmation is not None, first
    return await ex.run(stored, action, {**args, "_confirmation_id": first.confirmation.id})


def cart(site_url: str, sku: str) -> dict[str, Any]:
    return httpx.get(f"{site_url}/api/cart/{sku}").json()


# ---- the impersonate tier itself -------------------------------------------


async def test_impersonate_session_looks_like_chrome(site_url: str) -> None:
    session = ImpersonateSession(site_url, {"seed": "v"})
    try:
        url = f"{site_url}/api/redirect-to-echo"
        resp = await session.send(BuiltRequest("GET", url, {"x-test": "1"}), 5)
        assert resp.status == 200 and resp.hops == [(302, "/api/echo?via=redirect")]
        echoed = httpx.Response(200, text=resp.text).json()
        assert "Chrome/" in echoed["headers"]["user-agent"] and "sec-ch-ua" in echoed["headers"]
        assert echoed["headers"]["x-test"] == "1"
        assert echoed["cookies"] == {"seed": "v", "hop": "1"}
        assert session.cookies == {"seed": "v", "hop": "1"}

        raw = f"{site_url}/api/echo?variables=(keywords:Palantir%2C%20Inc.,start:20)&x=a%26b"
        resp = await session.send(BuiltRequest("GET", raw, {}), 5)
        assert httpx.Response(200, text=resp.text).json()["url"] == raw  # sent exactly as encoded
    finally:
        await session.aclose()


# ---- reads -----------------------------------------------------------------


async def test_read_escalates_on_403_challenge(executor: Executor, stored: StoredCard, site_url: str) -> None:
    env = await executor.run(stored, "guarded_search", {"query": "lamp"})
    assert env.status == R.SUCCESS and env.meta.tier == "impersonate"
    assert env.data is not None and env.data[0]["name"] == "Desk lamp"
    assert hits(site_url) == {"GET /api/guarded/products": 2}


async def test_escalation_keeps_cookies_and_later_steps_stay_up(
    executor: Executor, stored: StoredCard, site_url: str
) -> None:
    env = await executor.run(stored, "guarded_flow", {})
    assert env.status == R.SUCCESS and env.meta.tier == "impersonate"
    assert env.data == [{"order_id": "o-guarded", "status": "done"}]
    # Step 1 ran once on http; only the blocked step 2 was retried, on impersonate, with step 1's cookie.
    assert hits(site_url) == {"GET /api/flow/start": 1, "GET /api/guarded/flow-finish": 2}


async def test_browser_tier_falls_back_to_impersonate(executor: Executor, stored: StoredCard) -> None:
    env = await executor.run(stored, "browser_tier_search", {"query": "lamp"})
    assert env.status == R.SUCCESS and env.meta.tier == "impersonate"


async def test_blocked_at_top_tier(executor: Executor, stored: StoredCard, site_url: str) -> None:
    env = await executor.run(stored, "challenge", {})
    assert env.status == R.BLOCKED and env.meta.tier == "impersonate" and env.heal is None
    assert env.error is not None and "tiers tried: http, impersonate" in env.error.message


async def test_http_only_executor_does_not_escalate(stored: StoredCard, site_url: str) -> None:
    ex = Executor(InMemorySessionStore(), Confirmer("s"), tiers={"http": HttpTier()})
    env = await ex.run(stored, "guarded_search", {"query": "lamp"})
    assert env.status == R.BLOCKED and env.meta.tier == "http"
    assert hits(site_url) == {"GET /api/guarded/products": 1}


# ---- non-idempotent writes -------------------------------------------------


async def test_write_blocked_without_2xx_is_resent_higher(
    executor: Executor, stored: StoredCard, site_url: str
) -> None:
    env = await confirmed(executor, stored, "guarded_add", sku="G-1")
    assert env.status == R.SUCCESS and env.meta.tier == "impersonate"
    assert hits(site_url)["POST /api/guarded/cart"] == 2
    assert cart(site_url, "G-1") == {"in_cart": True, "qty": 1}  # written exactly once


async def test_write_blocked_with_2xx_is_not_resent(
    executor: Executor, stored: StoredCard, site_url: str
) -> None:
    env = await confirmed(executor, stored, "guarded_add_soft", sku="S-1")
    assert env.status == R.BLOCKED and env.meta.tier == "http"
    assert env.error is not None and "not re-sent" in env.error.message
    assert hits(site_url)["POST /api/guarded/cart-soft"] == 1
    assert cart(site_url, "S-1") == {"in_cart": True, "qty": 1}  # the write landed once; no duplicate


async def test_block_before_write_step_escalates(
    executor: Executor, stored: StoredCard, site_url: str
) -> None:
    env = await confirmed(executor, stored, "guarded_token_add", sku="T-1")
    assert env.status == R.SUCCESS and env.meta.tier == "impersonate"
    h = hits(site_url)
    assert h["GET /api/guarded/token"] == 2 and h["POST /api/cart"] == 1
    assert cart(site_url, "T-1") == {"in_cart": True, "qty": 1}


async def test_ladder_respects_action_budget(stored: StoredCard) -> None:
    ex = Executor(InMemorySessionStore(), Confirmer("s"), action_cap_s=5)
    env = await asyncio.wait_for(ex.run(stored, "guarded_search", {"query": "lamp"}), 10)
    assert env.status == R.SUCCESS
