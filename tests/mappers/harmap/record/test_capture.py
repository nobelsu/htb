import json
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fixture_site import scripts

from spider.mappers.harmap.record import driver
from spider.mappers.harmap.record.capture import CaptureConfig, CaptureError, record

META_KEYS = {
    "bundle_version",
    "domain",
    "start_url",
    "task",
    "task_description",
    "run",
    "markers",
    "real_inputs",
    "page",
    "kind_hint",
    "driver",
    "goal_marks",
    "started_at",
    "ended_at",
    "mapper_version",
}


def _har_entries(bundle: Path) -> list[dict]:
    return json.loads((bundle / "traffic.har").read_text())["log"]["entries"]


def _actions(bundle: Path) -> list[dict]:
    return [json.loads(line) for line in (bundle / "actions.jsonl").read_text().splitlines()]


# --- no browser needed ---------------------------------------------------------------------


def test_config_rejects_mismatched_inputs(tmp_path):
    base = dict(start_url="http://127.0.0.1:1/", task="search", out=tmp_path)
    with pytest.raises(CaptureError, match="--marker"):
        CaptureConfig(run="m1", real_inputs={"query": "x"}, **base).check()
    with pytest.raises(CaptureError, match="--input"):
        CaptureConfig(run="real", markers={"query": "x"}, **base).check()
    with pytest.raises(CaptureError, match="--script"):
        CaptureConfig(run="m1", headless=True, **base).check()
    with pytest.raises(CaptureError, match="state"):
        CaptureConfig(run="m1", state=tmp_path / "missing.json", **base).check()


def test_config_domain_is_registrable():
    cfg = CaptureConfig(start_url="https://www.linkedin.com/feed/", task="t", run="m1")
    assert cfg.domain == "linkedin.com"


def test_script_substitution_and_validation(tmp_path):
    assert driver.substitute("q={{params.query}}!", {"query": "zqxv8841"}) == "q=zqxv8841!"
    with pytest.raises(driver.ScriptError, match="query"):
        driver.substitute("{{ params.query }}", {})
    path = tmp_path / "s.jsonl"
    path.write_text('# comment\n{"type": "click", "selector": "a"}\n\n{"type": "mark"}\n')
    assert [s["type"] for s in driver.load_script(path)] == ["click", "mark"]
    with pytest.raises(driver.ScriptError):
        driver.load_script([{"type": "teleport"}])


def test_navigate_fills_in_params_url_encoded():
    """The explorer's LLM puts inputs into URLs; they used to reach the site as `{{params.query}}`."""
    visited = []
    page = SimpleNamespace(goto=lambda url, wait_until: visited.append(url), wait_for_timeout=lambda ms: None)
    tracker = SimpleNamespace(action=lambda: None, wait_for_action=lambda page, timeout_ms: None)
    steps = [{"type": "navigate", "url": "/search/results/companies/?keywords={{params.query}}&o=1"}]
    driver.run_script(
        page,
        steps,
        start_url="https://example.com/feed/",
        params={"query": "robotics & AI"},
        tracker=tracker,
        on_mark=lambda: None,
    )
    assert visited == ["https://example.com/search/results/companies/?keywords=robotics%20%26%20AI&o=1"]
    with pytest.raises(driver.ScriptError, match="query"):
        driver.run_script(
            page, steps, start_url="https://example.com/", params={}, tracker=tracker, on_mark=lambda: None
        )


# --- browser -------------------------------------------------------------------------------


@pytest.fixture
def m1_bundle(chromium, site, tmp_path):
    state = tmp_path / "login.state.json"
    site.login_state(state)
    cfg = CaptureConfig(
        start_url=site.url + "/",
        task="search",
        run="m1",
        markers={"query": "zqxv8841"},
        description="Search companies by keyword",
        state=state,
        out=tmp_path / "captures",
        headless=True,
        script=scripts.SEARCH,
    )
    return record(cfg)


@pytest.mark.browser
def test_bundle_layout_and_meta(m1_bundle, site, tmp_path):
    assert m1_bundle == tmp_path / "captures" / "127.0.0.1" / "search" / "m1"
    assert sorted(p.name for p in m1_bundle.iterdir()) == [
        "actions.jsonl",
        "meta.json",
        "state.json",
        "traffic.har",
    ]
    meta = json.loads((m1_bundle / "meta.json").read_text())
    assert set(meta) == META_KEYS
    assert meta["bundle_version"] == "1"
    assert meta["domain"] == "127.0.0.1"
    assert meta["markers"] == {"query": "zqxv8841"} and meta["real_inputs"] == {}
    assert meta["page"] == 1 and meta["kind_hint"] == "read" and meta["driver"] == "script"
    assert len(meta["goal_marks"]) == 1
    assert meta["started_at"] < meta["goal_marks"][0] < meta["ended_at"]


@pytest.mark.browser
def test_sensitive_files_are_private(m1_bundle):
    for name in ("traffic.har", "state.json"):
        mode = stat.S_IMODE((m1_bundle / name).stat().st_mode)
        assert mode == 0o600, (name, oct(mode))


