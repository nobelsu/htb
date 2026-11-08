"""Working out a new card's sign-in check (flowmap/signin.py) against the bookshop's members area."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from flowmap.signin import SignInCheckError, _fill, infer_sign_in, signals_between
from runtime.run.confirm import Confirmer
from runtime.run.executor import Executor
from runtime.run.tiers import HttpResponse
from runtime.signin.sessions import InMemorySessionStore


def test_only_params_are_filled_in() -> None:
    params = {"query": "a b", "page": 2}
    assert _fill("{{base_url}}/s/{{params.query | urlencode}}", params) == "{{base_url}}/s/a%20b"
    assert _fill({"start": "{{params.page | offset:10}}", "n": "{{params.limit}}"}, params) == {"start": 10}
    assert _fill({"csrf": "{{session.cookies.JSESSIONID | strip_quotes}}"}, params) == {
        "csrf": "{{session.cookies.JSESSIONID | strip_quotes}}"
    }


def test_signals_are_what_only_the_signed_out_response_shows() -> None:
    signed_in = HttpResponse(200, {}, "<h1>Your orders</h1>", "http://shop/orders")
    signed_out = HttpResponse(
        200, {}, "<h1>Sign in</h1>", "http://shop/login?next=/orders", [(302, "/login?next=/orders")]
    )
    assert signals_between(signed_in, signed_out, "http://shop/login") == ["redirect:/login"]
    denied = HttpResponse(401, {}, "{}", "http://shop/api/orders")
    assert signals_between(
        HttpResponse(200, {}, "{}", "http://shop/api/orders"), denied, "http://shop/login"
    ) == ["status:401"]


def _card(base: str) -> dict[str, Any]:
    """A card whose only action is public, so the start page is the only candidate that can tell."""
    return {
        "schema_version": "0.4",
        "domain": "bookshop.test",
        "tool_prefix": "bookshop",
        "base_url": base,
        "blocked_signals": [],
        "provenance": {"mapped_at": "2026-11-08T00:00:00Z", "source": "test", "mapper_version": "t"},
        "connect_providers": [],
        "defaults": {"tier": "http"},
        "actions": [],
    }


async def _signed_in(bookshop: str) -> InMemorySessionStore:
    sessions = InMemorySessionStore()
    async with httpx.AsyncClient() as http:
        await http.post(bookshop + "/login", data={"username": "reader", "password": "reader-pass"})
        sessions.put("alice", "bookshop.test", "bookshop", {"member": http.cookies["member"]})
    return sessions


async def test_the_start_page_is_the_fallback(bookshop: str) -> None:
    executor = Executor(await _signed_in(bookshop), Confirmer("s"))
    login = bookshop + "/login?next=/orders"
    check = await infer_sign_in(
        _card(bookshop),
        "bookshop",
        login_url=login,
        start_url=bookshop + "/orders",
        user="alice",
        executor=executor,
    )
    assert check.strong and check.provider["session_check"]["url"] == "{{base_url}}/orders"
    assert check.provider["logged_out_signals"] == ["redirect:/login"]


async def test_a_check_that_cant_tell_is_weak(bookshop: str) -> None:
    executor = Executor(await _signed_in(bookshop), Confirmer("s"))
    check = await infer_sign_in(
        _card(bookshop),
        "bookshop",
        login_url=bookshop + "/login",
        start_url=bookshop + "/",
        user="alice",
        executor=executor,
    )
    assert not check.strong and check.report()["strength"] == "weak"  # the home page is public
    with pytest.raises(SignInCheckError):
        await infer_sign_in(
            _card(bookshop),
            "bookshop",
            login_url="x",
            start_url=bookshop + "/",
            user="bob",
            executor=executor,
        )


async def test_a_sign_in_that_lands_on_the_login_page_is_no_check(bookshop: str) -> None:
    """With a sign-in the site doesn't accept, the start page "passes" only by redirecting to the login page:
    that must not become the card's check."""
    sessions = InMemorySessionStore()
    sessions.put("alice", "bookshop.test", "bookshop", {"member": "revoked"})
    with pytest.raises(SignInCheckError):
        await infer_sign_in(
            _card(bookshop),
            "bookshop",
            login_url=bookshop + "/login?next=/orders",
            start_url=bookshop + "/orders",
            user="alice",
            executor=Executor(sessions, Confirmer("s")),
        )


async def test_a_login_page_that_is_the_start_page_rejects_nothing(bookshop: str) -> None:
    """A job that was signed in from the start has no login page of its own: the start page stands in, and
    a check ending there is fine (LinkedIn's search failed on this)."""
    executor = Executor(await _signed_in(bookshop), Confirmer("s"))
    check = await infer_sign_in(
        _card(bookshop),
        "bookshop",
        login_url=bookshop + "/orders",
        start_url=bookshop + "/orders",
        user="alice",
        executor=executor,
    )
    assert check.strong and check.provider["session_check"]["url"] == "{{base_url}}/orders"
