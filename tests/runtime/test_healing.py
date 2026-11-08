"""Healing against the live fixture shop: break -> PAGE_CHANGED -> heal job -> new version -> works again."""

import copy
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from runtime.jobs.healing import HealConfig, HealManager, RemapError
from runtime.run.confirm import Confirmer
from runtime.run.executor import Executor
from runtime.signin.sessions import InMemorySessionStore
from runtime.store.registry import Registry
from runtime.store.state import StateStore
from schema import Card, ResultCode
from tests.conftest import hits
from tests.sites.fixture_site.remapper import FixtureRemapper

R = ResultCode
DOMAIN = "fixture.test"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "heal.db"


@pytest.fixture
def registry(db: Path, fixture_card: dict[str, Any]) -> Iterator[Registry]:
    reg = Registry(db)
    reg.save(fixture_card)
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


def manager(
    registry: Registry, state: StateStore, executor: Executor, remapper: Any = None, **cfg: Any
) -> HealManager:
    return HealManager(registry, state, executor, remapper or FixtureRemapper(), HealConfig(**cfg))


async def call(registry: Registry, executor: Executor, action: str, **args: Any) -> Any:
    stored = registry.get(DOMAIN)
    assert stored is not None
    return await executor.run(stored, action, args)


def item_url(registry: Registry) -> str:
    stored = registry.get(DOMAIN)
    assert stored is not None
    action = stored.card.action("get_item")
    assert action is not None
    return action.steps[0].request.url


# ---- the demo loop ---------------------------------------------------------


async def test_break_heal_and_heal_back(
    registry: Registry, state: StateStore, executor: Executor, site_url: str
) -> None:
    healer = manager(registry, state, executor)
    httpx.post(f"{site_url}/_admin/break")

    broken = await call(registry, executor, "get_item", id="p2")
    assert broken.status == R.PAGE_CHANGED
    assert broken.heal is not None and broken.heal.retry_after_s == 30
    again = await call(registry, executor, "get_item", id="p3")
    assert again.heal is not None and again.heal.job_id == broken.heal.job_id  # joins the open job

    status = healer.heal_status(broken.heal.job_id)
    assert status is not None and status["status"] == "queued" and status["retry_after_s"] == 30

    await healer.drain()
    done = healer.heal_status(broken.heal.job_id)
    assert done is not None and done["status"] == "succeeded", done
    assert done["new_version"] == 2 and done["result"]["healed"] == ["get_item"]
    assert "/api/v2/items/" in item_url(registry)

    fixed = await call(registry, executor, "get_item", id="p2")
    assert fixed.status == R.SUCCESS and fixed.data[0]["name"] == "Floor lamp"
    assert fixed.meta.map_version == 2 and fixed.meta.degraded is False
    stored = registry.get(DOMAIN)
    assert stored is not None and stored.card.provenance.source == "fixture_remapper"
    # Unchanged actions are copied as-is.
    assert stored.card.action("search_products") == Card.model_validate(
        registry.get(DOMAIN, 1).card.to_json_dict()
    ).action("search_products")  # type: ignore[union-attr]

    # Undo the break: the healed mapping is now the broken one, and heals back.
    httpx.post(f"{site_url}/_admin/reset")
    back = await call(registry, executor, "get_item", id="p2")
    assert back.status == R.PAGE_CHANGED and back.heal is not None
    assert back.heal.job_id != broken.heal.job_id
    await healer.drain()
    assert "/api/items/" in item_url(registry) and "/v2/" not in item_url(registry)
    assert (await call(registry, executor, "get_item", id="p2")).status == R.SUCCESS


# ---- pre-check -------------------------------------------------------------


async def test_precheck_cancels_on_blocked(registry: Registry, state: StateStore, executor: Executor) -> None:
    healer = manager(registry, state, executor)
    heal = healer.page_changed(DOMAIN, "forbidden", "test", "default")
    assert heal is not None
    await healer.drain()
    status = healer.heal_status(heal.job_id)
    assert status is not None and status["status"] == "cancelled"
    assert status["result"]["code"] == "BLOCKED"
    assert not state.is_degraded(DOMAIN, "forbidden")
    assert [h.version for h in registry.history(DOMAIN)] == [1]


async def test_precheck_cancels_when_example_passes(
    registry: Registry, state: StateStore, executor: Executor
) -> None:
    healer = manager(registry, state, executor)
    env = await call(registry, executor, "get_product", sku="SKU-DUP")  # ambiguous for this input only
    assert env.status == R.PAGE_CHANGED and env.heal is not None
    await healer.drain()
    status = healer.heal_status(env.heal.job_id)
    assert status is not None and status["status"] == "cancelled"
    assert "passes now" in status["result"]["reason"]


