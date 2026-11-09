"""flowmap.heal: break the bookshop, watch the runtime return PAGE_CHANGED, heal it with flowmap through the
runtime's HealManager, and get SUCCESS again. Non-breaks never queue a heal. Needs Chromium (except the pure
tests at the top)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from flowmap.flows import Flow, Goto, Input, save_flow
from flowmap.heal import FlowRemapper, diff_actions, keep_labels
from flowmap.infer import Inferred
from flowmap.minimise import executor_replayer
from runtime.jobs.healing import HealConfig, HealManager
from runtime.run.confirm import Confirmer
from runtime.run.executor import Executor
from runtime.run.tiers import HttpTier, ImpersonateTier
from runtime.signin.sessions import InMemorySessionStore
from runtime.store.registry import Registry
from runtime.store.state import StateStore
from schema.envelope import ResultCode
from tests.flowmap.conftest import book_flow, search_flow

DOMAIN = "bookshop.test"
QUERY = {"query": "harbour", "sort": "relevance"}

# ---------- pure ----------


def _inferred(paths: list[str]) -> Inferred:
    return Inferred(
        action="a",
        kind="read",
        base_url="https://x.test",
        steps=[],
        params={"q": {"type": "string"}},
        output={"from": "steps.s.items", "fields": {p: p for p in paths}},
        examples=[],
        auth_provider=None,
        changed_signals=[],
        confidence=1,
        reasons=[],
    )


FLOW = Flow(
    domain="x.test", action="a", inputs={"q": Input(examples=["x"])}, steps=[Goto(url="https://x.test/{{q}}")]
)
OLD = {
    "description": "Search.",
    "params": {"q": {"type": "string", "description": "words"}, "gone": {"type": "string"}},
    "output": {"fields": {"title": "$.book.title", "id": "$.book.id", "rating": "$.stats.rating"}},
}


def test_keep_labels_follows_a_renamed_field() -> None:
    labels, notes = keep_labels(OLD, _inferred(["$.book.name", "$.book.id", "$.tracking"]), FLOW)
    assert labels.description == "Search." and labels.params == {"q": "words"}
    assert labels.fields == {"title": "$.book.name", "id": "$.book.id"}
    assert "moved: $.book.title -> $.book.name" in notes[0]
    assert "'rating'" in notes[1] and "dropped" in notes[1]


def test_keep_labels_needs_a_clear_new_path() -> None:
    labels, notes = keep_labels(OLD, _inferred(["$.book.name", "$.book.subtitle", "$.book.id"]), FLOW)
    assert labels.fields == {"id": "$.book.id"}  # two new siblings: which one was the title is unclear
    assert any("'title'" in n and "dropped" in n for n in notes)


def test_diff_actions() -> None:
    old = {"steps": [{"request": {"url": "/api/search", "query": {"q": "{{params.query}}"}}}]}
    new = {"steps": [{"request": {"url": "/api/v2/search", "query": {"query": "{{params.query}}"}}}]}
    assert diff_actions(old, new) == [
        '- steps[0].request.query.q: "{{params.query}}"',
        '+ steps[0].request.query.query: "{{params.query}}"',
        '~ steps[0].request.url: "/api/search" -> "/api/v2/search"',
    ]


# ---------- against the bookshop ----------

_MAPPED: dict[str, dict[str, Any]] = {}


def make_executor(tier: Any = None) -> Executor:
    tiers: dict[str, Any] = {"http": HttpTier(), "impersonate": ImpersonateTier()}
    return Executor(
        InMemorySessionStore(), Confirmer("t" * 32), tiers=tiers if tier is None else {tier: tiers[tier]}
    )


async def mapped_card(base: str) -> dict[str, Any]:
    """search_books and get_book mapped once per test session (capturing is the slow part)."""
    if base not in _MAPPED:
        from flowmap.assemble import map_action
        from flowmap.capture import capture_flow

        card = None
        for flow in (search_flow(base), book_flow(base)):
            runs = await capture_flow(flow, pause_s=0)
            mapped = await map_action(
                flow, runs, existing=card, replay=executor_replayer(make_executor), use_llm=False, pause_s=0
            )
            card = mapped.final.card
        assert card is not None
        _MAPPED[base] = card
    return json.loads(json.dumps(_MAPPED[base]))


def _stable(rows: Any) -> list[dict[str, Any]]:
    return [{k: v for k, v in row.items() if k != "tracking"} for row in rows or []]  # tracking is random


class Setup:
    def __init__(self, base: str, tmp: Path, card: dict[str, Any]) -> None:
        self.base = base
        self.registry = Registry(tmp / "runtime.db")
        self.registry.save(card, note="mapped")
        flows = tmp / "flows"
        save_flow(search_flow(base), flows)
        save_flow(book_flow(base), flows)
        self.state = StateStore(tmp / "runtime.db")
        self.executor = make_executor()
        self.remapper = FlowRemapper(
            flows, tmp / "captures", pause_s=0, replay=executor_replayer(make_executor)
        )
        self.healer = HealManager(
            self.registry, self.state, self.executor, self.remapper, HealConfig(poll_s=0.05)
        )

    async def run(self, action: str, params: dict[str, Any]) -> Any:
        stored = self.registry.get(DOMAIN)
        assert stored is not None
        return await self.executor.run(stored, action, params)

    def brk(self, kind: str) -> None:
        httpx.post(f"{self.base}/_admin/break/{kind}").raise_for_status()


@pytest.fixture
async def shop(bookshop: str, tmp_path: Path) -> Setup:
    pytest.importorskip("playwright.async_api")
    return Setup(bookshop, tmp_path, await mapped_card(bookshop))


@pytest.mark.browser
@pytest.mark.parametrize(
    "kind, expect_in_diff",
    [
        ("endpoint", "/api/v2/search"),  # the endpoint was renamed: 404, probe 404
        ("nested", "results.items"),  # the list moved: a required path is missing
        ("param", "query.query"),  # the query parameter was renamed: 400, probe 400
    ],
)
async def test_break_then_heal(shop: Setup, kind: str, expect_in_diff: str) -> None:
    before = await shop.run("search_books", QUERY)
    assert before.status == ResultCode.SUCCESS
    shop.brk(kind)

    broken = await shop.run("search_books", QUERY)
    assert broken.status == ResultCode.PAGE_CHANGED and broken.heal is not None
    await shop.healer.drain()
    job = shop.state.get_job(broken.heal.job_id)
    assert job is not None and job.status == "succeeded", job.result if job else None
    assert job.result is not None and job.result["healed"] == ["search_books"] and job.new_version == 2

    after = await shop.run("search_books", QUERY)
    assert after.status == ResultCode.SUCCESS
    assert _stable(after.data) == _stable(before.data)  # the same results, under the same field names
    assert (await shop.run("get_book", {"book_id": "b001"})).status == ResultCode.SUCCESS  # untouched

    events = shop.state.events(job.id)
    assert [e["event"] for e in events] == ["remap_start", "remap_done", "heal_done"]
    detail = events[1]["data"]["details"]["search_books"]
    assert detail["capture_ms"] > 0 and detail["total_ms"] >= detail["capture_ms"] + detail["infer_ms"]
    assert any(expect_in_diff in line for line in detail["diff"]), detail["diff"]
    assert events[2]["data"]["status"] == "succeeded" and events[2]["data"]["ms"] >= events[1]["data"]["ms"]
    print(f"\nheal {kind}: remap {events[1]['data']['ms']} ms, total {events[2]['data']['ms']} ms")


@pytest.mark.browser
async def test_non_breaks_never_heal(shop: Setup) -> None:
    empty = await shop.run("search_books", {"query": "zzqqxx", "sort": "price"})
    assert empty.status == ResultCode.EMPTY and empty.heal is None
    bad_id = await shop.run("get_book", {"book_id": "nope"})
    assert bad_id.status == ResultCode.ERROR and bad_id.heal is None  # 404, but the example still works
    assert shop.state.latest_job_for(DOMAIN, "search_books") is None
    assert shop.state.latest_job_for(DOMAIN, "get_book") is None


@pytest.mark.browser
async def test_field_rename_is_not_detected_by_the_spec_today(shop: Setup) -> None:
    """CARD_SPEC: a field with no match is null, not an error, so a renamed field reads as SUCCESS with nulls
    and nothing queues a heal. Open question (see the step 7 report)."""
    shop.brk("field")
    env = await shop.run("search_books", QUERY)
    assert env.status == ResultCode.SUCCESS and env.heal is None
    assert all(row["title"] is None for row in env.data or [])