@pytest.mark.browser
def test_har_has_search_and_session_calls(m1_bundle):
    entries = _har_entries(m1_bundle)
    paths = [urlsplit(e["request"]["url"]).path for e in entries]
    assert "/api/session" in paths
    search = [e for e in entries if urlsplit(e["request"]["url"]).path == "/api/search"]
    assert len(search) == 1
    query = parse_qs(urlsplit(search[0]["request"]["url"]).query)
    assert query == {"q": ["zqxv8841"], "start": ["0"]}
    assert search[0]["response"]["status"] == 200
    body = search[0]["response"]["content"]["text"]  # content embedded
    assert json.loads(body)["total"] == 0
    names = {h["name"].lower() for h in search[0]["request"]["headers"]}
    assert {"x-token", "csrf-token"} <= names


@pytest.mark.browser
def test_actions_log_input_click_navigate(m1_bundle):
    actions = _actions(m1_bundle)
    assert [a["t"] for a in actions] == sorted(a["t"] for a in actions)
    inputs = [a for a in actions if a["type"] == "input"]
    assert inputs == [
        {"t": inputs[0]["t"], "type": "input", "selector": "#q", "value": "zqxv8841", "url": inputs[0]["url"]}
    ]
    clicks = [a for a in actions if a["type"] == "click"]
    assert clicks and clicks[0]["text"] == "Search"
    assert clicks[0]["selector"] == 'button[type="submit"]'
    navs = [urlsplit(a["url"]).path for a in actions if a["type"] == "navigate"]
    assert navs[:2] == ["/", "/search"]
    assert inputs[0]["t"] < clicks[0]["t"]


@pytest.mark.browser
def test_state_json_has_login_cookies(m1_bundle):
    state = json.loads((m1_bundle / "state.json").read_text())
    assert {c["name"] for c in state["cookies"]} >= {"sid", "JSESSIONID"}


@pytest.mark.browser
def test_existing_bundle_needs_force(m1_bundle, site, tmp_path):
    cfg = CaptureConfig(
        start_url=site.url + "/",
        task="search",
        run="m1",
        markers={"query": "zqxv8841"},
        out=tmp_path / "captures",
        headless=True,
        script=scripts.SEARCH,
    )
    with pytest.raises(CaptureError, match="--force"):
        record(cfg)


@pytest.mark.browser
def test_password_never_logged(chromium, site, tmp_path):
    cfg = CaptureConfig(
        start_url=site.url + "/",
        task="login",
        run="real",
        real_inputs={"password": "hunter2-secret"},
        out=tmp_path / "captures",
        headless=True,
        script=scripts.LOGIN,
    )
    bundle = record(cfg)
    text = (bundle / "actions.jsonl").read_text()
    assert "hunter2-secret" not in text
    pw = [a for a in _actions(bundle) if a["type"] == "input" and "password" in a["selector"]]
    assert pw and pw[0]["value"] == "[redacted]"


@pytest.mark.browser
def test_cli_headless_capture(chromium, site, tmp_path):
    state = tmp_path / "s.state.json"
    site.login_state(state)
    script = tmp_path / "search.jsonl"
    script.write_text("".join(json.dumps(s) + "\n" for s in scripts.SEARCH))
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "spider.mappers.harmap",
            "capture",
            site.url + "/",
            "--task",
            "search",
            "--run",
            "real",
            "--input",
            "query=software",
            "--state",
            str(state),
            "--out",
            str(tmp_path / "captures"),
            "--headless",
            "--script",
            str(script),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    bundle = tmp_path / "captures" / "127.0.0.1" / "search" / "real"
    meta = json.loads((bundle / "meta.json").read_text())
    assert meta["real_inputs"] == {"query": "software"} and meta["markers"] == {}
    search = [e for e in _har_entries(bundle) if "/api/search" in e["request"]["url"]]
    assert json.loads(search[0]["response"]["content"]["text"])["total"] == 23
    # nothing sensitive on the console
    sid = next(c["value"] for c in json.loads(state.read_text())["cookies"] if c["name"] == "sid")
    assert sid not in out.stdout + out.stderr