async def test_precheck_session_check_for_login_actions(
    registry: Registry, state: StateStore, site_url: str
) -> None:
    sessions = InMemorySessionStore()
    sessions.put("default", DOMAIN, "shop", {"sid": "sid-revoked", "csrftoken": "x"})
    ex = Executor(sessions, Confirmer("s"))
    healer = manager(registry, state, ex)
    heal = healer.page_changed(DOMAIN, "list_orders", "test", "default")
    assert heal is not None
    await healer.drain()
    status = healer.heal_status(heal.job_id)
    assert (
        status is not None and status["status"] == "cancelled" and status["result"]["code"] == "AUTH_EXPIRED"
    )


# ---- failed heals, degraded, cooldown --------------------------------------


class Broken:
    def __init__(self, change: Any = None, error: str | None = None) -> None:
        self.change, self.error = change, error
        self.users: list[str | None] = []

    async def remap(
        self, card: Card, actions: list[str], reason: str, user: str | None = None
    ) -> dict[str, Any]:
        self.users.append(user)
        if self.error:
            raise RemapError(self.error)
        data = card.to_json_dict()
        if self.change:
            self.change(data)
        return data


def action_of(data: dict[str, Any], name: str) -> dict[str, Any]:
    return next(a for a in data["actions"] if a["name"] == name)


async def test_failed_heal_marks_degraded_and_cools_down(
    registry: Registry, state: StateStore, executor: Executor, site_url: str
) -> None:
    healer = manager(registry, state, executor, remapper=Broken(error="capture failed"))
    httpx.post(f"{site_url}/_admin/break")
    env = await call(registry, executor, "get_item", id="p1")
    assert env.heal is not None
    await healer.drain()
    status = healer.heal_status(env.heal.job_id)
    assert (
        status is not None and status["status"] == "failed" and "capture failed" in status["result"]["error"]
    )
    assert state.is_degraded(DOMAIN, "get_item")

    later = await call(registry, executor, "get_item", id="p1")
    assert later.status == R.PAGE_CHANGED and later.meta.degraded is True
    assert later.heal is not None and later.heal.job_id == env.heal.job_id  # cooldown: no new job
    assert 1 <= later.heal.retry_after_s <= 300


async def test_healed_read_failing_examples_keeps_old(
    registry: Registry, state: StateStore, executor: Executor, site_url: str
) -> None:
    def point_to_v3(data: dict[str, Any]) -> None:
        action_of(data, "get_item")["steps"][0]["request"]["url"] = (
            "{{base_url}}/api/v3/items/{{params.id | urlencode}}"
        )

    remapper = Broken(point_to_v3)
    healer = manager(registry, state, executor, remapper=remapper)
    httpx.post(f"{site_url}/_admin/break")
    heal = healer.page_changed(DOMAIN, "get_item", "test", "default")
    assert heal is not None
    await healer.drain()
    assert remapper.users == ["default"]  # the remapper re-captures with the sign-in of whoever hit the break
    status = healer.heal_status(heal.job_id)
    assert status is not None and status["status"] == "failed"
    assert "get_item" in status["result"]["kept_old"] and status["result"]["degraded"] == ["get_item"]
    assert "/api/items/" in item_url(registry)  # no new version
    assert [h.version for h in registry.history(DOMAIN)] == [1]


async def test_invalid_remap_is_rejected(
    registry: Registry, state: StateStore, executor: Executor, site_url: str
) -> None:
    def break_card(data: dict[str, Any]) -> None:
        action_of(data, "get_item")["steps"][0]["request"]["url"] = "{{params.nope}}"

    healer = manager(registry, state, executor, remapper=Broken(break_card))
    httpx.post(f"{site_url}/_admin/break")
    heal = healer.page_changed(DOMAIN, "get_item", "test", "default")
    assert heal is not None
    await healer.drain()
    status = healer.heal_status(heal.job_id)
    assert status is not None and status["status"] == "failed"
    assert "unknown param 'nope'" in "\n".join(status["result"]["errors"])


async def test_site_level_change_must_pass_every_read(
    registry: Registry, state: StateStore, executor: Executor, site_url: str
) -> None:
    def change_site(data: dict[str, Any]) -> None:
        data["defaults"]["headers"]["x-new"] = "1"
        action_of(data, "get_item")["steps"][0]["request"]["url"] = (
            "{{base_url}}/api/v2/items/{{params.id | urlencode}}"
        )

    healer = manager(registry, state, executor, remapper=Broken(change_site))
    httpx.post(f"{site_url}/_admin/break")
    heal = healer.page_changed(DOMAIN, "get_item", "test", "default")
    assert heal is not None
    await healer.drain()
    status = healer.heal_status(heal.job_id)
    # Several fixture reads (rate_limited, challenge...) fail their examples by design, so the site-level
    # change is rejected as a whole.
    assert status is not None and status["status"] == "failed"
    assert status["result"]["site_level_changed"] is True
    assert "site-level" in status["result"]["error"]


