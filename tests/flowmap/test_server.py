"""flowmap as the gateway's mapper: a map job on the bookshop, with a scripted stand-in for Claude, goes from
the request to a saved card that agents can call. Raw captures don't outlive the job."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from flowmap.flows import flow_path, load_flow
from flowmap.server import FlowMapper
from runtime.confirm import Confirmer
from runtime.executor import Executor
from runtime.mapping import MapConfig, MapManager
from runtime.registry import Registry
from runtime.sessions import InMemorySessionStore
from runtime.state import StateStore
from schema import ResultCode
from tests.flowmap.test_agent import FakeClient, done_with_headings, role


class BookshopMapper(FlowMapper):
    def domain_for(self, host: str) -> str:
        return "bookshop.test"  # the test server is on 127.0.0.1


@pytest.mark.browser
async def test_a_map_job_maps_the_bookshop(
    bookshop: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("playwright.async_api")
    made: list[str] = []
    real = tempfile.TemporaryDirectory

    def recorded(*args: Any, **kwargs: Any) -> Any:
        folder = real(*args, **kwargs)
        made.append(folder.name)
        return folder

    monkeypatch.setattr(tempfile, "TemporaryDirectory", recorded)

    client = FakeClient(
        [
            {"name": "fill", "input": {"element": role("textbox", "Search books"), "text": "the"}},
            {"name": "press", "input": {"key": "Enter"}},
            done_with_headings(3, role("button", "Next")),
        ]
    )
    executor = Executor(InMemorySessionStore(), Confirmer("s"))
    registry, state = Registry(tmp_path / "rt.db"), StateStore(tmp_path / "rt.db")
    mapper = BookshopMapper(
        executor, tmp_path / "flows", channel="chromium", pause_s=0, client=client, use_llm=False
    )
    maps = MapManager(registry, state, executor, mapper, config=MapConfig(allow_local=True))
    try:
        job = await maps.request(
            "alice", bookshop + "/", "search the books by keyword", "search_books", {"query": "the"}
        )
        assert job["status"] == "queued"
        await maps.drain()
        out = await maps.status(job["job_id"], "alice")
        assert out is not None, job
        assert out["status"] == "succeeded", out
        report = out["result"]["report"]
        assert report["turns"] == 3 and report["confidence"] >= 0.8 and set(report["ms"]) == {"record", "map"}

        flow = load_flow(flow_path(tmp_path / "flows", "bookshop.test", "search_books"))
        assert flow.inputs["query"].marker  # kept for heal
        assert made and not any(Path(m).exists() for m in made)  # raw captures deleted

        stored = registry.get("bookshop.test")
        assert stored is not None and stored.version == 1
        action = stored.card.action("search_books")
        assert action is not None and "query" in action.params
        env = await executor.run(stored, "search_books", {"query": "harbour"}, user="bob")
        assert env.status == ResultCode.SUCCESS and env.data
    finally:
        registry.close()
        state.close()


@pytest.mark.browser
async def test_a_map_job_behind_a_sign_in(bookshop: str, tmp_path: Path) -> None:
    """The members area: the job stops for a sign-in, carries on once the requester has one, maps signed in,
    and the card gets a sign-in check worked out from the mapped request."""
    pytest.importorskip("playwright.async_api")
    import httpx

    from runtime.mapping import provider_for

    client = FakeClient(
        [
            {"name": "sign_in_needed", "input": {"reason": "the orders page wants a sign-in"}},
            {"name": "give_up", "input": {"reason": "needs a sign-in"}},
            # signed in, the second time
            {"name": "fill", "input": {"element": role("textbox", "Search your orders"), "text": "the"}},
            {"name": "press", "input": {"key": "Enter"}},
            done_with_headings(3),
        ]
    )
    sessions = InMemorySessionStore()
    executor = Executor(sessions, Confirmer("s"))
    registry, state = Registry(tmp_path / "rt.db"), StateStore(tmp_path / "rt.db")
    mapper = BookshopMapper(
        executor, tmp_path / "flows", channel="chromium", pause_s=0, client=client, use_llm=False
    )
    maps = MapManager(registry, state, executor, mapper, config=MapConfig(allow_local=True))
    try:
        job = await maps.request(
            "alice", bookshop + "/orders", "search my orders", "search_orders", {"query": "the"}
        )
        await maps.drain()
        out = await maps.status(job["job_id"], "alice")
        assert out is not None and out["status"] == "needs_sign_in", out
        assert out["result"]["login_url"] == bookshop + "/login?next=/orders"  # the page that asked

        # The requester signs in (through Connect in the gateway; here, straight to the store).
        async with httpx.AsyncClient() as http:
            await http.post(bookshop + "/login", data={"username": "reader", "password": "reader-pass"})
            member = http.cookies["member"]
        provider = provider_for("bookshop.test")
        sessions.put("alice", "bookshop.test", provider, {"member": member})
        maps._signed_in("alice", "bookshop.test", provider)
        await maps.drain()

        out = await maps.status(job["job_id"], "alice")
        assert out is not None and out["status"] == "succeeded", out
        sign_in = out["result"]["report"]["sign_in"]
        assert sign_in["strength"] == "strong" and "/api/orders" in sign_in["check"]
        stored = registry.get("bookshop.test")
        assert stored is not None
        card = stored.card
        assert [p.id for p in card.connect_providers] == ["bookshop"]
        check = card.connect_providers[0]
        assert (
            check.login_url == bookshop + "/login?next=/orders" and "status:401" in check.logged_out_signals
        )
        assert member not in stored.card.model_dump_json()  # no cookie values in the card
        action = card.action("search_orders")
        assert action is not None and action.auth_provider == "bookshop"

        env = await executor.run(stored, "search_orders", {"query": "harbour"}, user="alice")
        assert env.status == ResultCode.SUCCESS and env.data
        assert (await executor.run(stored, "search_orders", {"query": "x"}, user="bob")).status == (
            ResultCode.AUTH_REQUIRED
        )
        assert await executor.check_session(card, "bookshop", "alice") is None
        assert await executor.check_session(card, "bookshop", "alice", cookies={}) == ResultCode.AUTH_EXPIRED
    finally:
        registry.close()
        state.close()
