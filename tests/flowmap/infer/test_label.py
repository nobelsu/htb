"""spider.flowmap.infer.label: rule labels, enforcing what a model may change, and the model call (with a fake
client)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from spider.flowmap.infer.actions import Inferred
from spider.flowmap.infer.label import FALLBACK_BETA, LABEL_TOOL, default_name, enforce, label, rule_labels
from spider.flowmap.record.flows import Flow, Goto, Input


def inferred(kind: str = "read") -> Inferred:
    return Inferred(
        action="search_books",
        kind=kind,
        base_url="https://shop.test",
        steps=[{"id": "search", "request": {"method": "GET", "url": "{{base_url}}/api/search"}}],
        params={"query": {"type": "string", "required": True, "example": "harbour"}},
        output={
            "from": "steps.search.items",
            "fields": {p: p for p in ("$.book.title", "$.book.id", "$.tracking", "$.book.tags")},
        },
        examples=[],
        auth_provider=None,
        changed_signals=[],
        confidence=1.0,
        reasons=[],
        field_samples={
            "$.book.title": ["A"],
            "$.book.id": ["b1"],
            "$.tracking": ["ab12"],
            "$.book.tags": [["x"]],
        },
    )


FLOW = Flow(
    domain="shop.test",
    action="search_books",
    inputs={"query": Input(examples=["harbour"])},
    steps=[Goto(url="https://shop.test/?q={{query}}")],
)


def test_default_names() -> None:
    assert default_name("$.book.title") == "title"
    assert default_name("$['display-name']") == "display_name"
    assert default_name("$.items[0].firstName") == "first_name"


def test_rule_labels() -> None:
    out = rule_labels(inferred(), FLOW)
    assert out.source == "rules"
    assert out.fields == {
        "title": "$.book.title",
        "id": "$.book.id",
        "tracking": "$.tracking",
    }  # no list fields
    assert out.description == "Search books on shop.test. Returns title, id, tracking for each result."
    assert out.params == {"query": "query"}
    assert rule_labels(inferred("write"), FLOW).description == "Search books. Changes data on shop.test."


def test_enforce_keeps_only_what_labelling_may_change() -> None:
    raw: dict[str, Any] = {
        "description": "  Search the shop's books by title.  ",
        "params": [
            {"name": "query", "description": "Words in the title"},
            {"name": "admin", "description": "x"},
        ],
        "fields": [
            {"path": "$.book.title", "name": "title", "keep": True},
            {"path": "$.book.id", "name": "Book ID!", "keep": True},
            {"path": "$.tracking", "name": "tracking", "keep": False},
            {"path": "$.secret", "name": "secret", "keep": True},
            {"path": "$.book.title", "name": "again", "keep": True},
        ],
        "requires_confirmation": False,
        "url": "https://evil.test/",
    }
    out = enforce(raw, inferred(), FLOW)
    assert out.description == "Search the shop's books by title."
    assert out.params == {"query": "Words in the title"}
    assert out.fields == {"title": "$.book.title", "book_id": "$.book.id"}
    assert any("unknown param 'admin'" in r for r in out.reasons)
    assert any("unknown field path '$.secret'" in r for r in out.reasons)
    assert not hasattr(out, "url") and not hasattr(out, "requires_confirmation")


def test_enforce_falls_back_when_nothing_is_kept() -> None:
    out = enforce({"description": "", "params": [], "fields": []}, inferred(), FLOW)
    assert out.fields == rule_labels(inferred(), FLOW).fields
    assert len(out.reasons) == 2


class FakeClient:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response, self.error = response, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def tool_use(data: dict[str, Any]) -> Any:
    block = SimpleNamespace(type="tool_use", name="label_action", input=data)
    return SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(type="text", text="ok"), block])


async def test_label_with_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    client = FakeClient(
        tool_use(
            {
                "description": "Find books.",
                "params": [],
                "fields": [{"path": "$.book.title", "name": "title", "keep": True}],
            }
        )
    )
    out = await label(inferred(), FLOW, visible=["The Quiet Harbour"], client=client)
    assert (
        out.source == "llm" and out.description == "Find books." and out.fields == {"title": "$.book.title"}
    )
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["tools"] == [LABEL_TOOL] and LABEL_TOOL["strict"] is True
    assert call["tool_choice"] == {"type": "auto"}
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    prompt = call["messages"][0]["content"]
    assert (
        "The Quiet Harbour" in prompt and "$.book.title" in prompt and "GET {{base_url}}/api/search" in prompt
    )


async def test_model_from_env_without_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-5-5")
    client = FakeClient(tool_use({"description": "d", "params": [], "fields": []}))
    await label(inferred(), FLOW, client=client)
    assert client.calls[0]["model"] == "claude-haiku-5-5" and "fallbacks" not in client.calls[0]


@pytest.mark.parametrize(
    "client, reason",
    [
        (FakeClient(SimpleNamespace(stop_reason="refusal", content=[])), "declined"),
        (FakeClient(SimpleNamespace(stop_reason="end_turn", content=[])), "didn't call label_action"),
        (FakeClient(error=RuntimeError("boom")), "call failed (RuntimeError)"),
    ],
)
async def test_falls_back_to_rules(client: FakeClient, reason: str) -> None:
    out = await label(inferred(), FLOW, client=client)
    assert out.source == "rules" and any(reason in r for r in out.reasons)


async def test_no_credentials_means_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    out = await label(inferred(), FLOW)
    assert out.source == "rules" and not out.reasons
