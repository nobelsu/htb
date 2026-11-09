"""spider.flowmap.record.flows: the flow model, files, the codegen converter and replay (against a local
page)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spider.flowmap.record.flows import (
    Click,
    Fill,
    Flow,
    FlowError,
    Goto,
    Input,
    LocatorCall,
    Press,
    Select,
    WaitFor,
    flow_path,
    from_codegen,
    list_flows,
    load_flow,
    play,
    render,
    save_flow,
)

CODEGEN_SYNC = """
import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://www.example.com/")
    page.get_by_placeholder("Search products").click()
    page.get_by_placeholder("Search products").fill("blue mug")
    page.get_by_label("Sort by").select_option("price")
    page.get_by_placeholder("Search products").press("Enter")
    expect(page.locator("#results .card").first).to_be_visible()
    page.get_by_role("link", name="Next", exact=True).click()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
"""

CODEGEN_ASYNC = """
import asyncio
from playwright.async_api import Playwright, async_playwright, expect


async def run(playwright: Playwright) -> None:
    browser = await playwright.chromium.launch(headless=False)
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto("https://shop.test/search?q=blue%20mug")
    await page.locator("ul.results > li").nth(2).click()
    await page.keyboard.press("Escape")
    await context.close()
    await browser.close()
"""


def minimal(**over: object) -> dict:
    data: dict = {
        "domain": "example.com",
        "action": "search",
        "inputs": {"query": {"examples": ["mug"]}},
        "steps": [{"op": "goto", "url": "https://example.com/?q={{query}}"}],
    }
    data.update(over)
    return data


# ---------- model ----------


def test_minimal_flow_and_example_values() -> None:
    flow = Flow.model_validate(minimal())
    assert flow.inputs["query"].marker is True
    assert flow.example_values() == {"query": "mug"}


def test_marker_false_needs_two_different_examples() -> None:
    with pytest.raises(ValueError, match="two different real values"):
        Input(marker=False, examples=["a", "a"])
    assert Input(marker=False, examples=["a", "b"]).examples == ["a", "b"]


def test_inputs_need_an_example() -> None:
    with pytest.raises(ValueError):
        Input(examples=[])


@pytest.mark.parametrize(
    "over, message",
    [
        ({"steps": [{"op": "click", "target": "#go"}]}, "starts with a goto"),
        ({"inputs": {}}, "undeclared inputs: query"),
        (
            {"inputs": {"query": {"examples": ["x"]}, "extra": {"examples": ["y"]}}},
            "never used in a step: extra",
        ),
        ({"action": "Search-Things"}, "snake_case"),
        ({"domain": "https://example.com"}, "lowercase host"),
    ],
)
def test_invalid_flows(over: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Flow.model_validate(minimal(**over))


def test_wait_for_needs_something() -> None:
    with pytest.raises(ValueError, match="needs a target"):
        WaitFor()


def test_unknown_step_op_rejected() -> None:
    with pytest.raises(ValueError):
        Flow.model_validate(minimal(steps=[{"op": "goto", "url": "https://x/{{query}}"}, {"op": "hover"}]))


def test_render_encodes_only_in_urls() -> None:
    assert render("{{q}}", {"q": "a b/c"}) == "a b/c"
    assert render("https://x/?q={{ q }}", {"q": "a b/c"}, url=True) == "https://x/?q=a%20b%2Fc"
    with pytest.raises(FlowError, match="no value for input 'q'"):
        render("{{q}}", {})


# ---------- files ----------


def test_save_load_and_list(tmp_path: Path) -> None:
    flow = Flow.model_validate(minimal())
    path = save_flow(flow, tmp_path)
    assert path == flow_path(tmp_path, "example.com", "search") == tmp_path / "example.com" / "search.json"
    assert load_flow(path) == flow
    assert "results_selector" not in json.loads(path.read_text())  # empty fields left out: short to read
    other = Flow.model_validate(minimal(domain="other.org", action="find"))
    save_flow(other, tmp_path)
    assert [p.name for p in list_flows(tmp_path)] == ["search.json", "find.json"]
    assert [p.name for p in list_flows(tmp_path, "other.org")] == ["find.json"]


def test_load_flow_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with pytest.raises(FlowError, match="can't read"):
        load_flow(bad)
    bad.write_text(json.dumps(minimal(inputs={})))
    with pytest.raises(FlowError, match="not valid"):
        load_flow(bad)


# ---------- codegen ----------


def test_codegen_sync_script() -> None:
    flow = from_codegen(
        CODEGEN_SYNC,
        action="search_products",
        inputs={"query": "blue mug"},
        examples={"sort_by": ["relevance"]},
        results_selector="#results .card h3",
    )
    assert flow.domain == "example.com"  # www. dropped
    assert flow.inputs["query"] == Input(marker=True, examples=["blue mug"])
    assert flow.inputs["sort_by"] == Input(
        marker=False, examples=["price", "relevance"]
    )  # named from its label
    assert [s.op for s in flow.steps] == ["goto", "click", "fill", "select", "press", "wait_for", "click"]
    fill = flow.steps[2]
    assert isinstance(fill, Fill) and fill.value == "{{query}}"
    assert fill.target == [LocatorCall(method="get_by_placeholder", args=["Search products"])]
    sel = flow.steps[3]
    assert isinstance(sel, Select) and sel.value == "{{sort_by}}"
    wait = flow.steps[5]
    assert isinstance(wait, WaitFor)
    assert wait.target == [
        LocatorCall(method="locator", args=["#results .card"]),
        LocatorCall(method="first"),
    ]
    nxt = flow.steps[6]
    assert isinstance(nxt, Click)
    assert nxt.target == [
        LocatorCall(method="get_by_role", args=["link"], kwargs={"name": "Next", "exact": True})
    ]


def test_codegen_async_script_templates_the_url() -> None:
    flow = from_codegen(CODEGEN_ASYNC, action="open_result", inputs={"query": "blue mug"})
    goto = flow.steps[0]
    assert isinstance(goto, Goto) and goto.url == "https://shop.test/search?q={{query}}"
    click = flow.steps[1]
    assert isinstance(click, Click)
    assert click.target == [
        LocatorCall(method="locator", args=["ul.results > li"]),
        LocatorCall(method="nth", args=[2]),
    ]
    press = flow.steps[2]
    assert isinstance(press, Press) and press.target is None and press.key == "Escape"
    assert flow.domain == "shop.test"


def test_codegen_unmapped_fill_becomes_named_input() -> None:
    src = 'page.goto("https://a.test/")\npage.get_by_label("Company name").fill("acme")\n'
    flow = from_codegen(src, action="find")
    assert flow.inputs == {"company_name": Input(examples=["acme"])}


def test_codegen_select_without_second_value_fails_loudly() -> None:
    with pytest.raises(FlowError, match="give a second real value"):
        from_codegen(CODEGEN_SYNC, action="search_products", inputs={"query": "blue mug"})


@pytest.mark.parametrize(
    "src, message",
    [
        ('page.get_by_text("x").click()\n', "doesn't start by opening a page"),
        ('page.goto("https://a.test/")\npage.mouse.wheel(0, 100)\n', "wheel"),
        ('page.goto("https://a.test/")\nwith page.expect_popup() as p:\n    page.click("a")\n', "popup"),
        ('page.goto("https://a.test/")\npage.get_by_text(re.compile("x")).click()\n', "isn't a plain value"),
        ("def (:\n", "not a Python codegen script"),
        ("x = 1\n", "no steps"),
    ],
)
def test_codegen_errors(src: str, message: str) -> None:
    with pytest.raises(FlowError, match=message):
        from_codegen(src, action="act")


def test_codegen_mapped_value_not_found() -> None:
    with pytest.raises(FlowError, match="not found in the recording: query"):
        from_codegen('page.goto("https://a.test/")\n', action="act", inputs={"query": "nothing"})


# ---------- replay (needs Chromium) ----------

PAGE = """<!doctype html><title>t</title>
<input placeholder="Search" id="q">
<select id="sort"><option value="a">A</option><option value="b">B</option></select>
<button id="go" onclick="document.getElementById('out').textContent =
  document.getElementById('q').value + '/' + document.getElementById('sort').value">Go</button>
