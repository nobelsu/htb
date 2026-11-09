"""spider.flowmap.infer.assemble and spider.flowmap.infer.minimise: building, merging, checking and saving
cards. The end-to-end test maps the bookshop with the runtime's own executor and registry (needs Chromium)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from spider.card import validate_card
from spider.card.envelope import Envelope, Meta, ResultCode
from spider.flowmap.infer.actions import Inferred
from spider.flowmap.infer.assemble import (
    AssembleError,
    build_action,
    finalise,
    map_action,
    merge,
    tool_prefix_for,
)
from spider.flowmap.infer.label import Labels
from spider.flowmap.infer.minimise import Replayer, executor_replayer, minimise
from spider.flowmap.record.flows import Flow, Goto, Input
from spider.run.confirm import Confirmer
from spider.run.executor import Executor
from spider.run.tiers.http import HttpTier, ImpersonateTier
from spider.signin.sessions.base import InMemorySessionStore
from spider.store.registry import Registry, StoredCard

FLOW = Flow(
    domain="shop.test",
    action="search",
    inputs={"q": Input(examples=["mug"])},
    steps=[Goto(url="https://shop.test/?q={{q}}")],
)


def inferred(kind: str = "read", method: str = "GET") -> Inferred:
    return Inferred(
        action="search",
        kind=kind,
        base_url="https://shop.test",
        steps=[
            {
                "id": "search",
                "request": {
                    "method": method,
                    "url": "{{base_url}}/api/search",
                    "query": {"q": "{{params.q}}"},
                    "headers": {"accept": "application/json", "x-csrf": "{{steps.home.csrf}}"},
                },
                "expect": {"status": [200], "content_type": "json", "paths": []},
                "extract": {"items": {"from": "body", "type": "jsonpath", "expr": "$.items[*]"}},
            }
        ],
        params={"q": {"type": "string", "required": True, "example": "mug"}},
        output={
            "from": "steps.search.items",
            "fields": {"$.name": "$.name"},
            "limit": 3,
            "next_params": {"param": "page", "increment": 1, "page_size": 10},
        },
        examples=[{"params": {"q": "mug"}, "min_items": 1}],
        auth_provider=None,
        changed_signals=["body_contains:PersistedQueryNotFound"],
        confidence=1.0,
        reasons=[],
    )


LABELS = Labels(
    description="Find things.", params={"q": "search words", "nope": "x"}, fields={"name": "$.name"}
)


def test_tool_prefix() -> None:
    assert tool_prefix_for("www.linkedin.com") == "linkedin"
    assert tool_prefix_for("find.company-information.service.gov.uk") == "service"
    assert tool_prefix_for("bookshop.test") == "bookshop"


def test_build_action_sets_safety_flags_itself() -> None:
    read = build_action(inferred(), LABELS, FLOW)
    assert read["requires_confirmation"] is False and read["idempotent"] is True and read["spends"] is False
    assert read["params"]["q"]["description"] == "search words" and "nope" not in read["params"]
    assert read["output"]["fields"] == {"name": "$.name"} and read["output"]["limit"] == 10  # = page_size
    write = build_action(inferred("write", "POST"), LABELS, FLOW)
    assert write["requires_confirmation"] is True and write["idempotent"] is False and write["examples"] == []
    assert build_action(inferred("write", "PUT"), LABELS, FLOW)["idempotent"] is True


def test_merge_new_and_existing_cards() -> None:
    inf = inferred()
    card = merge(build_action(inf, LABELS, FLOW), inf, FLOW, None, source="t", mapped_at="2026-11-07")
    assert card["tool_prefix"] == "shop" and card["base_url"] == "https://shop.test"
    assert card["provenance"] == {"mapped_at": "2026-11-07", "source": "t", "mapper_version": "flowmap-0.1"}
    other = copy.deepcopy(card["actions"][0]) | {"name": "other"}
    existing = card | {
        "actions": [other, card["actions"][0] | {"description": "old"}],
        "base_url": "https://api.shop.test",
    }
    merged = merge(build_action(inf, LABELS, FLOW), inf, FLOW, existing, source="t2")
    assert [a["name"] for a in merged["actions"]] == ["other", "search"]
    assert (
        merged["actions"][1]["steps"][0]["request"]["url"] == "https://shop.test/api/search"
    )  # other origin
    assert merged["changed_signals"] == ["body_contains:PersistedQueryNotFound"]


def test_merge_needs_the_login_provider() -> None:
    flow = FLOW.model_copy(update={"auth_provider": "main"})
    with pytest.raises(AssembleError, match="no connect provider"):
        merge(build_action(inferred(), LABELS, flow), inferred(), flow, None, source="t")


async def test_finalise_refuses_invalid_cards_and_unsafe_writes() -> None:
    card = merge(build_action(inferred(), LABELS, FLOW), inferred(), FLOW, None, source="t")
    with pytest.raises(AssembleError, match="isn't valid") as e:  # steps.home doesn't exist
        await finalise(card, "search")
    assert any("home" in r for r in e.value.reasons)
    write = merge(build_action(inferred("write", "POST"), LABELS, FLOW), inferred(), FLOW, None, source="t")
    write["actions"][0]["requires_confirmation"] = False
    with pytest.raises(AssembleError, match="requires_confirmation"):
        await finalise(write, "search")


# ---------- minimise (fake replays) ----------


def env(status: ResultCode, items: int = 0) -> Envelope:
    return Envelope(status=status, data=[{}] * items, meta=Meta(ms=1, tier="http", run_id="r"))


class FakeReplay(Replayer):
    """http needs header ``x-csrf``; impersonate works with anything; counts calls."""

    def __init__(self, http_ok: bool = True, rate_limit_after: int | None = None) -> None:
        self.http_ok, self.rate_limit_after, self.calls = http_ok, rate_limit_after, 0

    async def __call__(self, card: dict[str, Any], action: str, args: dict[str, Any], tier: Any) -> Envelope:
        self.calls += 1
        if self.rate_limit_after is not None and self.calls > self.rate_limit_after:
            return env(ResultCode.RATE_LIMITED)
        if tier == "http" and not self.http_ok:
            return env(ResultCode.BLOCKED)
        headers = card["actions"][0]["steps"][0]["request"].get("headers", {})
        return env(ResultCode.SUCCESS, 3) if "x-csrf" in headers else env(ResultCode.PAGE_CHANGED)


def _card() -> dict[str, Any]:
    return merge(build_action(inferred(), LABELS, FLOW), inferred(), FLOW, None, source="t")


async def test_minimise_drops_unneeded_headers() -> None:
    out = await minimise(_card(), "search", {"q": "mug"}, FakeReplay(), pause_s=0)
    assert out.tier == "http" and out.dropped == ["search: accept"] and out.kept == ["search: x-csrf"]
    assert out.card["actions"][0]["tier"] == "http"
    assert out.card["actions"][0]["steps"][0]["request"]["headers"] == {"x-csrf": "{{steps.home.csrf}}"}


async def test_minimise_climbs_tiers_and_stops_on_rate_limit() -> None:
    out = await minimise(_card(), "search", {"q": "mug"}, FakeReplay(http_ok=False), pause_s=0)
    assert out.tier == "impersonate" and out.reasons[0].startswith("http tier: BLOCKED")
    limited = await minimise(_card(), "search", {"q": "mug"}, FakeReplay(rate_limit_after=1), pause_s=0)
    assert any("RATE_LIMITED" in r for r in limited.reasons)
    assert (
        limited.card["actions"][0]["steps"][0]["request"]["headers"]
        == _card()["actions"][0]["steps"][0]["request"]["headers"]
    )


async def test_minimise_never_replays_writes() -> None:
    card = merge(build_action(inferred("write", "POST"), LABELS, FLOW), inferred(), FLOW, None, source="t")
    replay = FakeReplay()
    out = await minimise(card, "search", {}, replay, pause_s=0)
    assert replay.calls == 0 and out.kept == ["search: accept", "search: x-csrf"]


# ---------- the bookshop, end to end ----------


def make_executor(tier: Any) -> Executor:
    client = {"http": HttpTier(), "impersonate": ImpersonateTier()}[tier]
    return Executor(InMemorySessionStore(), Confirmer("t" * 32), tiers={tier: client})


@pytest.mark.browser
async def test_map_bookshop_actions_into_the_registry(bookshop: str, tmp_path: Path) -> None:
    pytest.importorskip("playwright.async_api")
    from spider.flowmap.record.capture import capture_flow
    from tests.flowmap.conftest import book_flow, cart_flow, search_flow

    registry = Registry(tmp_path / "runtime.db")
    replay = executor_replayer(make_executor)
    existing = None
    reports = {}
    for make in (search_flow, book_flow, cart_flow):
        flow = make(bookshop)
        runs = await capture_flow(flow, pause_s=0, marker_runs=1 if flow.action == "add_to_cart" else 2)
        mapped = await map_action(
            flow, runs, existing=existing, replay=replay, registry=registry, use_llm=False, pause_s=0
        )
        reports[flow.action] = mapped.report()
        stored = registry.get("bookshop.test")
        assert stored is not None
        existing = stored.card.to_json_dict()

    assert stored.version == 3 and [a.name for a in stored.card.actions] == [
        "search_books",
        "get_book",
        "add_to_cart",
    ]
    search = reports["search_books"]
    assert search["tier"] == "http" and search["replay"]["status"] == "SUCCESS"
    assert sorted(search["dropped_headers"]) == ["home: accept", "search: accept", "search: referer"]
    csrf = stored.card.action("search_books").steps[1].request.headers  # type: ignore[union-attr]
    assert csrf == {"csrf-token": "{{steps.home.jsessionid | strip_quotes}}"}  # the one header it needs
    assert reports["get_book"]["replay"]["items"] == 1
    cart = stored.card.action("add_to_cart")
    assert cart is not None and cart.requires_confirmation and reports["add_to_cart"]["replays"] == 0

    # the saved card runs through the executor like any other
    first = await make_executor("http").run(stored, "search_books", {"query": "the", "sort": "price"})
    assert first.status == ResultCode.SUCCESS and len(first.data or []) == 10
    assert first.next_params == {"query": "the", "sort": "price", "page": 2}
    second = await make_executor("http").run(stored, "search_books", first.next_params)
    assert len(second.data or []) == 8 and second.next_params is None  # 18 books have "the" in the title


@pytest.mark.browser
async def test_a_page_rendered_on_the_server_is_read_with_css(bookshop: str, tmp_path: Path) -> None:
    """Level 2: no request carries the results as data, but the page's own HTML does: the action fetches the
    page and reads the results with the recorder's selector."""
    pytest.importorskip("playwright.async_api")
    from spider.flowmap.record.capture import capture_flow
    from spider.flowmap.record.flows import Fill, Flow, Goto, Input, LocatorCall, Press, WaitFor

    box = [LocatorCall(method="get_by_placeholder", args=["Search by author"])]
    flow = Flow(
        domain="bookshop.test",
        action="search_by_author",
        inputs={"author": Input(examples=["Author B", "Author C"])},
        steps=[
            Goto(url=bookshop + "/authors"),
            Fill(target=box, value="{{author}}"),
            Press(target=box, key="Enter"),
            WaitFor(target="#count"),
        ],
        results_selector=".hit h3",
    )
    runs = await capture_flow(flow, pause_s=0)
    mapped = await map_action(flow, runs, replay=executor_replayer(make_executor), use_llm=False, pause_s=0)
    action = next(a for a in mapped.final.card["actions"] if a["name"] == "search_by_author")
    step = action["steps"][-1]
    assert step["extract"]["items"] == {"from": "body", "type": "css", "expr": ".hit h3"}
    assert step["expect"]["content_type"] == "html" and "q" in step["request"]["query"]
    assert mapped.final.replay_status == "SUCCESS"

    report = validate_card(mapped.final.card)
    assert report.ok and report.card is not None, report.errors
    stored = StoredCard("bookshop.test", 1, report.card, True, "2026-11-08T00:00:00Z", None)
    env = await make_executor("http").run(stored, "search_by_author", {"author": "Author D"}, user="u")
    assert env.status == ResultCode.SUCCESS and env.data
    first = env.data[0]
    assert any(v == "/book/b003" or str(v).endswith("/book/b003") for v in first.values()), first
