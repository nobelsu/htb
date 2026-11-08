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