<p id="out"></p>"""


@pytest.mark.browser
async def test_play_runs_every_step(tmp_path: Path) -> None:
    playwright = pytest.importorskip("playwright.async_api")
    (tmp_path / "index.html").write_text(PAGE)
    flow = Flow(
        domain="local.test",
        action="search",
        inputs={"query": Input(examples=["mug"]), "sort": Input(marker=False, examples=["a", "b"])},
        steps=[
            Goto(url=(tmp_path / "index.html").as_uri() + "?q={{query}}"),
            Fill(target=[LocatorCall(method="get_by_placeholder", args=["Search"])], value="{{query}}"),
            Select(target="#sort", value="{{sort}}"),
            Click(target=[LocatorCall(method="get_by_role", args=["button"], kwargs={"name": "Go"})]),
            WaitFor(target="#out:not(:empty)"),
        ],
    )
    seen: list[int] = []
    async with playwright.async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as e:  # no Chromium installed
            pytest.skip(f"no browser: {e}")
        page = await browser.new_page()
        await play(page, flow, {"query": "blue mug", "sort": "b"}, on_step=lambda i, s: seen.append(i))
        assert await page.text_content("#out") == "blue mug/b"
        assert seen == [0, 1, 2, 3, 4]
        bad = flow.model_copy(update={"steps": [*flow.steps[:1], Click(target="#missing")]})
        with pytest.raises(FlowError, match=r"step 2 \(click\) failed"):
            await play(page, bad, {"query": "x", "sort": "a"}, timeout_ms=300)
        await browser.close()


def test_committed_bookshop_flows_match_the_tests() -> None:
    """flows/bookshop.test/ is what `python -m spider.flowmap map bookshop.test` uses with the shop on port
    8910."""
    from tests.flowmap.conftest import book_flow, cart_flow, search_flow

    root = Path(__file__).resolve().parents[3] / "flows"
    for make in (search_flow, book_flow, cart_flow):
        expected = make("http://127.0.0.1:8910")
        saved = load_flow(flow_path(root, "bookshop.test", expected.action))
        assert saved.model_copy(update={"kind": None}) == expected
