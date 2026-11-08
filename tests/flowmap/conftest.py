from __future__ import annotations

from collections.abc import Iterator

import pytest

from flowmap.flows import Click, Fill, Flow, Goto, Input, LocatorCall, Press, Select, WaitFor
from tests.conftest import LiveServer
from tests.sites.bookshop import app, state


@pytest.fixture(autouse=True)
def no_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """``python -m flowmap`` loads .env; in tests that would bring in a real ANTHROPIC_API_KEY (paid labelling
    calls, and other tests then see the key too). Tests run with neither."""
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def bookshop_server() -> Iterator[LiveServer]:
    with LiveServer(app) as server:
        yield server


@pytest.fixture
def bookshop(bookshop_server: LiveServer) -> Iterator[str]:
    state.reset()
    yield bookshop_server.url
    state.reset()


def search_flow(base: str) -> Flow:
    return Flow(
        domain="bookshop.test",
        action="search_books",
        inputs={
            "query": Input(examples=["the", "harbour"]),
            "sort": Input(marker=False, examples=["relevance", "price"]),
        },
        steps=[
            Goto(url=base + "/"),
            Fill(target=[LocatorCall(method="get_by_placeholder", args=["Search books"])], value="{{query}}"),
            Select(target=[LocatorCall(method="get_by_label", args=["Sort by"])], value="{{sort}}"),
            Press(target=[LocatorCall(method="get_by_placeholder", args=["Search books"])], key="Enter"),
            WaitFor(target="#count:not(:empty)"),
        ],
        results_selector=".result h3",
        next_page="#next",
    )


def book_flow(base: str) -> Flow:
    return Flow(
        domain="bookshop.test",
        action="get_book",
        inputs={"book_id": Input(examples=["b001", "b005"])},
        steps=[Goto(url=base + "/book/{{book_id}}")],
        results_selector="#title",
    )


def cart_flow(base: str) -> Flow:
    return Flow(
        domain="bookshop.test",
        action="add_to_cart",
        inputs={"book_id": Input(examples=["b002", "b003"])},
        steps=[
            Goto(url=base + "/book/{{book_id}}"),
            WaitFor(target="#title:not(:empty)"),
            Click(
                target=[LocatorCall(method="get_by_role", args=["button"], kwargs={"name": "Add to cart"})]
            ),
            WaitFor(target="#cart:not(:empty)"),
        ],
    )
