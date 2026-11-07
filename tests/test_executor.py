"""Integration tests against the fixture site: every result code, plus probe, retry and redaction rules."""

import json
import logging
from typing import Any

import httpx
import pytest

from runtime.confirm import Confirmer
from runtime.executor import Executor
from runtime.registry import StoredCard
from runtime.sessions import InMemorySessionStore
from schema import Envelope, Heal, ResultCode, load_card
from tests.conftest import hits
from tests.fixture_site.app import CSRF, EXPIRED_SID, VALID_SID

R = ResultCode
SECRET = "test-secret"


@pytest.fixture
def stored(fixture_card: dict[str, Any]) -> StoredCard:
    return StoredCard("fixture.test", 3, load_card(fixture_card), True, "now", None)


@pytest.fixture
def sessions() -> InMemorySessionStore:
    return InMemorySessionStore()


@pytest.fixture
def executor(sessions: InMemorySessionStore) -> Executor:
    return Executor(sessions, Confirmer(SECRET))


async def call(ex: Executor, stored: StoredCard, action: str, **args: Any) -> Envelope:
    return await ex.run(stored, action, args)


def login(sessions: InMemorySessionStore, sid: str = VALID_SID, csrf: str | None = CSRF) -> None:
    cookies = {"sid": sid}
    if csrf is not None:
        cookies["csrftoken"] = csrf
    sessions.put("default", "fixture.test", "shop", cookies)


# ---- SUCCESS / EMPTY -------------------------------------------------------


async def test_success_with_projection_and_next_params(executor: Executor, stored: StoredCard) -> None:
    env = await call(executor, stored, "search_products", query="lamp")
    assert env.status == R.SUCCESS
    assert env.data is not None and len(env.data) == 5
    assert env.data[0] == {"id": "p1", "sku": "SKU-001", "name": "Desk lamp", "price": 11, "colour": None}
    assert env.next_params == {"query": "lamp", "page": 2}
    assert env.error is None and env.heal is None and env.confirmation is None
    assert env.meta.tier == "http" and env.meta.map_version == 3 and env.meta.run_id.startswith("r_")
    assert not env.meta.cached and not env.meta.degraded


async def test_last_page_has_no_next_params(executor: Executor, stored: StoredCard, site_url: str) -> None:
    env = await call(executor, stored, "search_products", query="lamp", page=2)
    assert env.status == R.SUCCESS and env.data is not None and len(env.data) == 5
    assert env.data[0]["name"] == "Wall lamp"
    assert env.next_params is None


async def test_empty_list(executor: Executor, stored: StoredCard) -> None:
    env = await call(executor, stored, "search_products", query="zzz")
    assert env.status == R.EMPTY and env.data == [] and env.error is None and env.next_params is None


async def test_two_step_success(executor: Executor, stored: StoredCard) -> None:
    env = await call(executor, stored, "get_product", sku="SKU-002")
    assert env.status == R.SUCCESS
    assert env.data == [{"id": "p2", "sku": "SKU-002", "name": "Floor lamp", "price": 12, "colour": None}]


async def test_unknown_item_zero_match_is_empty(executor: Executor, stored: StoredCard, site_url: str) -> None:
    env = await call(executor, stored, "get_product", sku="SKU-NOPE")
    assert env.status == R.EMPTY and env.data == [] and env.error is None
    assert hits(site_url) == {"GET /api/lookup": 1}  # remaining steps were not run


async def test_two_matches_is_page_changed(executor: Executor, stored: StoredCard) -> None:
    env = await call(executor, stored, "get_product", sku="SKU-DUP")
    assert env.status == R.PAGE_CHANGED
    assert env.error is not None and env.error.step == "lookup" and "2 matches" in env.error.message
    assert env.heal is None  # healing not built yet


async def test_cookie_set_in_step_one_is_sent_in_step_two(executor: Executor, stored: StoredCard) -> None:
    env = await call(executor, stored, "cookie_flow")
    assert env.status == R.SUCCESS
    assert env.data == [{"order_id": "o-flow", "status": "done"}]


