"""A replayed goto that the page's own navigation cancelled is opened again."""

from __future__ import annotations

from typing import Any

from flowmap.flows import Goto, _play_step


class Page:
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        self.opened: list[str] = []

    async def goto(self, url: str, timeout: int) -> None:
        self.opened.append(url)
        if self.errors:
            raise RuntimeError(self.errors.pop(0))

    async def wait_for_load_state(self, state: str, timeout: int) -> None:
        return None


async def test_an_aborted_goto_is_opened_again() -> None:
    page: Any = Page(["Page.goto: net::ERR_ABORTED at https://x.test/s?q=a"])
    await _play_step(page, Goto(url="https://x.test/s?q={{q}}"), {"q": "a"}, 1000)
    assert page.opened == ["https://x.test/s?q=a", "https://x.test/s?q=a"]


async def test_other_goto_errors_still_fail() -> None:
    page: Any = Page(["Page.goto: net::ERR_NAME_NOT_RESOLVED"])
    try:
        await _play_step(page, Goto(url="https://x.test/"), {}, 1000)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the error")
    assert len(page.opened) == 1
