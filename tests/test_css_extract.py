"""CSS extracts (CARD_SPEC "Extraction", ``css``): results read from a page rendered on the server."""

from __future__ import annotations

import copy
from typing import Any

from runtime.extract import css_items
from schema import validate_card

PAGE = """<div class="hit"><h3><a href="/book/b1">The  Quiet
  Harbour</a></h3></div><div class="hit"><h3>No link</h3></div>"""


def test_each_match_is_its_text_and_link() -> None:
    assert css_items(PAGE, ".hit h3") == [
        {"text": "The Quiet Harbour", "href": "/book/b1"},
        {"text": "No link", "href": None},
    ]
    assert css_items(PAGE, ".hit a") == [{"text": "The Quiet Harbour", "href": "/book/b1"}]
    assert css_items(PAGE, ".nothing") == []


def test_css_extracts_are_checked(fixture_card: dict[str, Any]) -> None:
    card = copy.deepcopy(fixture_card)
    step = card["actions"][0]["steps"][0]
    step["extract"]["items"] = {"from": "body", "type": "css", "expr": ".hit h3"}
    assert validate_card(card).ok
    step["extract"]["items"] = {"from": "body", "type": "css", "expr": "div["}
    assert any("bad CSS selector" in e for e in validate_card(card).errors)
    step["extract"]["items"] = {"from": "headers", "type": "css", "expr": "h3"}
    assert any("css extract reads the body" in e for e in validate_card(card).errors)
