"""Sanity checks that the fixture site behaves as the executor tests assume."""

from typing import Any

import httpx

from schema import validate_card


def test_fixture_card_is_valid(fixture_card: dict[str, Any]) -> None:
    report = validate_card(fixture_card)
    assert report.ok, report.errors
    assert report.warnings == []


def test_search_paging(site_url: str) -> None:
    r = httpx.get(f"{site_url}/api/products", params={"q": "lamp", "start": 5, "count": 5}).json()
    assert r["total"] == 10 and len(r["results"]) == 5


def test_break_switch_moves_items(site_url: str) -> None:
    assert httpx.get(f"{site_url}/api/items/p1").status_code == 200
    httpx.post(f"{site_url}/_admin/break")
    assert httpx.get(f"{site_url}/api/items/p1").status_code == 404
    assert httpx.get(f"{site_url}/api/v2/items/p1").status_code == 200
    httpx.post(f"{site_url}/_admin/reset")
    assert httpx.get(f"{site_url}/api/items/p1").status_code == 200


def test_cookie_flow(site_url: str) -> None:
    with httpx.Client(base_url=site_url) as c:
        assert c.get("/api/flow/finish", params={"token": "flow-token-1"}).status_code == 400
        c.get("/api/flow/start")
        assert c.get("/api/flow/finish", params={"token": "flow-token-1"}).status_code == 200


def test_hits_counted(site_url: str) -> None:
    httpx.get(f"{site_url}/api/limited")
    assert httpx.get(f"{site_url}/_admin/hits").json() == {"GET /api/limited": 1}
