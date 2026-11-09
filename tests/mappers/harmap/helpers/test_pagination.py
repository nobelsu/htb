"""Step 7: page2 run → offset:N, page_size, next_params with until from the total."""

import pytest
from synthetic import action, har_entry, make_bundle

from spider.mappers.harmap.card.validate import RuntimeExecutor
from spider.mappers.harmap.infer.goal import find_read_goal
from spider.mappers.harmap.pipeline import map_domain, pagination

SITE = "https://www.example.com"


def _page_bundle(url1, url2, body1, body2):
    return make_bundle(
        "page2",
        [har_entry("GET", url1, at=2, body=body1), har_entry("GET", url2, at=5, body=body2)],
        values={"query": "acme"},
        marks=[3.0, 6.0],
        actions=[action("click", 1.5, selector="b"), action("click", 4.5, selector="#next")],
    )


def _infer(url1, url2, body1=None, body2=None):
    body1 = body1 or {"total": 57, "count": 25, "elements": [{"id": i} for i in range(25)]}
    body2 = body2 or {"total": 57, "count": 25, "elements": [{"id": i} for i in range(25, 50)]}
    b = _page_bundle(url1, url2, body1, body2)
    return pagination.infer(b, b.entries[0], b, b.entries[1], "$.elements")


def test_offset_from_zero():
    pag, warnings = _infer(f"{SITE}/api/search?q=acme&start=0", f"{SITE}/api/search?q=acme&start=25")
    assert (pag.address, pag.mode, pag.step, pag.page_size) == ("query.start", "offset", 25, 25)
    assert pag.template == "{{params.page | offset:25}}"
    assert pag.until_path == "$.total"  # preferred over "count"
    assert not warnings


def test_page_number():
    pag, _ = _infer(f"{SITE}/api/search?q=acme&page=1", f"{SITE}/api/search?q=acme&page=2")
    assert (pag.mode, pag.page_size, pag.template) == ("page", 25, "{{params.page}}")


def test_restli_offset_inside_variables():
    v = "(keywords:acme,start:{})"
    pag, _ = _infer(f"{SITE}/graphql?variables={v.format(0)}", f"{SITE}/graphql?variables={v.format(10)}")
    assert pag.address == "query.variables.$.start" and pag.step == 10


def test_total_must_be_stable_and_cover_the_page():
    body1 = {"meta": {"totalCount": 3}, "elements": [{"id": i} for i in range(25)]}
    body2 = {"meta": {"totalCount": 3}, "elements": [{"id": 9}]}
    pag, _ = _infer(f"{SITE}/a?start=0", f"{SITE}/a?start=25", body1, body2)
    assert pag.until_path is None  # 3 < 25 items on the page: not a total
    assert pagination.find_total({"paging": {"total": 40}}, {"paging": {"total": 40}}, 10) == "$.paging.total"
    assert pagination.find_total({"total": 40}, {"total": 41}, 10) is None


def test_nothing_changed_or_not_an_offset():
    pag, warnings = _infer(f"{SITE}/a?start=0", f"{SITE}/a?start=0")
    assert pag is None and "no numeric request field changed" in warnings[0]
    pag, warnings = _infer(f"{SITE}/a?start=5", f"{SITE}/a?start=9")
    assert pag is None and "not an offset" in warnings[-1]


def test_page2_goal_is_the_second_page():
    b = _page_bundle(
        f"{SITE}/api/search?q=acme&start=0", f"{SITE}/api/search?q=acme&start=25", {"e": [1]}, {"e": [2]}
    )
    assert find_read_goal(b, mark_index=-1).entry is b.entries[1]


# --- fixture site, real captures --------------------------------------------------------------


@pytest.fixture(scope="module")
def paged(fixture_captures):
    for run in ("m1", "m2", "real", "page2"):
        fixture_captures("search", run)
    return map_domain(fixture_captures.root / "127.0.0.1", task="search", live=True)


@pytest.mark.browser
def test_fixture_card_paginates(paged):
    assert paged.validation.ok, paged.validation.errors
    [action] = paged.card["actions"]
    search = action["steps"][-1]
    assert search["request"]["query"] == {"q": "{{params.query}}", "start": "{{params.page | offset:10}}"}
    assert search["extract"]["total"] == {"from": "body", "type": "jsonpath", "expr": "$.total"}
    assert "$.total" in search["expect"]["paths"]
    assert action["params"]["page"] == {
        "type": "integer",
        "required": False,
        "default": 1,
        "description": "1-based page number",
    }
    assert action["output"]["limit"] == 10
    assert action["output"]["next_params"] == {
        "param": "page",
        "increment": 1,
        "page_size": 10,
        "until": "steps.search.total",
    }


@pytest.mark.browser
def test_fixture_pages_through_the_executor(paged):
    """23 results at 10 per page: pages 1 and 2 point to the next page, page 3 (3 items) ends it."""
    ex = RuntimeExecutor()
    cookies = paged.tasks[0].runs["m1"].state_cookies
    seen, page, pages = [], 1, []
    while page is not None and len(pages) < 5:
        env = ex.run_action(
            paged.card, "search", {"query": "software", "page": page}, session_cookies=cookies
        )
        assert env["status"] == "SUCCESS", env
        pages.append(len(env["data"]))
        seen += [item["id"] for item in env["data"]]
        page = (env["next_params"] or {}).get("page")
    assert pages == [10, 10, 3]
    assert len(set(seen)) == 23


@pytest.mark.browser
def test_report_explains_pagination(paged, tmp_path):
    from spider.mappers.harmap.pipeline import write_outputs

    _, report_path = write_outputs(paged, tmp_path)
    line = next(ln for ln in report_path.read_text().splitlines() if ln.startswith("| `query.start`"))
    assert "param" in line and "went 0 → 10" in line and "offset:10" in line
