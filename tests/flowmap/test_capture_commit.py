"""A write's replay holds back every click that commits it, not only the last one."""

from __future__ import annotations

from pathlib import Path

import pytest

from flowmap.browser import BrowserSetup
from flowmap.capture import capture_flow, commit_step
from flowmap.flows import Click, Flow, Goto, Input, LocatorCall


def _cart_twice(base: str) -> Flow:
    add = [LocatorCall(method="get_by_role", args=["button"], kwargs={"name": "Add to cart"})]
    return Flow(
        domain="bookshop.test",
        action="add_to_cart",
        kind="write",
        inputs={"book_id": Input(examples=["b002", "b003"])},
        steps=[
            Goto(url=base + "/book/{{book_id}}"),
            Click(target=add),  # a first try
            Goto(url=base + "/book/{{book_id}}"),
            Click(target=add),  # the same again: the recording's last click
        ],
    )


def test_the_first_click_that_reads_like_a_change_arms_it() -> None:
    flow = _cart_twice("http://x.test")
    assert commit_step(flow) == 1
    link = [LocatorCall(method="get_by_role", args=["link"], kwargs={"name": "Books"})]
    flow.steps.insert(1, Click(target=link))  # a plain link before it isn't a change
    assert commit_step(flow) == 2


@pytest.mark.browser
async def test_every_change_click_is_held_back_on_replay(bookshop: str, tmp_path: Path) -> None:
    pytest.importorskip("playwright.async_api")
    from tests.flowmap.site import state as site

    before = {k: list(v) for k, v in site.carts.items()}
    runs = await capture_flow(
        _cart_twice(bookshop), setup=BrowserSetup(channel="chromium", headless=True), pause_s=0
    )
    assert runs and all(r.kind in ("real", "marker") for r in runs)
    assert {k: list(v) for k, v in site.carts.items()} == before  # the site never got either click
