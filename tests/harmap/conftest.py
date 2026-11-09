from __future__ import annotations

import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import pytest
from fixture_site import FixtureSite


@pytest.fixture
def site():
    with FixtureSite() as running:
        yield running


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: str

    def json(self):
        return json.loads(self.body)


class Client:
    """Minimal cookie-keeping HTTP client for poking the fixture site from tests."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def cookie(self, name: str) -> str | None:
        return next((c.value for c in self.jar if c.name == name), None)

    def request(self, method: str, path: str, data: bytes | None = None, headers=None) -> Response:
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers or {})
        try:
            with self.opener.open(req, timeout=5) as r:
                return Response(r.status, dict(r.headers), r.read().decode())
        except urllib.error.HTTPError as e:
            return Response(e.code, dict(e.headers), e.read().decode())

    def get(self, path: str, headers=None) -> Response:
        return self.request("GET", path, headers=headers)

    def post_form(self, path: str, form: dict[str, str]) -> Response:
        body = urllib.parse.urlencode(form).encode()
        return self.request(
            "POST", path, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

    def login(self) -> None:
        r = self.post_form("/login", {"username": "demo", "password": "demo", "next": "/"})
        assert r.status == 200, r


@pytest.fixture
def client(site):
    return Client(site.url)


@pytest.fixture(scope="session")
def chromium():
    """Skip browser tests when Playwright's Chromium isn't installed (`playwright install chromium`)."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
    except Exception as e:  # pragma: no cover - depends on the machine
        pytest.skip(f"Playwright Chromium unavailable: {e}")
    return True


MARKERS = {"m1": "zqxv8841", "m2": "kjtr5527"}
REAL = "software"


@pytest.fixture(scope="session")
def fixture_captures(chromium, tmp_path_factory):
    """Capture the fixture site's tasks once per session. Returns a callable(task, run) -> bundle dir."""
    from fixture_site import scripts

    from harmap.capture.record import CaptureConfig, record

    root = tmp_path_factory.mktemp("captures")
    site = FixtureSite().start()
    state = root / "login.state.json"
    site.login_state(state)  # one logged-in session shared by every run, like a real operator
    tasks = {
        "search": ("query", scripts.SEARCH, scripts.SEARCH_PAGE2, "read"),
        "add_note": ("text", scripts.ADD_NOTE, None, "write"),
    }
    done: dict[tuple[str, str], object] = {}

    def capture(task: str, run: str):
        if (task, run) in done:
            return done[(task, run)]
        param, script, page2_script, kind = tasks[task]
        value = MARKERS.get(run, REAL)
        if task == "add_note" and run not in MARKERS:
            value = "Call Acme about the Q3 renewal"
        cfg = CaptureConfig(
            start_url=site.url + "/",
            task=task,
            run=run,
            markers={param: value} if run in MARKERS else {},
            real_inputs={} if run in MARKERS else {param: value},
            kind=kind,
            description={"search": "Search companies by keyword", "add_note": "Save a note"}[task],
            state=state,
            out=root,
            headless=True,
            script=page2_script if run == "page2" else script,
        )
        done[(task, run)] = record(cfg)
        return done[(task, run)]

    capture.root = root
    capture.site = site
    yield capture
    site.stop()


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """Mapper tests never call the Anthropic API, even when the developer has a key set."""
    monkeypatch.setenv("MAPPER_NO_LLM", "1")
