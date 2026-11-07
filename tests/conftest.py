import copy
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from tests.fixture_site.app import app, state

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_CARD: dict[str, Any] = json.loads((ROOT / "tests" / "fixture_site.json").read_text())


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class LiveServer:
    def __init__(self, asgi_app: Any) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(asgi_app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "LiveServer":
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *_: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture(scope="session")
def site() -> Iterator[LiveServer]:
    with LiveServer(app) as server:
        yield server


@pytest.fixture
def site_url(site: LiveServer) -> Iterator[str]:
    state.reset()
    yield site.url
    state.reset()


@pytest.fixture
def fixture_card(site_url: str) -> dict[str, Any]:
    """The fixture card pointed at the live test server."""
    card = copy.deepcopy(FIXTURE_CARD)
    card["base_url"] = site_url
    card["connect_providers"][0]["login_url"] = f"{site_url}/login"
    return card


def hits(site_url: str) -> dict[str, int]:
    return httpx.get(f"{site_url}/_admin/hits").json()