# ---- BAD_PARAMS ------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "query: required"),
        ({"query": 5}, "query: must be a string"),
        ({"query": "x", "page": "2"}, "page: must be an integer"),
        ({"query": "x", "color": "red"}, "color: unknown param"),
        ({"query": "x", "_confirmation_id": "c_x"}, "_confirmation_id: this action does not take a confirmation id"),
    ],
)
async def test_bad_params(
    executor: Executor, stored: StoredCard, site_url: str, args: dict[str, Any], message: str
) -> None:
    env = await executor.run(stored, "search_products", args)
    assert env.status == R.BAD_PARAMS
    assert env.error is not None and message in env.error.message and env.error.step is None
    assert hits(site_url) == {}  # nothing was sent


async def test_bad_enum(executor: Executor, stored: StoredCard) -> None:
    env = await call(executor, stored, "changed_structure", mode="xml")
    assert env.status == R.BAD_PARAMS
    assert env.error is not None and "mode: must be one of" in env.error.message


# ---- auth ------------------------------------------------------------------


async def test_auth_required(executor: Executor, stored: StoredCard, site_url: str) -> None:
    env = await call(executor, stored, "list_orders")
    assert env.status == R.AUTH_REQUIRED
    assert env.error is not None and env.error.hint is not None and "start_connection" in env.error.hint
    assert hits(site_url) == {}


async def test_logged_in_success(executor: Executor, stored: StoredCard, sessions: InMemorySessionStore) -> None:
    login(sessions)
    env = await call(executor, stored, "list_orders")
    assert env.status == R.SUCCESS
    assert env.data == [{"id": "o1", "total": 30}, {"id": "o2", "total": 12}]


async def test_auth_expired_on_401(executor: Executor, stored: StoredCard, sessions: InMemorySessionStore) -> None:
    login(sessions, sid="sid-revoked")
    env = await call(executor, stored, "list_orders")
    assert env.status == R.AUTH_EXPIRED
    assert env.error is not None and env.error.step == "orders" and "reconnect" in (env.error.hint or "")


async def test_auth_expired_on_login_redirect(
    executor: Executor, stored: StoredCard, sessions: InMemorySessionStore
) -> None:
    login(sessions, sid=EXPIRED_SID)
    env = await call(executor, stored, "list_orders")
    assert env.status == R.AUTH_EXPIRED
    assert env.error is not None and "redirect:/login" in env.error.message


async def test_auth_expired_when_cookie_missing(
    executor: Executor, stored: StoredCard, sessions: InMemorySessionStore, site_url: str
) -> None:
    login(sessions, csrf=None)
    env = await call(executor, stored, "list_orders")
    assert env.status == R.AUTH_EXPIRED
    assert env.error is not None and "csrftoken" in env.error.message
    assert hits(site_url) == {}


async def test_401_without_auth_provider_is_error(executor: Executor, stored: StoredCard) -> None:
    env = await call(executor, stored, "private_report")
    assert env.status == R.ERROR
    assert env.error is not None and "no login configured" in env.error.message


# ---- confirmation and writes -----------------------------------------------


async def test_needs_confirmation_then_success(executor: Executor, stored: StoredCard, site_url: str) -> None:
    first = await call(executor, stored, "add_to_cart", sku="SKU-001", qty=2)
    assert first.status == R.NEEDS_CONFIRMATION and first.confirmation is not None
    assert "Add a product to the cart." in first.confirmation.summary
    assert '"qty":2' in first.confirmation.summary
    assert hits(site_url) == {}  # nothing sent
    second = await call(executor, stored, "add_to_cart", sku="SKU-001", qty=2, _confirmation_id=first.confirmation.id)
    assert second.status == R.SUCCESS and second.data is None
    assert httpx.get(f"{site_url}/api/cart/SKU-001").json() == {"in_cart": True, "qty": 2}


async def test_confirmation_with_changed_params_is_bad_params(executor: Executor, stored: StoredCard) -> None:
    first = await call(executor, stored, "add_to_cart", sku="SKU-001")
    assert first.confirmation is not None
    env = await call(executor, stored, "add_to_cart", sku="SKU-002", _confirmation_id=first.confirmation.id)
    assert env.status == R.BAD_PARAMS
    assert env.error is not None and "does not match" in env.error.message


