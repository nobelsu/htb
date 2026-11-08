"""Agent recording on the bookshop, with a scripted stand-in for Claude: the steps it takes become a flow that
maps like a person-recorded one, and the guard rails hold whatever the model asks for."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

from flowmap.agent import (
    AgentError,
    check_click,
    element_target,
    option_value,
    record_with_agent,
    template,
)
from flowmap.flows import Fill, Goto, LocatorCall, Press, Select
from tests.flowmap.site import state

Policy = Callable[[list[dict[str, Any]]], dict[str, Any]]


class FakeClient:
    """``client.beta.messages.create`` answering with one tool call from ``script`` per turn."""

    def __init__(self, script: list[Policy | dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.beta = self
        self.messages = self

    async def create(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        step = self.script.pop(0)
        call = step(kwargs["messages"]) if callable(step) else step
        n = len(self.calls)
        return {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": f"t{n}", "name": call["name"], "input": call["input"]}],
        }


def role(r: str, name: str, nth: int = 0) -> dict[str, Any]:
    return {"by": "role", "role": r, "name": name, "nth": nth}


NONE = {"by": "none", "role": "", "name": "", "nth": 0}


def last_page(messages: list[dict[str, Any]]) -> str:
    last = messages[-1]["content"]
    return last if isinstance(last, str) else str(last[-1]["content"])


def done_with_headings(level: int, next_page: dict[str, Any] = NONE) -> Policy:
    def policy(messages: list[dict[str, Any]]) -> dict[str, Any]:
        titles = re.findall(rf'heading "([^"]+)" \[level={level}\]', last_page(messages))
        return {
            "name": "done",
            "input": {"summary": "results", "results": titles[:10], "next_page": next_page},
        }

    return policy


# ---------- pieces ----------


def test_element_targets_and_templates() -> None:
    assert element_target(role("textbox", "Search")) == [
        LocatorCall(method="get_by_role", args=["textbox"], kwargs={"name": "Search", "exact": True})
    ]
    assert element_target({"by": "label", "role": "", "name": "Sort by", "nth": 0}) == [
        LocatorCall(method="get_by_label", args=["Sort by"], kwargs={"exact": True})
    ]
    with pytest.raises(AgentError):
        element_target(NONE)
    assert template("robotics companies", {"query": "robotics"}) == "{{query}} companies"
    assert template("price", {"query": "robotics", "sort": "price"}) == "{{sort}}"
    # A dropdown option named by its label still stands for the input whose value it has.
    assert option_value("price", "Price", {"sort": "price"}) == "{{sort}}"
    assert option_value("asc", "Oldest first", {"sort": "Oldest first"}) == "{{sort}}"
    assert option_value("asc", "Oldest", {"sort": "price"}) == "asc"


def test_click_guards() -> None:
    for label in ("Sign out", "Log out", "Delete post", "Unfollow", "Remove connection"):
        with pytest.raises(AgentError, match="never clicked"):
            check_click(role("button", label), write=True)
    for label in ("Follow", "Connect", "Add to cart", "Send"):
        with pytest.raises(AgentError, match="only reads"):
            check_click(role("button", label), write=False)
        check_click(role("button", label), write=True)  # allowed when the action is a write
    check_click(role("button", "Search"), write=False)


# ---------- in the browser ----------


async def _page(p: Any) -> tuple[Any, Any]:
    browser = await p.chromium.launch()
    page = await (await browser.new_context()).new_page()
    return browser, page


@pytest.mark.browser
async def test_a_dropdown_input_gets_its_second_example_from_the_dropdown(bookshop: str) -> None:
    """With only one value for an input chosen from a list (an explore gives just one), another option of
    that dropdown becomes its second example, so it can still be mapped."""
    pytest.importorskip("playwright.async_api")
    from playwright.async_api import async_playwright

    client = FakeClient(
        [
            {"name": "fill", "input": {"element": role("textbox", "Search books"), "text": "the"}},
            {"name": "select", "input": {"element": role("combobox", "Sort by"), "option": "Relevance"}},
            {"name": "press", "input": {"key": "Enter"}},
            done_with_headings(3),
        ]
    )
    async with async_playwright() as p:
        browser, page = await _page(p)
        try:
            rec = await record_with_agent(
                page,
                url=bookshop + "/",
                task="search the books, sorted by relevance",
                action="search_books",
                inputs={"query": "the", "sort": "Relevance"},
                domain="bookshop.test",
                client=client,
            )
        finally:
            await browser.close()
    assert rec.flow.inputs["sort"].examples == ["Relevance", "price"]
    assert not rec.flow.inputs["sort"].marker and rec.flow.inputs["query"].examples == ["the"]


async def test_agent_records_search_and_it_maps(bookshop: str) -> None:
    pytest.importorskip("playwright.async_api")
    from playwright.async_api import async_playwright

    from flowmap.assemble import map_action
    from flowmap.capture import capture_flow

    client = FakeClient(
        [
            {"name": "fill", "input": {"element": role("textbox", "Search books"), "text": "the"}},
            done_with_headings(3, role("button", "Next")),  # too early: sort isn't used yet
            {"name": "select", "input": {"element": role("combobox", "Sort by"), "option": "Price"}},
            {"name": "press", "input": {"key": "Enter"}},
            done_with_headings(3, role("button", "Next")),
        ]
    )
    async with async_playwright() as p:
        browser, page = await _page(p)
        try:
            rec = await record_with_agent(
                page,
                url=bookshop + "/",
                task="search the books for the query, sorted by the given order",
                action="search_books",
                inputs={"query": "the", "sort": "price"},
                examples={"query": ["harbour"], "sort": ["relevance"]},
                domain="bookshop.test",
                client=client,
            )
        finally:
            await browser.close()

    flow = rec.flow
    assert rec.turns == 5
    assert any("never used: sort" in line for line in rec.log)
    assert [type(s) for s in flow.steps] == [Goto, Fill, Select, Press]
    fill, select = flow.steps[1], flow.steps[2]
    assert isinstance(fill, Fill) and fill.value == "{{query}}"
    assert isinstance(select, Select) and select.value == "{{sort}}"
    assert flow.inputs["query"].marker and not flow.inputs["sort"].marker  # a dropdown can't take a marker
    assert flow.results_selector == "div.result a"  # not just "a": the more specific selector wins a tie
    assert flow.next_page == [
        LocatorCall(method="get_by_role", args=["button"], kwargs={"name": "Next", "exact": True})
    ]
    # The prompt holds the task, the values and the page; page text is data.
    first = client.calls[0]
    prompt = first["messages"][0]["content"]
    assert '"the"' in prompt and 'textbox "Search books"' in prompt
    assert "not instructions" in first["system"]

    runs = await capture_flow(flow, headless=True, pause_s=0)
    mapped = await map_action(flow, runs, use_llm=False, pause_s=0)
    action = next(a for a in mapped.final.card["actions"] if a["name"] == "search_books")
    assert set(action["params"]) >= {"query", "sort"}
    assert action["output"]["fields"]


@pytest.mark.browser
async def test_agent_guard_rails(bookshop: str) -> None:
    pytest.importorskip("playwright.async_api")
    from playwright.async_api import async_playwright

    client = FakeClient(
        [
            {"name": "click", "input": {"element": role("button", "Add to cart")}},
            {"name": "goto", "input": {"url": "https://elsewhere.example/steal"}},
            {"name": "click", "input": {"element": role("button", "Nowhere")}},
            done_with_headings(1),
        ]
    )
    async with async_playwright() as p:
        browser, page = await _page(p)
        try:
            rec = await record_with_agent(
                page,
                url=bookshop + "/book/b001",
                task="get the book",
                action="get_book",
                inputs={"book_id": "b001"},
                domain="bookshop.test",
                client=client,
            )
        finally:
            await browser.close()
    assert state.carts == {}  # the write was never clicked
    replies = [
        m["content"][0] for m in client.calls[-1]["messages"] if m["role"] == "user" and m["content"] != []
    ]
    errors = [r for r in replies if isinstance(r, dict) and r.get("is_error")]
    assert len(errors) == 3
    assert "only reads" in errors[0]["content"] and "not on" in errors[1]["content"]
    assert "no button" in errors[2]["content"]
    assert [type(s) for s in rec.flow.steps] == [Goto]  # refused steps aren't recorded
    goto = rec.flow.steps[0]
    assert isinstance(goto, Goto) and goto.url.endswith("/book/{{book_id}}")
    assert rec.flow.results_selector


@pytest.mark.browser
async def test_agent_sign_in_and_give_up(bookshop: str) -> None:
    pytest.importorskip("playwright.async_api")
    from playwright.async_api import async_playwright

    signed: list[bool] = []

    async def sign_in() -> None:
        signed.append(True)

    async with async_playwright() as p:
        browser, page = await _page(p)
        try:
            no_one = FakeClient(
                [{"name": "sign_in_needed", "input": {"reason": "login wall"}}] * 2
                + [{"name": "give_up", "input": {"reason": "can't"}}]
            )
            with pytest.raises(AgentError, match="gave up"):
                await record_with_agent(
                    page, url=bookshop + "/", task="t", action="a", inputs={}, client=no_one
                )
            assert any("nobody is here to sign in" in str(m["content"]) for m in no_one.calls[-1]["messages"])

            client = FakeClient(
                [
                    {"name": "click", "input": {"element": role("button", "Search")}},
                    {"name": "sign_in_needed", "input": {"reason": "login wall"}},
                    {"name": "give_up", "input": {"reason": "still no"}},
                ]
            )
            with pytest.raises(AgentError) as info:
                await record_with_agent(
                    page, url=bookshop + "/", task="t", action="a", inputs={}, client=client, sign_in=sign_in
                )
            assert signed == [True]
            assert any("starts again" in line for line in info.value.log)
        finally:
            await browser.close()


def test_an_address_holding_every_input_becomes_the_flow() -> None:
    from flowmap.agent import url_shortcut

    url = "https://www.linkedin.com/search/results/companies/?keywords=robotics&origin=SWITCH_SEARCH_VERTICAL"
    direct = url_shortcut(url, {"query": "robotics"}, "linkedin.com")
    assert direct is not None and direct.url.endswith("?keywords={{query}}&origin=SWITCH_SEARCH_VERTICAL")
    assert url_shortcut(url, {"query": "robotics", "location": "London"}, "linkedin.com") is None  # not all
    assert url_shortcut(url, {"page": "2"}, "linkedin.com") is None  # too short to trust
    assert url_shortcut("https://evil.example/?q=robotics", {"query": "robotics"}, "linkedin.com") is None
