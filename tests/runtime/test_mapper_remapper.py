"""The heal adapter: a heal job re-records the broken actions with WeiWei's mapper and re-maps the site.

Unit tests use a fake mapper (no browser). The end-to-end test captures the mapper's fixture site, breaks it
(the search API moves) and lets the runtime's HealManager heal it through the adapter.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from runtime.jobs.healing import RemapError
from runtime.jobs.mapper_remapper import MapperRemapper
from runtime.signin.sessions import InMemorySessionStore
from schema import load_card

FIXTURE_CARD = Path(__file__).parents[1] / "sites" / "fixture_site.json"


def fixture_card() -> Any:
    return load_card(json.loads(FIXTURE_CARD.read_text()))


def provider_of(card: Any, action: str) -> str:
    a = card.action(action)
    assert a is not None
    provider = a.effective_auth_provider(card.defaults)
    assert provider is not None
    return str(provider)


@dataclass
class FakeValidation:
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class FakeResult:
    card: dict[str, Any]
    validation: FakeValidation


@dataclass
class FakeMapper:
    """Records what the adapter asked the mapper to do."""

    card: dict[str, Any]
    errors: list[str] = field(default_factory=list)
    fail_replay: Exception | None = None
    replays: list[dict[str, Any]] = field(default_factory=list)
    mapped: list[Path] = field(default_factory=list)

    def recapture(
        self, domain_dir: Path, *, task: str, state: Path | None, headless: bool
    ) -> tuple[list[Any], list[str]]:
        seen: dict[str, Any] = {"dir": Path(domain_dir), "task": task, "headless": headless, "state": None}
        if state is not None:
            seen["state"] = json.loads(Path(state).read_text())
            seen["state_mode"] = stat.S_IMODE(os.stat(state).st_mode)
            seen["state_path"] = Path(state)
        self.replays.append(seen)
        if self.fail_replay is not None:
            raise self.fail_replay
        return [], [f"{task}: a replay warning"]

    def map_domain(self, captures_dir: Path) -> FakeResult:
        self.mapped.append(Path(captures_dir))
        return FakeResult(self.card, FakeValidation(list(self.errors)))


@pytest.fixture
def captures(tmp_path: Path) -> Path:
    root = tmp_path / "captures"
    for task in ("list_orders", "search_products", "get_product"):
        (root / "fixture.test" / task / "m1").mkdir(parents=True)
    return root


def adapter(
    captures: Path, mapper: FakeMapper, sessions: InMemorySessionStore | None = None
) -> MapperRemapper:
    return MapperRemapper(
        captures,
        sessions=sessions,
        recapture=mapper.recapture,
        map_domain=mapper.map_domain,  # type: ignore[arg-type]
    )


async def test_replays_only_the_broken_actions_then_remaps_the_site(captures: Path) -> None:
    card = fixture_card()
    mapper = FakeMapper(card.to_json_dict())
    out = await adapter(captures, mapper).remap(card, ["search_products", "get_product"], "PAGE_CHANGED")
    assert [r["task"] for r in mapper.replays] == ["search_products", "get_product"]
    assert all(r["dir"] == captures / "fixture.test" and r["headless"] for r in mapper.replays)
    assert mapper.mapped == [captures / "fixture.test"]  # one re-map of the whole site afterwards
    assert out == card.to_json_dict()


async def test_uses_the_users_current_sign_in(captures: Path) -> None:
    card = fixture_card()
    store = InMemorySessionStore()
    store.put(
        "default", "fixture.test", provider_of(card, "list_orders"), {"sid": "s3cret", "csrftoken": "c"}
    )
    mapper = FakeMapper(card.to_json_dict())
    await adapter(captures, mapper, store).remap(card, ["list_orders"], "PAGE_CHANGED")

    (replay,) = mapper.replays
    cookies = {c["name"]: c for c in replay["state"]["cookies"]}
    assert {n: c["value"] for n, c in cookies.items()} == {"sid": "s3cret", "csrftoken": "c"}
    host = card.base_url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
    assert all(c["domain"] == host and c["path"] == "/" for c in cookies.values())
    assert replay["state_mode"] == 0o600  # only we can read it while the replay runs
    assert not replay["state_path"].exists()  # and it's gone afterwards


async def test_falls_back_to_the_recorded_sign_in(captures: Path) -> None:
    card = fixture_card()
    mapper = FakeMapper(card.to_json_dict())
    await adapter(captures, mapper, InMemorySessionStore()).remap(card, ["list_orders"], "PAGE_CHANGED")
    assert mapper.replays[0]["state"] is None  # the mapper then uses the bundle's own state.json


async def test_no_recordings_for_the_site(tmp_path: Path) -> None:
    card = fixture_card()
    mapper = FakeMapper(card.to_json_dict())
    with pytest.raises(RemapError, match="no recordings for fixture.test"):
        await adapter(tmp_path / "nothing", mapper).remap(card, ["list_orders"], "PAGE_CHANGED")
    assert mapper.replays == []


async def test_no_recording_for_the_broken_action(captures: Path) -> None:
    card = fixture_card()
    shutil.rmtree(captures / "fixture.test" / "get_product")
    mapper = FakeMapper(card.to_json_dict())
    with pytest.raises(RemapError, match="no recording for get_product"):
        await adapter(captures, mapper).remap(card, ["get_product"], "PAGE_CHANGED")
    assert mapper.replays == []


async def test_replay_failure_becomes_a_remap_error(captures: Path) -> None:
    card = fixture_card()
    mapper = FakeMapper(
        card.to_json_dict(), fail_replay=RuntimeError("replay of list_orders wrote no bundles")
    )
    with pytest.raises(RemapError, match="replay of list_orders wrote no bundles"):
        await adapter(captures, mapper).remap(card, ["list_orders"], "PAGE_CHANGED")
    assert mapper.mapped == []


async def test_invalid_remapped_card_is_rejected(captures: Path) -> None:
    card = fixture_card()
    mapper = FakeMapper(card.to_json_dict(), errors=["schema: actions.0.steps: field required"])
    with pytest.raises(RemapError, match="re-mapped card is invalid: schema: actions.0.steps"):
        await adapter(captures, mapper).remap(card, ["list_orders"], "PAGE_CHANGED")


# --- end to end: the mapper's fixture site changes, the runtime heals it ---------------------------------


@pytest.mark.browser
async def test_heal_job_heals_a_changed_site_with_the_mapper(tmp_path: Path) -> None:
    import asyncio

    from fixture_site import FixtureSite, scripts  # the mapper's fixture site (tests/mapper is on pythonpath)

    from mapper.capture.record import CaptureConfig, record
    from mapper.pipeline import map_domain
    from runtime.jobs.healing import HealConfig, HealManager
    from runtime.run.confirm import Confirmer
    from runtime.run.executor import Executor
    from runtime.store.registry import Registry
    from runtime.store.state import StateStore
    from schema.envelope import ResultCode

    def chromium_ok() -> str | None:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                pw.chromium.launch(headless=True).close()
        except Exception as e:  # pragma: no cover - depends on the machine
            return str(e)
        return None

    # The mapper drives the browser with Playwright's sync API, so it runs in a worker thread (as in the
    # adapter).
    if (problem := await asyncio.to_thread(chromium_ok)) is not None:
        pytest.skip(f"Playwright Chromium unavailable: {problem}")

    os.environ["MAPPER_NO_LLM"] = "1"
    site = FixtureSite().start()
    try:
        root = tmp_path / "captures"
        login = tmp_path / "login.state.json"
        site.login_state(login)
        markers = {"m1": "zqxv8841", "m2": "kjtr5527"}

        def capture_search() -> None:
            for run in ("m1", "m2", "real", "page2"):
                record(
                    CaptureConfig(
                        start_url=site.url + "/",
                        task="search",
                        run=run,
                        markers={"query": markers[run]} if run in markers else {},
                        real_inputs={} if run in markers else {"query": "software"},
                        kind="read",
                        description="Search companies by keyword",
                        state=login,
                        out=root,
                        headless=True,
                        script=scripts.SEARCH_PAGE2 if run == "page2" else scripts.SEARCH,
                    )
                )

        await asyncio.to_thread(capture_search)
        domain_dir = root / site.host
        # Only the user's current sign-in can make the replay work: drop the sign-ins saved in the recordings.
        for saved in domain_dir.glob("*/*/state.json"):
            saved.unlink()

        mapped = await asyncio.to_thread(map_domain, domain_dir)
        assert mapped.validation.ok, mapped.validation.errors
        registry = Registry(tmp_path / "runtime.db")
        registry.save(mapped.card, note="initial map")
        card = registry.get(site.host)
        assert card is not None

        store = InMemorySessionStore()
        state = json.loads(login.read_text())
        store.put(
            "default",
            site.host,
            provider_of(card.card, "search"),
            {c["name"]: c["value"] for c in state["cookies"]},
        )
        executor = Executor(store, Confirmer("s"))
        healer = HealManager(
            registry,
            StateStore(tmp_path / "runtime.db"),
            executor,
            MapperRemapper(root, sessions=store),
            HealConfig(),
        )
        params = {"query": "software"}
        assert (await executor.run(card, "search", params)).status == ResultCode.SUCCESS

        site.move_search(True)  # the site's search API moves to /api/v2/search
        broken = await executor.run(card, "search", params)
        assert broken.status == ResultCode.PAGE_CHANGED and broken.heal is not None
        await healer.drain()

        job = healer.heal_status(broken.heal.job_id)
        assert job is not None and job["status"] == "succeeded", job
        healed = registry.get(site.host)
        assert healed is not None and healed.version == 2
        search = healed.card.action("search")
        assert search is not None and search.steps[-1].request.url.endswith("/api/v2/search")
        assert (await executor.run(healed, "search", params)).status == ResultCode.SUCCESS
        assert (root / f"{site.host}.prev" / "search").is_dir()  # the old recordings are kept
        registry.close()
    finally:
        site.stop()
        os.environ.pop("MAPPER_NO_LLM", None)


def test_gateway_setting_turns_it_on(tmp_path: Path) -> None:
    from runtime.gateway import load_remapper

    store = InMemorySessionStore()
    remapper = load_remapper("mapper", store, tmp_path / "caps")
    assert isinstance(remapper, MapperRemapper)
    assert remapper.sessions is store and remapper.captures_dir == tmp_path / "caps"
    assert load_remapper("") is None