@pytest.mark.browser
def test_selectors_skip_framework_generated_ids(chromium):
    """React useId / Ember ids change between loads, so recorded selectors must not rely on them."""
    from playwright.sync_api import sync_playwright

    from spider.mappers.harmap.record.capture import _logger_js

    html = """
    <input id="_r_4_" placeholder="Search">
    <input id=":r7:" aria-label="City">
    <input id="ember1234" name="company">
    <div id="«r2»"><button>Go</button></div>
    <input id="q">
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.add_init_script(_logger_js())
            page.goto("about:blank")
            page.set_content(html)
            pick = lambda sel: page.eval_on_selector(sel, "el => window.__mapperSelectorFor(el)")  # noqa: E731
            assert pick("input[placeholder=Search]") == 'input[placeholder="Search"]'
            assert pick("input[aria-label=City]") == 'input[aria-label="City"]'
            assert pick("input[name=company]") == 'input[name="company"]'
            assert pick("button") == "div > button"
            assert pick("input:not([placeholder]):not([aria-label]):not([name])") == "#q"  # a real id is kept
        finally:
            browser.close()


def test_slim_har_keeps_data_and_html_bodies_only(tmp_path):
    def entry(rtype, text):
        return {"_resourceType": rtype, "response": {"content": {"mimeType": "x", "text": text}}}

    har = tmp_path / "traffic.har"
    kinds = ["script", "stylesheet", "image", "media", "font", "fetch", "xhr", "document"]
    har.write_text(json.dumps({"log": {"entries": [entry(k, f"body of {k}") for k in kinds]}}))
    from spider.mappers.harmap.record.capture import slim_har

    slim_har(har)
    kept = {
        e["_resourceType"]: e["response"]["content"].get("text")
        for e in json.loads(har.read_text())["log"]["entries"]
    }
    assert kept == {k: (f"body of {k}" if k in ("fetch", "xhr", "document") else None) for k in kinds}


def test_mark_comes_right_after_the_action_and_typing_pauses():
    """Seen on LinkedIn: waiting for quiet before the mark put it 20 s late, and an instant Enter after
    typing let suggestion lookups land inside the goal window."""
    calls = []
    page = SimpleNamespace(
        fill=lambda sel, value: calls.append(("fill", value)),
        press=lambda sel, key: calls.append(("press", key)),
        click=lambda sel: calls.append(("click", sel)),
        wait_for_timeout=lambda ms: calls.append(("pause", ms)),
    )
    tracker = SimpleNamespace(
        action=lambda: calls.append(("act",)), wait_for_action=lambda page, ms: calls.append(("wait", ms))
    )
    steps = [
        {"type": "input", "selector": "#q", "value": "{{params.query}}"},
        {"type": "key", "selector": "#q", "key": "Enter"},
        {"type": "mark"},
        {"type": "click", "selector": "#more"},
    ]
    driver.run_script(
        page,
        steps,
        start_url="https://example.com/",
        params={"query": "zqxv8841"},
        tracker=tracker,
        on_mark=lambda: calls.append(("MARK",)),
    )
    assert calls == [
        ("act",),
        ("fill", "zqxv8841"),
        ("wait", driver.TYPING_PAUSE_MS),  # until the lookups the typing started are answered
        ("act",),
        ("press", "Enter"),  # no wait between the triggering action and the mark
        ("MARK",),
        ("wait", driver.AFTER_MARK_MS),  # until the task's own requests are answered
        ("act",),
        ("click", "#more"),
        ("pause", driver.SETTLE_MS),  # ordinary waits: a flat pause, background traffic ignored
    ]


def test_never_ending_connections_do_not_count_as_loading():
    handlers = {}
    context = SimpleNamespace(on=lambda event, fn: handlers.setdefault(event, fn))
    tracker = driver.NetworkTracker(context)
    live, api = SimpleNamespace(resource_type="eventsource"), SimpleNamespace(resource_type="fetch")
    handlers["request"](live)
    handlers["request"](api)
    assert tracker.inflight == 1  # the live-update stream is ignored
    handlers["requestfinished"](api)
    assert tracker.inflight == 0  # so the page can go quiet while the stream stays open


def test_a_failing_step_is_named_in_the_error():
    def missing(sel):
        raise TimeoutError("Page.click: Timeout 8000ms exceeded.\nwaiting for locator('#filter')")

    page = SimpleNamespace(fill=lambda sel, value: None, click=missing, wait_for_timeout=lambda ms: None)
    tracker = SimpleNamespace(action=lambda: None, wait_for_action=lambda page, timeout_ms: None)
    steps = [{"type": "input", "selector": "#q", "value": "x"}, {"type": "click", "selector": "#filter"}]
    with pytest.raises(
        driver.StepFailed, match=r"step 2 \(click #filter\): TimeoutError: Page.click: Timeout"
    ):
        driver.run_script(
            page, steps, start_url="https://example.com/", params={}, tracker=tracker, on_mark=lambda: None
        )


def test_waits_for_the_actions_own_requests_not_background_traffic():
    """Seen on LinkedIn: the search was answered in 0.27 s, but tracking and bot-check traffic kept
    every 'wait until quiet' running to its 10 s cap."""
    import time

    handlers = {}
    context = SimpleNamespace(on=lambda event, fn: handlers.setdefault(event, fn))
    tracker = driver.NetworkTracker(context)
    background, search, later = (SimpleNamespace(resource_type="fetch") for _ in range(3))
    handlers["request"](background)  # already running before the action, never finishes
    tracker.action()
    t0 = time.monotonic()
    timeline = [(0.10, "request", search), (0.30, "requestfinished", search), (0.70, "request", later)]

    def pump(ms):
        time.sleep(ms / 1000)
        while timeline and time.monotonic() - t0 >= timeline[0][0]:
            _, event, req = timeline.pop(0)
            handlers[event](req)

    tracker.wait_for_action(SimpleNamespace(wait_for_timeout=pump), timeout_ms=5000)
    took = time.monotonic() - t0
    assert 0.5 <= took < 1.0  # the action window, then done: not the 5 s cap