async def test_healed_write_goes_live(
    registry: Registry, state: StateStore, executor: Executor, site_url: str
) -> None:
    def change_write(data: dict[str, Any]) -> None:
        action_of(data, "add_to_cart")["steps"][0]["request"]["url"] = "{{base_url}}/api/v2/cart"

    healer = manager(registry, state, executor, remapper=Broken(change_write))
    heal = healer.page_changed(DOMAIN, "add_to_cart", "test", "default")
    assert heal is not None
    await healer.drain()
    status = healer.heal_status(heal.job_id)
    assert status is not None and status["status"] == "succeeded", status
    assert status["result"]["healed"] == ["add_to_cart"] and status["result"]["unvalidated"] == [
        "add_to_cart"
    ]
    assert status["new_version"] == 2
    stored = registry.get(DOMAIN)
    assert stored is not None
    add = stored.card.action("add_to_cart")
    assert add is not None and add.steps[0].request.url.endswith("/api/v2/cart")
    assert not state.is_degraded(DOMAIN, "add_to_cart")
    # The healed write was not executed to validate it.
    assert "POST /api/cart" not in hits(site_url) and "POST /api/v2/cart" not in hits(site_url)


# ---- EMPTY spikes ----------------------------------------------------------


@pytest.fixture
def empty_example_registry(db: Path, fixture_card: dict[str, Any]) -> Iterator[Registry]:
    """search_products whose example query now finds nothing, as if the site's search broke."""
    card = copy.deepcopy(fixture_card)
    action_of(card, "search_products")["examples"] = [{"params": {"query": "zzz"}, "min_items": 1}]
    reg = Registry(db)
    reg.save(card)
    yield reg
    reg.close()


async def test_empty_spike_queues_heal(
    empty_example_registry: Registry, state: StateStore, executor: Executor
) -> None:
    reg = empty_example_registry
    healer = manager(reg, state, executor, remapper=Broken(error="no change"))
    for _ in range(4):
        assert (await call(reg, executor, "search_products", query="nothing")).status == R.EMPTY
    assert state.empty_count(DOMAIN, "search_products") == 4
    await healer.drain()
    assert state.open_job_for(DOMAIN, "search_products") is None

    assert (await call(reg, executor, "search_products", query="nothing")).status == R.EMPTY
    assert state.empty_count(DOMAIN, "search_products") == 0
    await healer.drain()
    job = state.latest_job_for(DOMAIN, "search_products")
    assert job is not None and job.trigger == "empty_spike"


async def test_success_resets_empty_count(registry: Registry, state: StateStore, executor: Executor) -> None:
    healer = manager(registry, state, executor)
    for _ in range(3):
        await call(registry, executor, "search_products", query="nothing")
    await call(registry, executor, "search_products", query="lamp")
    assert state.empty_count(DOMAIN, "search_products") == 0
    for _ in range(5):
        await call(registry, executor, "search_products", query="nothing")
    await healer.drain()
    # The example ("lamp") still returns items, so the spike is not a breakage.
    assert state.latest_job_for(DOMAIN, "search_products") is None


# ---- health checks ---------------------------------------------------------


async def test_health_check_queues_heal_for_no_login_sites(
    db: Path, fixture_card: dict[str, Any], state: StateStore, executor: Executor, site_url: str
) -> None:
    card = copy.deepcopy(fixture_card)
    card["actions"] = [
        a for a in card["actions"] if a["name"] in ("search_products", "get_item", "add_to_cart")
    ]
    card["connect_providers"] = []
    reg = Registry(db)
    reg.save(card)
    healer = manager(reg, state, executor)
    assert await healer.check_domain(DOMAIN) == []
    httpx.post(f"{site_url}/_admin/break")
    assert await healer.check_domain(DOMAIN) == ["get_item"]
    job = state.latest_job_for(DOMAIN, "get_item")
    assert job is not None and job.trigger == "health_check"
    await healer.drain()
    assert "/api/v2/items/" in item_url(reg)
    reg.close()


async def test_health_check_skips_login_sites(
    registry: Registry, state: StateStore, executor: Executor
) -> None:
    assert await manager(registry, state, executor).check_domain(DOMAIN) == []


def test_orphaned_running_jobs_fail_on_restart(state: StateStore) -> None:
    job = state.create_job(DOMAIN, ["get_item"], "page_changed", "x", "default")
    assert state.start_job(job.id)
    assert state.fail_orphaned_jobs() == 1
    refreshed = state.get_job(job.id)
    assert refreshed is not None and refreshed.status == "failed"
