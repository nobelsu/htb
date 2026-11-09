import pytest
from synthetic import action, har_entry, make_bundle

from spider.harmap.pipeline.goal import GoalNotFound, endpoint_key, find_goal, find_read_goal, find_write_goal

SITE = "https://www.example.com"
A = "zqxv8841"


def _search(at, q=A, path="/api/search", body=None):
    return har_entry("GET", f"{SITE}{path}?q={q}", at=at, body=body or {"elements": [], "total": 0})


def test_read_goal_is_request_after_the_triggering_click():
    b = make_bundle(
        "m1",
        [
            _search(1.5, path="/api/typeahead", body={"hits": []}),  # fires while typing
            _search(3.2),
        ],
        values={"query": A},
        marks=[4.0],
        actions=[action("input", 1.0, selector="#q", value=A), action("click", 3.0, selector="button")],
    )
    goal = find_read_goal(b)
    assert goal.entry.path == "/api/search"
    assert goal.tie is False


def test_read_goal_needs_json_list_over_html_echo():
    b = make_bundle(
        "m1",
        [
            har_entry("GET", f"{SITE}/search?q={A}", at=1, mime="text/html", body=f"<p>{A}</p>"),
            _search(1.1),
        ],
        values={"query": A},
    )
    assert find_read_goal(b).entry.path == "/api/search"


def test_read_goal_html_fallback_when_no_json():
    b = make_bundle(
        "m1",
        [har_entry("GET", f"{SITE}/search?q={A}", at=1, mime="text/html", body="<ul></ul>")],
        values={"query": A},
    )
    goal = find_read_goal(b)
    assert goal.entry.path == "/search" and "HTML" in goal.reason


def test_read_goal_ignores_requests_before_marker_typed():
    b = make_bundle(
        "m1",
        [_search(0.5, path="/api/recent"), _search(2.0)],
        values={"query": A},
        actions=[action("input", 1.0, selector="#q", value=A)],
    )
    assert find_read_goal(b).entry.path == "/api/search"


def test_read_goal_tie_is_flagged_and_earliest_wins():
    b = make_bundle(
        "m1",
        [_search(3.1, path="/api/search/companies"), _search(3.2, path="/api/search/people")],
        values={"query": A},
        marks=[4.0],
        actions=[action("click", 3.0, selector="button")],
    )
    goal = find_read_goal(b)
    assert goal.tie is True and goal.entry.path == "/api/search/companies"
    assert len(goal.candidates) == 2


def test_page2_goal_is_latest_before_second_mark():
    b = make_bundle(
        "page2",
        [
            har_entry("GET", f"{SITE}/api/search?q=software&start=0", at=2, body={"elements": [1]}),
            har_entry("GET", f"{SITE}/api/search?q=software&start=10", at=5, body={"elements": [2]}),
        ],
        values={"query": "software"},
        marks=[3.0, 6.0],
        actions=[action("click", 1.5, selector="b"), action("click", 4.5, selector="#next")],
    )
    assert find_read_goal(b, mark_index=1).entry.raw_query.endswith("start=10")
    assert find_read_goal(b, mark_index=0).entry.raw_query.endswith("start=0")


def test_no_goal_raises():
    b = make_bundle("m1", [har_entry("GET", f"{SITE}/api/x", body={"a": []})], values={"query": A})
    with pytest.raises(GoalNotFound):
        find_read_goal(b)


def _post(path, at):
    return har_entry(
        "POST", f"{SITE}{path}", at=at, post="a=1", post_mime="application/x-www-form-urlencoded"
    )


def test_write_goal_is_last_write_between_click_and_mark():
    b = make_bundle(
        "m1",
        [_post("/api/draft-autosave", 1.0), _post("/api/notes", 3.1), _post("/api/events", 5.0)],
        kind="write",
        marks=[4.0],
        actions=[action("click", 3.0, selector="button")],
    )
    goal = find_write_goal(b)
    assert goal.entry.path == "/api/notes"
    assert "between the triggering click and the goal mark" in goal.reason


def test_write_goal_falls_back_to_first_after_mark():
    b = make_bundle(
        "m1",
        [_post("/api/notes", 5.0), _post("/api/events", 6.0)],
        kind="write",
        marks=[4.0],
        actions=[action("click", 3.0, selector="button")],
    )
    goal = find_goal(b)
    assert goal.entry.path == "/api/notes" and "after the goal mark" in goal.reason


def test_endpoint_key_masks_marker_in_path():
    b = make_bundle("m1", [har_entry("GET", f"{SITE}/company/{A}/about")], values={"query": A})
    assert endpoint_key(b.entries[0], {"query": A}) == ("GET", "www.example.com", "/company/{}/about")
