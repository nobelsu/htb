"""The checks on what Claude proposes when exploring (flowmap/serve/explore.py)."""

from __future__ import annotations

from typing import Any

from spider.flowmap.serve.explore import MAX_PER_ROUND, clean_tasks, samples

START = "https://www.shop.example/"


def task(action: str, text: str = "", url: str = START, **inputs: str) -> dict[str, Any]:
    return {
        "action": action,
        "task": text or f"Do {action}",
        "start_url": url,
        "inputs": [{"name": k, "value": v} for k, v in inputs.items()],
    }


def test_writes_are_dropped_but_reads_about_them_stay() -> None:
    raw = [
        task("follow_company", "Follow the company"),
        task("add_to_cart", "Add a book to the cart"),
        task("search_posts", "Search posts for robots", query="robots"),
        task("get_order", "Look up an order", order_id="o1"),
        task("publish", "Write a post saying hello"),
    ]
    assert [t.action for t in clean_tasks(raw, start_url=START, taken=set())] == ["search_posts", "get_order"]
    writes = [
        {**task("add_to_cart", "Add the book to the cart"), "kind": "write"},
        {**task("follow_company", "Follow the company"), "kind": "write"},
        {**task("delete_post", "Delete my last post"), "kind": "write"},
        {**task("checkout", "Check out the cart"), "kind": "write"},
        {**task("send_message", "Send a message"), "kind": "write"},
    ]
    out = clean_tasks(writes, start_url=START, taken=set())
    assert [(t.action, t.write) for t in out] == [("add_to_cart", True), ("follow_company", True)]  # max 2


def test_names_pages_and_inputs_are_checked() -> None:
    raw = [
        task("Search Books"),  # not snake_case
        task("search_books", url="https://evil.example/x", query="q"),  # off the site: starts at START
        task("search_books", query="again"),  # a duplicate
        task("list_taken"),  # mapped already
        task("too_many", in_a="1", in_b="2", in_c="3", in_d="4", in_e="5"),
        task("on_a_subdomain", url="https://m.shop.example/top"),
    ]
    out = clean_tasks(raw, start_url=START, taken={"list_taken"})
    assert [(t.action, t.url) for t in out] == [
        ("search_books", START),
        ("on_a_subdomain", "https://m.shop.example/top"),
    ]
    assert out[0].inputs == {"query": "q"}
    many = [task(f"read_{i}") for i in range(10)]
    assert len(clean_tasks(many, start_url=START, taken=set())) == MAX_PER_ROUND


def test_samples_are_short() -> None:
    action = {"name": "search_books", "description": "Search", "params": {"query": {}, "page": {}}}
    out = samples(action, [{"title": "x" * 1000}] * 5)
    assert (
        out["inputs"] == ["page", "query"]
        and len(out["returned"]) < 1_600
        and out["returned"].endswith("(cut)")
    )


def test_an_action_that_sends_the_same_requests_is_a_duplicate() -> None:
    from spider.flowmap.serve.server import _same_requests

    def action(name: str, url: str, *query: str) -> dict[str, Any]:
        step = {"request": {"method": "GET", "url": url, "query": dict.fromkeys(query, "x")}}
        return {"name": name, "steps": [step]}

    card = {
        "actions": [
            action("search_books", "{{base_url}}/api/search", "q", "sort"),
            action("get_book", "{{base_url}}/api/books/{{params.id}}"),
            action("list_by_author", "{{base_url}}/api/search", "sort", "q"),
        ]
    }
    assert _same_requests(card, "list_by_author") == "search_books"
    assert _same_requests(card, "get_book") is None


def test_pages_of_one_kind_are_explored_once() -> None:
    from spider.flowmap.serve.explore import page_kind

    assert page_kind("https://www.linkedin.com/in/alice/") == page_kind("https://www.linkedin.com/in/bob-1/")
    assert page_kind("https://www.linkedin.com/in/alice/recent-activity/all/") != page_kind(
        "https://www.linkedin.com/in/alice/"
    )
    assert page_kind("https://www.linkedin.com/company/x/") != page_kind("https://www.linkedin.com/in/x/")


def test_results_lead_to_one_page_of_each_kind_on_the_site() -> None:
    from spider.flowmap.serve.explore import links

    data = [
        {"name": "Alice", "profile_url": "https://www.linkedin.com/in/alice/"},
        {"name": "Bob", "profile_url": "/in/bob/"},  # same kind: not again
        {"name": "Co", "href": "/company/co/#about"},
        {"name": "Elsewhere", "href": "https://evil.example/in/x/"},
        {"name": "No link", "href": None},
    ]
    assert links(data, "https://www.linkedin.com", "linkedin.com") == [
        "https://www.linkedin.com/in/alice/",
        "https://www.linkedin.com/company/co/",
    ]
