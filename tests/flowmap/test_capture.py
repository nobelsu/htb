"""flowmap.capture: secrets hashing (pure) and real captures of the bookshop (needs Chromium)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from flowmap.capture import (
    SECRET,
    Exchange,
    Run,
    capture_flow,
    cookie_header_values,
    load_runs,
    new_marker,
    run_plan,
    save_run,
    scrub_run,
    secret_hash,
    secret_values,
)
from tests.flowmap.conftest import book_flow, cart_flow, search_flow
from tests.sites.bookshop import BOOKS

# ---------- pure ----------


def test_marker_shape() -> None:
    m = new_marker()
    assert len(m) == 8 and m[:4].isalpha() and m[4:].isdigit()
    assert new_marker() != m


def test_secret_values_strip_quotes_and_auth_scheme() -> None:
    found = secret_values(
        ['"ajax:1234567890"', "short"],
        [("csrf-token", "ajax:1234567890"), ("Authorization", "Bearer abcdefghijk"), ("accept", "x" * 20)],
    )
    assert found == {"ajax:1234567890", "Bearer abcdefghijk", "abcdefghijk"}


def test_cookie_header_values() -> None:
    headers = [("cookie", 'a=1; JSESSIONID="ajax:9"'), ("set-cookie", "sid=xyz; Path=/\nother=q; HttpOnly")]
    assert cookie_header_values(headers) == ["1", '"ajax:9"', "xyz", "q"]


def test_scrub_run_hashes_everywhere_and_keeps_links() -> None:
    live = "ajax:5550001112223"
    run = Run(
        run_id="r",
        session="s",
        domain="d",
        action="a",
        kind="real",
        index=0,
        values={},
        started_at="t",
        exchanges=[
            Exchange(
                id=0,
                step=0,
                resource_type="fetch",
                method="GET",
                url=f"https://d/x?t={live}",
                request_headers=[("cookie", f'JSESSIONID="{live}"'), ("csrf-token", live)],
                body=json.dumps({"token": live}),
            )
        ],
    )
    out = scrub_run(run, {live})
    raw = out.model_dump_json()
    assert live not in raw
    h = secret_hash(live)
    assert out.exchanges[0].request_headers == [("cookie", f'JSESSIONID="{h}"'), ("csrf-token", h)]
    assert out.exchanges[0].url.endswith(h) and json.loads(out.exchanges[0].body or "")["token"] == h
    assert out.secrets_hashed == 1 and SECRET.fullmatch(h)


def test_run_plan() -> None:
    flow = search_flow("http://x")
    plan = run_plan(flow)
    assert [k for k, _, _ in plan] == ["marker", "marker", "real", "real"]
    m0, m1 = plan[0][2]["query"], plan[1][2]["query"]
    assert m0 != m1 and plan[0][1]["query"] == m0
    assert plan[0][1]["sort"] == "relevance" and plan[1][1]["sort"] == "price"  # no marker: real values cycle
    assert plan[2][1] == {"query": "the", "sort": "relevance"} and plan[2][2] == {}


def test_save_and_load_latest_session(tmp_path: Path) -> None:
    def run(session: str, kind: str, i: int) -> Run:
        return Run(
            run_id=f"{session}-{kind}{i}",
            session=session,
            domain="d.test",
            action="a",
            kind=kind,  # type: ignore[arg-type]
            index=i,
            values={},
            started_at="t",
        )

    for r in (run("20260101", "real", 0), run("20260102", "real", 0), run("20260102", "marker", 0)):
        path = save_run(r, tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    latest = load_runs(tmp_path, "d.test", "a")
    assert [(r.session, r.kind) for r in latest] == [("20260102", "marker"), ("20260102", "real")]
    assert [r.session for r in load_runs(tmp_path, "d.test", "a", "20260101")] == ["20260101"]
    assert load_runs(tmp_path, "d.test", "missing") == []


# ---------- against the bookshop ----------


def _chromium() -> None:
    pytest.importorskip("playwright.async_api")


@pytest.mark.browser
async def test_capture_search(bookshop: str, tmp_path: Path) -> None:
    _chromium()
    runs = await capture_flow(search_flow(bookshop), root=tmp_path, pause_s=0)
    assert [(r.kind, r.index) for r in runs] == [("marker", 0), ("marker", 1), ("real", 0), ("real", 1)]
    assert all(r.error is None for r in runs), [r.error for r in runs]

    marker = runs[0]
    m = marker.markers["query"]
    urls = [e.url for e in marker.exchanges]
    assert any("/api/search?" in u and m in u for u in urls)  # the action request carries the marker
    assert any("/api/suggest?q=" + m[:3] in u for u in urls)  # the typeahead carries only part of it
    assert any(u.startswith("http://localhost:") and m in u for u in urls)  # the beacon (another host)
    assert marker.visible == []  # a marker finds nothing

    real = runs[2]
    expected = [b["title"] for b in BOOKS if "the" in b["title"].lower()]
    assert real.visible == expected[:10] and len(expected) > 10
    assert real.visible_page2 == expected[10:20]
    assert runs[3].visible_page2 is None  # "harbour" fits on one page: no next link, no page 2
    search = [e for e in real.exchanges if "/api/search?" in e.url]
    assert [e.phase for e in search] == ["flow", "next_page"]
    assert search[0].step == 3  # the Enter press
    assert search[1].step == 5 and "start=10" in search[1].url  # len(steps): the next-page click
    body = json.loads(search[0].body or "")
    assert body["data"]["paging"]["total"] > 10

    # live session values are hashed; the csrf header still matches the (quoted) cookie
    raw = "".join(p.read_text() for p in (tmp_path / "bookshop.test" / "search_books").glob("*.json"))
    assert "ajax:" not in raw
    headers = dict(search[0].request_headers)
    cookie = next(c for c in real.cookies if c.name == "JSESSIONID")
    assert SECRET.fullmatch(headers["csrf-token"]) and cookie.value == f'"{headers["csrf-token"]}"'
    assert real.secrets_hashed >= 1
    assert len(load_runs(tmp_path, "bookshop.test", "search_books")) == 4


@pytest.mark.browser
async def test_capture_two_step_and_write(bookshop: str) -> None:
    _chromium()
    book = await capture_flow(book_flow(bookshop), marker_runs=1, real_runs=1, pause_s=0)
    real = book[-1]
    assert real.visible == ["The Quiet Harbour"]
    token = next(e for e in real.exchanges if e.url.endswith("/api/token"))
    detail = next(e for e in real.exchanges if "/api/books/b001?token=" in e.url)
    assert json.loads(token.body or "")["token"] in detail.url
    assert token.started_ms <= detail.started_ms

    cart = await capture_flow(cart_flow(bookshop), marker_runs=0, real_runs=1, pause_s=0)
    post = next(e for e in cart[-1].exchanges if e.method == "POST")
    assert post.step == 2 and json.loads(post.post_data or "") == {"book_id": "b002", "quantity": 1}
    assert post.status == 200


@pytest.mark.browser
async def test_capture_keeps_partial_run_on_failure(bookshop: str) -> None:
    _chromium()
    flow = search_flow(bookshop).model_copy(update={"results_selector": None, "next_page": None})
    flow.steps.append(flow.steps[0].model_copy(update={"url": bookshop + "/"}))
    flow.steps.append(type(flow.steps[4])(target="#never-there", timeout_ms=200))
    runs = await capture_flow(flow, marker_runs=0, real_runs=1, pause_s=0, timeout_ms=500)
    assert runs[0].error and "step 7 (wait_for) failed" in runs[0].error
    assert any("/api/search?" in e.url for e in runs[0].exchanges)


def test_a_cookie_holding_a_host_name_is_not_a_secret() -> None:
    """LinkedIn sets a cookie whose value is its own host name: hashing it hashed www.linkedin.com out of
    every captured URL, so nothing looked same-site and every LinkedIn mapping failed."""
    from flowmap.capture import secret_values

    found = secret_values(["www.linkedin.com", ".linkedin.com", "AQEDAS-a-real-session-token"], [])
    assert found == {"AQEDAS-a-real-session-token"}


@pytest.mark.browser
async def test_blocking_the_search_request_empties_the_page(bookshop: str) -> None:
    """The blocking test's replay: without /api/search the bookshop shows no results; without the typeahead
    it still does."""
    pytest.importorskip("playwright.async_api")
    from flowmap.capture import visible_without
    from tests.flowmap.conftest import search_flow

    flow = search_flow(bookshop)
    assert await visible_without(flow, lambda m, u: "/api/search" in u, timeout_ms=3_000) == []
    assert await visible_without(flow, lambda m, u: "/api/suggest" in u, timeout_ms=3_000)
