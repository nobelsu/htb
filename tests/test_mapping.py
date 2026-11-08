"""Map jobs (DECISIONS M8): who may ask, what a request does when the site already has a card, how jobs run
and what the runtime checks before saving a mapped card. The mapper is a stand-in that returns the fixture
shop's public reads; flowmap's real one is tested in tests/flowmap/test_server.py."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import mcp_types as types
import pytest

from runtime.confirm import Confirmer
from runtime.executor import Executor
from runtime.gateway import Gateway
from runtime.healing import HealConfig, HealManager
from runtime.mapping import MapConfig, MapError, MapManager, MapOutcome, MapRequest, SignInNeeded
from runtime.registry import Registry
from runtime.sessions import InMemorySessionStore
from runtime.state import StateStore
from tests.fixture_site.remapper import FixtureRemapper

DOMAIN = "fixture.test"


WORKING_READS = ("search_products", "get_product", "get_item")


def public_reads(card: dict[str, Any]) -> dict[str, Any]:
    """The fixture card with only its plain working reads (it also holds deliberately failing ones), as a
    fresh map would find."""
    out = copy.deepcopy(card)
    out["actions"] = [a for a in out["actions"] if a["name"] in WORKING_READS]
    return out


class StandIn:
    """A mapper that returns ``card`` (or raises ``error``) and records what it was asked."""

    def __init__(self, card: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.card, self.error = card, error
        self.requests: list[MapRequest] = []
        self.release = asyncio.Event()
        self.release.set()

    def domain_for(self, host: str) -> str:
        return DOMAIN

    async def map_site(self, request: MapRequest, progress: Callable[[str], None]) -> MapOutcome:
        self.requests.append(request)
        progress("doing the task")
        await self.release.wait()
        if self.error is not None:
            raise self.error
        assert self.card is not None
        return MapOutcome(card=copy.deepcopy(self.card), report={"confidence": 1.0}, log=["done"])


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "map.db"


@pytest.fixture
def registry(db: Path) -> Iterator[Registry]:
    reg = Registry(db)
    yield reg
    reg.close()


@pytest.fixture
def state(db: Path) -> Iterator[StateStore]:
    s = StateStore(db)
    yield s
    s.close()


@pytest.fixture
def executor() -> Executor:
    return Executor(InMemorySessionStore(), Confirmer("s"))


def maps(registry: Registry, state: StateStore, executor: Executor, mapper: Any, **kw: Any) -> MapManager:
    healer = HealManager(registry, state, executor, FixtureRemapper(), HealConfig(cooldown_s=0))
    return MapManager(
        registry, state, executor, mapper, healer, MapConfig(allow_local=True, poll_s=0.01, **kw)
    )


async def test_requests_are_checked(registry: Registry, state: StateStore, executor: Executor) -> None:
    strict = MapManager(registry, state, executor, StandIn(), config=MapConfig())
    for site in (
        "http://127.0.0.1:8900/",
        "localhost:3000",
        "http://10.1.2.3/",
        "http://printer.local/",
        "http://[::1]/",
    ):
        out = await strict.request("alice", site, "search things", "search_things")
        assert "private network" in out["error"], site
    assert "not a website" in (await strict.request("alice", "ftp://example.com", "x", "search"))["error"]
    lenient = maps(registry, state, executor, StandIn())
    assert "snake_case" in (await lenient.request("alice", "shop.test", "search", "Search Things"))["error"]
    assert "task" in (await lenient.request("alice", "shop.test", "   ", "search"))["error"]
    bad = await lenient.request("alice", "shop.test", "search", "search", {"query": {"nested": 1}})
    assert "inputs" in bad["error"]
    assert state.open_map_job_for(DOMAIN) is None  # nothing was queued


async def test_a_new_site_is_mapped_once_for_everyone(
    registry: Registry, state: StateStore, executor: Executor, fixture_card: dict[str, Any]
) -> None:
    mapper = StandIn(public_reads(fixture_card))
    m = maps(registry, state, executor, mapper)
    first = await m.request(
        "alice",
        "https://shop.test/search",
        "search products",
        "search_products",
        {"query": "widget", "in_stock": True},
    )
    assert first["status"] == "queued" and first["task"] == "search products" and first["retry_after_s"] == 30
    # Someone else asking for the same site waits on the same job, without seeing alice's task or values.
    second = await m.request("bob", "shop.test", "find a product", "find_product")
    assert second["job_id"] == first["job_id"] and "task" not in second and "someone else" in second["note"]

    await m.drain()
    request = mapper.requests[0]
    assert request.url == "https://shop.test/search" and request.inputs == {
        "query": "widget",
        "in_stock": "true",
    }
    assert request.user == "alice" and len(mapper.requests) == 1
    done = await m.status(first["job_id"], "alice")
    assert done is not None and done["status"] == "succeeded" and done["new_version"] == 1
    assert done["progress"] == "doing the task" and "find_actions" in done["hint"]
    stored = registry.get(DOMAIN)
    assert stored is not None and {a.name for a in stored.card.actions} == set(done["result"]["actions"])

    # Mapped: asking again points at find_actions instead of mapping again.
    again = await m.request("bob", "shop.test", "search products", "search_products")
    assert again["status"] == "exists" and "find_actions" in again["hint"]
    assert [j["job_id"] for j in m.jobs_for("alice")] == [first["job_id"]] and m.jobs_for("bob") == []


async def test_a_broken_site_is_healed_instead(
    registry: Registry, state: StateStore, executor: Executor, fixture_card: dict[str, Any]
) -> None:
    registry.save(fixture_card)
    m = maps(registry, state, executor, StandIn())
    state.set_degraded(DOMAIN, "get_item", True, "heal failed")
    out = await m.request("carol", "fixture.test", "look up an item", "get_item")
    assert out["status"] == "healing" and out["heal"]["job_id"].startswith("h_")
    job = state.get_job(out["heal"]["job_id"])
    assert job is not None and job.actions == ["get_item"] and job.user == "carol"
    assert state.open_map_job_for(DOMAIN) is None


@pytest.mark.parametrize(
    ("error", "status", "message"),
    [
        (SignInNeeded("fixture.test wants a sign-in for this", ["open"]), "needs_sign_in", "signed in"),
        (MapError("the agent gave up: no search box", ["open", "gave up"]), "failed", "no search box"),
    ],
)
async def test_mapping_failures(
    registry: Registry, state: StateStore, executor: Executor, error: Exception, status: str, message: str
) -> None:
    m = maps(registry, state, executor, StandIn(error=error))
    job = await m.request("alice", "shop.test", "search", "search_products")
    await m.drain()
    out = await m.status(job["job_id"], "alice")
    assert out is not None and out["status"] == status and message in out["user_message"]
    assert out["result"]["log"]
    other = await m.status(job["job_id"], "bob")
    assert other is not None and "no search box" not in other["user_message"]  # only the requester sees why
    assert registry.get(DOMAIN) is None


async def test_a_card_whose_examples_fail_is_not_saved(
    registry: Registry, state: StateStore, executor: Executor, fixture_card: dict[str, Any]
) -> None:
    card = public_reads(fixture_card)
    card["actions"][0]["examples"][0]["min_items"] = 999
    m = maps(registry, state, executor, StandIn(card))
    job = await m.request("alice", "shop.test", "search", "search_products")
    await m.drain()
    out = await m.status(job["job_id"], "alice")
    assert out is not None and out["status"] == "failed"
    assert card["actions"][0]["name"] in out["result"]["examples"]
    assert registry.get(DOMAIN) is None

    broken = public_reads(fixture_card)
    broken["actions"][0]["params"]["_reserved"] = {"type": "string", "required": False}
    m.mapper = StandIn(broken)
    job = await m.request("alice", "shop.test", "search", "search_products")
    await m.drain()
    out = await m.status(job["job_id"], "alice")
    assert out is not None and out["status"] == "failed" and "isn't valid" in out["result"]["error"]


async def test_status_waits_for_the_job(
    registry: Registry, state: StateStore, executor: Executor, fixture_card: dict[str, Any]
) -> None:
    mapper = StandIn(public_reads(fixture_card))
    mapper.release.clear()
    m = maps(registry, state, executor, mapper)
    job = await m.request("alice", "shop.test", "search", "search_products")
    worker = asyncio.create_task(m.run_forever())
    try:
        quick = await m.status(job["job_id"], "alice", wait_s=0.1)
        assert quick is not None and quick["status"] == "running"
        asyncio.get_running_loop().call_later(0.1, mapper.release.set)
        done = await m.status(job["job_id"], "alice", wait_s=5)
        assert done is not None and done["status"] == "succeeded"
    finally:
        worker.cancel()
    # A job left running by a restart fails instead of hanging.
    stuck = state.create_map_job("other.test", "https://other.test/", "t", "a", {}, "alice")
    assert state.start_map_job(stuck.id) and state.fail_orphaned_map_jobs() == 1
    after = state.get_map_job(stuck.id)
    assert after is not None and after.status == "failed"


async def test_gateway_map_tools(
    registry: Registry, state: StateStore, executor: Executor, fixture_card: dict[str, Any]
) -> None:
    m = maps(registry, state, executor, StandIn(public_reads(fixture_card)))
    gateway = Gateway(registry, executor, maps=m)
    names = [t.name for t in (await gateway._list_tools(None, None)).tools]
    assert "request_map" in names and "map_status" in names
    assert "request_map" not in [
        t.name for t in (await Gateway(registry, executor)._list_tools(None, None)).tools
    ]

    async def call(name: str, **args: Any) -> types.CallToolResult:
        result = await gateway._call_tool(None, types.CallToolRequestParams(name=name, arguments=args))
        assert isinstance(result, types.CallToolResult)
        return result

    bad = await call("request_map", site="shop.test", task="search")
    assert bad.is_error
    queued = await call(
        "request_map",
        site="shop.test",
        task="search products",
        action="search_products",
        inputs={"query": "widget"},
    )
    job_id = json.loads(queued.content[0].text)["job_id"]  # type: ignore[union-attr]
    await m.drain()
    done = await call("map_status", job_id=job_id, wait_seconds=0)
    assert not done.is_error and done.structured_content is not None
    assert done.structured_content["status"] == "succeeded"
    found = await call("find_actions", site=DOMAIN)
    assert "search_products" in found.content[0].text  # type: ignore[union-attr]
    assert (await call("map_status", job_id="m_nope")).is_error


async def test_the_account_page_lists_your_map_jobs(
    db: Path, registry: Registry, state: StateStore, executor: Executor
) -> None:
    import httpx

    from runtime.keys import KeyStore
    from runtime.oauth import OAuth
    from tests.conftest import LiveServer, free_port
    from tests.test_gateway import API_KEY

    m = maps(registry, state, executor, StandIn(error=MapError("no search box")))
    await m.request("alice", "first.test", "search", "search_first")
    await m.drain()
    held = StandIn()
    held.release.clear()  # the page's own worker picks the job up: keep it open
    held.domain_for = lambda host: host  # type: ignore[method-assign]
    m.mapper = held
    await m.request("alice", "second.test", "search", "search_second")
    await m.request("bob", "third.test", "search", "search_third")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    keys = KeyStore(db)
    keys.create("alice", "alice@example.com", "alice-password1")
    gateway = Gateway(registry, executor, maps=m)
    app = gateway.asgi_app(API_KEY, keys=keys, public_base_url=base, oauth=OAuth(db, base))
    with LiveServer(app, port=port):
        async with httpx.AsyncClient(follow_redirects=False) as http:
            login = {"action": "password", "login": "alice", "password": "alice-password1"}
            assert (await http.post(f"{base}/account/login", data=login)).status_code == 302
            page = (await http.get(f"{base}/account")).text
    assert "second.test" in page and ("Queued" in page or "Setting up" in page)
    assert DOMAIN in page and ("Didn&#x27;t work" in page or "Didn't work" in page)
    assert "third.test" not in page  # bob's