async def test_default_applied_before_confirmation(executor: Executor, stored: StoredCard) -> None:
    # qty defaults to 1, so passing it explicitly is the same canonical params.
    first = await call(executor, stored, "add_to_cart", sku="SKU-003")
    assert first.confirmation is not None
    env = await call(executor, stored, "add_to_cart", sku="SKU-003", qty=1, _confirmation_id=first.confirmation.id)
    assert env.status == R.SUCCESS


async def confirmed(ex: Executor, stored: StoredCard, **args: Any) -> Envelope:
    first = await ex.run(stored, "add_to_cart", args)
    assert first.confirmation is not None
    return await ex.run(stored, "add_to_cart", {**args, "_confirmation_id": first.confirmation.id})


async def test_write_5xx_is_unknown_outcome_never_retried(
    executor: Executor, stored: StoredCard, site_url: str
) -> None:
    env = await confirmed(executor, stored, sku="FAIL-1")
    assert env.status == R.UNKNOWN_OUTCOME
    assert env.error is not None and env.error.step == "add" and "verify:" in env.error.message
    assert hits(site_url)["POST /api/cart"] == 1


async def test_write_timeout_then_verify_passes_is_success(
    executor: Executor, stored: StoredCard, site_url: str
) -> None:
    env = await confirmed(executor, stored, sku="SLOW-1")
    assert env.status == R.SUCCESS
    assert hits(site_url)["POST /api/cart"] == 1


async def test_verify_failed(executor: Executor, stored: StoredCard) -> None:
    env = await confirmed(executor, stored, sku="GHOST-1")
    assert env.status == R.VERIFY_FAILED
    assert env.error is not None and env.error.step == "verify" and "$.in_cart" in env.error.message


# ---- rate limit / blocked --------------------------------------------------


async def test_rate_limited(executor: Executor, stored: StoredCard, site_url: str) -> None:
    env = await call(executor, stored, "rate_limited")
    assert env.status == R.RATE_LIMITED
    assert env.error is not None and env.error.retry_after_s == 7 and env.error.step == "limited"
    assert hits(site_url) == {"GET /api/limited": 1}


async def test_blocked_by_challenge_page(executor: Executor, stored: StoredCard) -> None:
    env = await call(executor, stored, "challenge")
    assert env.status == R.BLOCKED
    assert env.error is not None and "body_contains:cf-challenge" in env.error.message and env.heal is None


async def test_blocked_by_status(executor: Executor, stored: StoredCard, site_url: str) -> None:
    env = await call(executor, stored, "forbidden")
    assert env.status == R.BLOCKED and env.meta.tier == "impersonate"
    assert env.error is not None and "tiers tried: http, impersonate" in env.error.message
    assert hits(site_url) == {"GET /api/forbidden": 2}  # one per tier; not probed, not 5xx-retried


# ---- ERROR: 5xx / timeout --------------------------------------------------


async def test_5xx_retried_once_then_success(executor: Executor, stored: StoredCard, site_url: str) -> None:
    env = await call(executor, stored, "flaky_status", key="a", fail=1)
    assert env.status == R.SUCCESS and env.data == [{"ok": True, "attempt": 2}]


async def test_5xx_twice_is_error(executor: Executor, stored: StoredCard, site_url: str) -> None:
    env = await call(executor, stored, "flaky_status", key="b", fail=5)
    assert env.status == R.ERROR
    assert env.error is not None and env.error.step == "flaky" and "after one retry" in env.error.message
    assert hits(site_url) == {"GET /api/flaky": 2}


async def test_timeout_is_error_after_retry(executor: Executor, stored: StoredCard, site_url: str) -> None:
    env = await call(executor, stored, "slow_status", ms=1000)
    assert env.status == R.ERROR
    assert env.error is not None and "timeout" in env.error.message and env.error.step == "slow"
    assert hits(site_url) == {"GET /api/slow": 2}
    assert env.meta.ms < 1000


