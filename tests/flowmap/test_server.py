"""flowmap as the gateway's mapper: a map job on the bookshop, with a scripted stand-in for Claude, goes from
the request to a saved card that agents can call. Raw captures don't outlive the job."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from flowmap.flows import flow_path, load_flow
from flowmap.server import FlowMapper
from runtime.jobs.mapping import MapConfig, MapManager
from runtime.run.confirm import Confirmer
from runtime.run.executor import Executor
from runtime.signin.sessions import InMemorySessionStore
from runtime.store.registry import Registry
from runtime.store.state import StateStore
from schema import ResultCode
from tests.flowmap.conftest import search_flow
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

    from runtime.jobs.mapping import provider_for

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


@pytest.mark.browser
async def test_explore_maps_what_passes_and_follows_up(bookshop: str, tmp_path: Path) -> None:
    """An explore: round 1 proposes a search, a change (dropped before it runs) and a task the recorder
    gives up on (dropped); round 2 follows up with a book page, using an id the search returned."""
    pytest.importorskip("playwright.async_api")
    import re

    from tests.flowmap.test_agent import NONE, last_page
    from tests.sites.bookshop import state as state_site

    seen: dict[str, str] = {}

    def first_round(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert 'textbox "Search books"' in last_page(messages)  # it saw the start page
        return {
            "name": "propose_tasks",
            "input": {
                "tasks": [
                    {
                        "action": "search_books",
                        "task": 'Search the books for "harbour"',
                        "start_url": bookshop + "/",
                        "inputs": [{"name": "query", "value": "harbour"}],
                    },
                    {
                        "action": "add_to_cart",
                        "kind": "write",
                        "task": 'Add the book "b002" to the cart',
                        "start_url": bookshop + "/book/b002",
                        "inputs": [{"name": "book_id", "value": "b002"}],
                    },
                    {
                        "action": "delete_account",
                        "kind": "write",
                        "task": "Delete my account",
                        "start_url": bookshop + "/",
                        "inputs": [],
                    },
                    {
                        "action": "get_bestsellers",
                        "task": "Show this week's bestsellers",
                        "start_url": "https://elsewhere.example/",  # not this site: starts at the start page
                        "inputs": [],
                    },
                ]
            },
        }

    def follow_up(messages: list[dict[str, Any]]) -> dict[str, Any]:
        text = last_page(messages)
        assert "search_books" in text and "returned" in text
        seen["id"] = re.findall(r"b0\d\d", text)[0]  # copied from what the search returned
        return {
            "name": "propose_tasks",
            "input": {
                "tasks": [
                    {
                        "action": "get_book",
                        "task": f"Open the page of the book {seen['id']}",
                        "start_url": f"{bookshop}/book/{seen['id']}",
                        "inputs": [{"name": "book_id", "value": seen["id"]}],
                    }
                ]
            },
        }

    client = FakeClient(
        [
            first_round,
            # search_books
            {"name": "fill", "input": {"element": role("textbox", "Search books"), "text": "harbour"}},
            {"name": "press", "input": {"key": "Enter"}},
            done_with_headings(3),  # 4 results: no next page
            # add_to_cart: the click is held back
            {"name": "click", "input": {"element": role("button", "Add to cart")}},
            {"name": "done", "input": {"summary": "added", "results": [], "next_page": NONE}},
            # get_bestsellers
            {"name": "give_up", "input": {"reason": "there is no bestseller list"}},
            follow_up,
            # get_book
            done_with_headings(1),
        ]
    )
    executor = Executor(InMemorySessionStore(), Confirmer("s"))
    registry, state = Registry(tmp_path / "rt.db"), StateStore(tmp_path / "rt.db")
    mapper = BookshopMapper(
        executor, tmp_path / "flows", channel="chromium", pause_s=0, client=client, use_llm=False
    )
    maps = MapManager(registry, state, executor, mapper, config=MapConfig(allow_local=True))
    try:
        job = await maps.request("alice", bookshop + "/", "find books")
        assert job["status"] == "queued" and "Exploring" in job["user_message"]
        await maps.drain()
        out = await maps.status(job["job_id"], "alice")
        assert out is not None and out["status"] == "succeeded", out
        explored = {e["action"]: e for e in out["result"]["report"]["explored"]}
        assert set(explored) == {"search_books", "add_to_cart", "get_bestsellers", "get_book"}
        assert explored["add_to_cart"]["kept"], explored  # delete_account is never even tried
        assert state_site.carts == {}  # the write was held back every time: the site never got it
        assert explored["search_books"]["kept"] and explored["get_book"]["kept"]
        assert not explored["get_bestsellers"]["kept"] and "gave up" in explored["get_bestsellers"]["reason"]
        assert explored["get_book"]["depth"] == 1 and not client.script

        stored = registry.get("bookshop.test")
        assert stored is not None
        assert {a.name for a in stored.card.actions} == {"search_books", "add_to_cart", "get_book"}
        cart = stored.card.action("add_to_cart")
        assert cart is not None and cart.kind == "write" and cart.requires_confirmation
        assert cart.steps[-1].expect.content_type == "any"  # the site's real answer is unknown
        assert state.is_never_run("bookshop.test", "add_to_cart")
        env = await executor.run(stored, "get_book", {"book_id": "b005"}, user="bob")
        assert env.status == ResultCode.SUCCESS and env.data, env
        env = await executor.run(stored, "search_books", {"query": "garden"}, user="bob")
        assert env.status == ResultCode.SUCCESS and env.data
    finally:
        registry.close()
        state.close()


@pytest.mark.browser
async def test_explore_crawls_to_the_pages_results_link_to(bookshop: str, tmp_path: Path) -> None:
    """The author search's results link to book pages: the crawl opens one, Claude proposes what can be read
    there, and that's mapped one level down."""
    pytest.importorskip("playwright.async_api")
    import re

    from tests.flowmap.test_agent import last_page

    def start(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assert 'textbox "Search by author"' in last_page(messages)
        task = {
            "action": "search_by_author",
            "kind": "read",
            "task": 'Search the books by the author "Author D"',
            "start_url": bookshop + "/authors",
            "inputs": [{"name": "author", "value": "Author D"}],
        }
        return {"name": "propose_tasks", "input": {"tasks": [task]}}

    def book_page(messages: list[dict[str, Any]]) -> dict[str, Any]:
        text = last_page(messages)
        assert "results of search_by_author link to" in text
        url = re.search(r"This page: (\S+/book/(b\d+))", text)
        assert url is not None, text[:300]
        task = {
            "action": "get_book",
            "kind": "read",
            "task": f"Read the book {url.group(2)}",
            "start_url": url.group(1),
            "inputs": [{"name": "book_id", "value": url.group(2)}],
        }
        return {"name": "propose_tasks", "input": {"tasks": [task]}}

    client = FakeClient(
        [
            start,
            {"name": "fill", "input": {"element": role("textbox", "Search by author"), "text": "Author D"}},
            {"name": "press", "input": {"key": "Enter"}},
            done_with_headings(3),
            book_page,  # depth 1: the page search_by_author's results link to
            done_with_headings(1),
            {"name": "propose_tasks", "input": {"tasks": []}},  # get_book's results: nothing follows
        ]
    )
    executor = Executor(InMemorySessionStore(), Confirmer("s"))
    registry, state = Registry(tmp_path / "rt.db"), StateStore(tmp_path / "rt.db")
    mapper = BookshopMapper(
        executor, tmp_path / "flows", channel="chromium", pause_s=0, client=client, use_llm=False
    )
    maps = MapManager(registry, state, executor, mapper, config=MapConfig(allow_local=True))
    try:
        job = await maps.request("alice", bookshop + "/authors")
        await maps.drain()
        out = await maps.status(job["job_id"], "alice")
        assert out is not None and out["status"] == "succeeded", out
        explored = {e["action"]: e for e in out["result"]["report"]["explored"]}
        assert explored["search_by_author"]["depth"] == 0 and explored["get_book"]["depth"] == 1
        assert all(e["kept"] for e in explored.values()) and not client.script
        stored = registry.get("bookshop.test")
        assert stored is not None and {a.name for a in stored.card.actions} == {
            "search_by_author",
            "get_book",
        }
    finally:
        registry.close()
        state.close()


async def test_the_blocking_test_keeps_the_request_whose_absence_empties_the_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tracking ping ranked first still leaves the results there when blocked; the search, blocked, empties
    them: the search is the source. The page itself (a document) is never chosen by elimination."""
    from flowmap import server
    from flowmap.capture import Exchange, Run

    ping, search, page = (
        ("POST", "shop.test", "/track"),
        ("GET", "shop.test", "/api/search"),
        (
            "GET",
            "shop.test",
            "/results",
        ),
    )
    doc = Exchange(
        id=0, step=0, phase="flow", resource_type="document", method="GET", url="https://shop.test/results"
    )
    run = Run(
        run_id="r",
        session="s",
        domain="shop.test",
        action="a",
        kind="real",
        index=0,
        values={"q": "lamp"},
        markers={},
        started_at="",
        visible=["Desk lamp"],
        exchanges=[doc],
    )
    flow = search_flow("https://shop.test")
    blocked: list[tuple[str, str, str]] = []

    async def without(flow: Any, block: Any, **kw: Any) -> list[str]:
        sig = next(s for s in (ping, search, page) if block(s[0], "https://" + s[1] + s[2]))
        blocked.append(sig)
        return [] if sig == search else ["Desk lamp"]

    monkeypatch.setattr(server, "visible_without", without)
    mapper = FlowMapper(Executor(InMemorySessionStore(), Confirmer("s")), tmp_path)

    monkeypatch.setattr(server, "candidates", lambda f, r, **kw: [page, ping, search])
    assert await mapper._confirm_source(flow, [run], None, lambda line: None) == search  # type: ignore[arg-type]
    assert blocked == [ping, search]  # the page itself is never blocked

    # Nothing proves to be the source: no choice (scoring decides), never the page by elimination.
    monkeypatch.setattr(server, "candidates", lambda f, r, **kw: [page, ping])
    blocked.clear()
    assert await mapper._confirm_source(flow, [run], None, lambda line: None) is None  # type: ignore[arg-type]


@pytest.mark.browser
async def test_exploring_again_carries_on_from_the_actions_the_site_has(
    bookshop: str, tmp_path: Path
) -> None:
    """A site with just the author search: exploring again proposes nothing new on the start page, but the
    crawl starts from where the search's results lead (book pages) and adds get_book there."""
    pytest.importorskip("playwright.async_api")
    import re

    from tests.flowmap.test_agent import last_page

    def same_again(messages: list[dict[str, Any]]) -> dict[str, Any]:
        task = {
            "action": "search_by_author",
            "kind": "read",
            "task": 'Search the books by the author "Author D"',
            "start_url": bookshop + "/authors",
            "inputs": [{"name": "author", "value": "Author D"}],
        }
        return {"name": "propose_tasks", "input": {"tasks": [task]}}  # taken: dropped before it runs

    def book_page(messages: list[dict[str, Any]]) -> dict[str, Any]:
        url = re.search(r"This page: (\S+/book/(b\d+))", last_page(messages))
        assert url is not None
        task = {
            "action": "get_book",
            "kind": "read",
            "task": f"Read the book {url.group(2)}",
            "start_url": url.group(1),
            "inputs": [{"name": "book_id", "value": url.group(2)}],
        }
        return {"name": "propose_tasks", "input": {"tasks": [task]}}

    client = FakeClient(
        [
            # the first map: just the author search
            {"name": "fill", "input": {"element": role("textbox", "Search by author"), "text": "Author D"}},
            {"name": "press", "input": {"key": "Enter"}},
            done_with_headings(3),
            # exploring again
            same_again,
            book_page,
            done_with_headings(1),
            {"name": "propose_tasks", "input": {"tasks": []}},
        ]
    )
    executor = Executor(InMemorySessionStore(), Confirmer("s"))
    registry, state = Registry(tmp_path / "rt.db"), StateStore(tmp_path / "rt.db")
    mapper = BookshopMapper(
        executor, tmp_path / "flows", channel="chromium", pause_s=0, client=client, use_llm=False
    )
    maps = MapManager(registry, state, executor, mapper, config=MapConfig(allow_local=True))
    try:
        first = await maps.request(
            "alice",
            bookshop + "/authors",
            "search books by author",
            "search_by_author",
            {"author": "Author D"},
        )
        await maps.drain()
        assert (await maps.status(first["job_id"], "alice") or {}).get("status") == "succeeded"
        again = await maps.request("alice", bookshop + "/authors")
        await maps.drain()
        out = await maps.status(again["job_id"], "alice")
        assert out is not None and out["status"] == "succeeded", out
        assert out["result"]["added"] == ["get_book"] and out["new_version"] == 2
        assert not client.script
    finally:
        registry.close()
        state.close()


@pytest.mark.browser
async def test_a_named_write_is_held_back_and_saved_never_run(bookshop: str, tmp_path: Path) -> None:
    """request_map with write: the agent clicks the committing button, the site never gets it, and the
    action is saved as a write that asks for confirmation and is marked never run for real."""
    pytest.importorskip("playwright.async_api")
    from tests.flowmap.test_agent import NONE
    from tests.sites.bookshop import state as state_site

    client = FakeClient(
        [
            # done before doing anything: refused, since nothing was held back yet
            {"name": "done", "input": {"summary": "added", "results": [], "next_page": NONE}},
            {"name": "click", "input": {"element": role("button", "Add to cart")}},
            {"name": "done", "input": {"summary": "added", "results": [], "next_page": NONE}},
        ]
    )
    executor = Executor(InMemorySessionStore(), Confirmer("s"))
    registry, state = Registry(tmp_path / "rt.db"), StateStore(tmp_path / "rt.db")
    mapper = BookshopMapper(
        executor, tmp_path / "flows", channel="chromium", pause_s=0, client=client, use_llm=False
    )
    maps = MapManager(registry, state, executor, mapper, config=MapConfig(allow_local=True))
    carts = dict(state_site.carts)
    try:
        job = await maps.request(
            "alice",
            bookshop + "/book/b002",
            "add the book to the cart",
            "add_to_cart",
            {"book_id": "b002"},
            write=True,
        )
        assert job["status"] == "queued" and job["write"], job
        await maps.drain()
        out = await maps.status(job["job_id"], "alice")
        assert out is not None and out["status"] == "succeeded", out
        assert state_site.carts == carts  # held back every time
        stored = registry.get("bookshop.test")
        assert stored is not None
        cart = stored.card.action("add_to_cart")
        assert cart is not None and cart.kind == "write" and cart.requires_confirmation
        assert state.is_never_run("bookshop.test", "add_to_cart")
        assert out["result"]["report"]["turns"] == 3 and not client.script  # the early done was refused
        env = await executor.run(stored, "add_to_cart", {"book_id": "b003"}, user="bob")
        assert env.status == ResultCode.NEEDS_CONFIRMATION, env  # nothing sent without the user's yes
        assert state_site.carts == carts
    finally:
        registry.close()
        state.close()


async def test_a_named_action_that_is_never_mapped_or_reads_like_a_change(tmp_path: Path) -> None:
    """Payments, deleting and settings are refused as named actions too; change wording is a write."""
    from flowmap.explore import NEVER, changes

    assert NEVER.search("delete my account") and NEVER.search("pay invoice")
    assert changes("send a connect request") and changes("follow a company")
    assert not changes("search people")