async def test_action_cap(sessions: InMemorySessionStore, stored: StoredCard) -> None:
    ex = Executor(sessions, Confirmer(SECRET), action_cap_s=0.4)
    env = await ex.run(stored, "slow_status", {"ms": 1000})
    assert env.status == R.ERROR
    assert env.meta.ms < 600


# ---- 4xx: changed_signals and the probe ------------------------------------


async def test_changed_signal_is_page_changed(executor: Executor, stored: StoredCard, site_url: str) -> None:
    env = await call(executor, stored, "legacy_search")
    assert env.status == R.PAGE_CHANGED
    assert env.error is not None and "PersistedQueryNotFound" in env.error.message
    assert hits(site_url) == {"GET /api/gql": 1}  # no probe needed


async def test_404_with_working_example_is_error(executor: Executor, stored: StoredCard, site_url: str) -> None:
    env = await call(executor, stored, "get_item", id="nope")
    assert env.status == R.ERROR
    assert env.error is not None and "probably bad" in env.error.message and env.error.step == "item"
    assert hits(site_url) == {"GET /api/items/nope": 1, "GET /api/items/p1": 1}


async def test_400_bad_input_is_error(executor: Executor, stored: StoredCard) -> None:
    env = await call(executor, stored, "search_products", query="lamp", page=0)
    assert env.status == R.ERROR
    assert env.error is not None and "HTTP 400" in env.error.message


async def test_unknown_route_after_break_is_page_changed(
    executor: Executor, stored: StoredCard, site_url: str
) -> None:
    httpx.post(f"{site_url}/_admin/break")
    env = await call(executor, stored, "get_item", id="p2")
    assert env.status == R.PAGE_CHANGED
    assert env.error is not None and "same HTTP 404" in env.error.message
    assert hits(site_url) == {"GET /api/items/p2": 1, "GET /api/items/p1": 1}  # exactly one probe


async def test_healing_hooks_for_page_changed(sessions: InMemorySessionStore, stored: StoredCard) -> None:
    calls: list[tuple[str, str]] = []

    class Hooks:
        def page_changed(self, domain: str, action: str, reason: str, user: str) -> Heal | None:
            calls.append((domain, action))
            return Heal(job_id="h_1", retry_after_s=30)

        def record_result(self, domain: str, action: str, code: ResultCode, user: str) -> None:
            return None

        def is_degraded(self, domain: str, action: str) -> bool:
            return True

    ex = Executor(sessions, Confirmer(SECRET), healing=Hooks())
    env = await ex.run(stored, "legacy_search", {})
    assert env.heal == Heal(job_id="h_1", retry_after_s=30)
    assert env.meta.degraded is True
    assert calls == [("fixture.test", "legacy_search")]


# ---- PAGE_CHANGED on 2xx ---------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "fragment"),
    [("structure", "missing ['$.results']"), ("html", "content type 'text/html"), ("status", "unexpected HTTP 204")],
)
async def test_changed_structure(executor: Executor, stored: StoredCard, mode: str, fragment: str) -> None:
    env = await call(executor, stored, "changed_structure", mode=mode)
    assert env.status == R.PAGE_CHANGED
    assert env.error is not None and fragment in env.error.message and env.error.step == "list"


# ---- redaction -------------------------------------------------------------


async def test_cookie_values_never_logged_or_returned(
    executor: Executor, stored: StoredCard, sessions: InMemorySessionStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    login(sessions, sid="sid-revoked-secret")
    env = await call(executor, stored, "list_orders")
    assert env.status == R.AUTH_EXPIRED
    login(sessions)
    ok = await call(executor, stored, "list_orders")
    assert ok.status == R.SUCCESS
    dumped = json.dumps([env.model_dump(mode="json"), ok.model_dump(mode="json")])
    for secret in ("sid-revoked-secret", VALID_SID, CSRF):
        assert secret not in dumped
        assert secret not in caplog.text
    assert "x-csrf': '[redacted]'" in caplog.text
