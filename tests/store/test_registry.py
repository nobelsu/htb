import copy
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from spider.card import CardInvalid
from spider.store.registry import Registry

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE: dict[str, Any] = json.loads((ROOT / "action_map.example.json").read_text())


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "reg.db"


@pytest.fixture
def reg(db_path: Path) -> Iterator[Registry]:
    r = Registry(db_path)
    yield r
    r.close()


def variant(desc: str) -> dict[str, Any]:
    card = copy.deepcopy(EXAMPLE)
    card["actions"][0]["description"] = desc
    return card


def test_wal_mode(reg: Registry, db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_save_and_get(reg: Registry) -> None:
    assert reg.save(EXAMPLE, note="first") == 1
    stored = reg.get("linkedin.com")
    assert stored is not None
    assert stored.version == 1 and stored.active and stored.note == "first"
    assert stored.card.action("search_companies") is not None
    assert set(reg.active_cards()) == {"linkedin.com"}


def test_versions_increment_and_only_one_active(reg: Registry, db_path: Path) -> None:
    reg.save(variant("v1"))
    reg.save(variant("v2"))
    reg.save(variant("v3"))
    hist = reg.history("linkedin.com")
    assert [h.version for h in hist] == [3, 2, 1]
    assert [h.active for h in hist] == [True, False, False]
    active = reg.get("linkedin.com")
    assert active is not None and active.card.actions[0].description == "v3"
    old = reg.get("linkedin.com", version=1)
    assert old is not None and not old.active and old.card.actions[0].description == "v1"


def test_partial_unique_index_enforced(reg: Registry, db_path: Path) -> None:
    reg.save(EXAMPLE)
    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO action_maps (domain, version, map_json, active, created_at) "
            "VALUES ('linkedin.com', 99, '{}', 1, 'now')"
        )
    # Inactive rows for the same domain are fine.
    conn.execute(
        "INSERT INTO action_maps (domain, version, map_json, active, created_at) VALUES ('linkedin.com', "
        "98, '{}', 0, 'now')"
    )


def test_save_rejects_invalid_card(reg: Registry) -> None:
    bad = copy.deepcopy(EXAMPLE)
    bad["actions"][0]["output"]["limit"] = 3
    with pytest.raises(CardInvalid, match="must equal next_params.page_size"):
        reg.save(bad)
    assert reg.history("linkedin.com") == []
    assert reg.get("linkedin.com") is None


def test_save_is_transactional(reg: Registry, db_path: Path) -> None:
    reg.save(variant("v1"))
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TRIGGER fail_insert BEFORE INSERT ON action_maps BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="boom"):
        reg.save(variant("v2"))
    # The UPDATE that deactivated v1 must have been rolled back with the failed INSERT.
    hist = reg.history("linkedin.com")
    assert [(h.version, h.active) for h in hist] == [(1, True)]


def test_save_inactive(reg: Registry) -> None:
    reg.save(variant("v1"))
    assert reg.save(variant("candidate"), activate=False) == 2
    active = reg.get("linkedin.com")
    assert active is not None and active.version == 1


def test_rollback(reg: Registry) -> None:
    reg.save(variant("v1"))
    reg.save(variant("v2"))
    reg.rollback("linkedin.com", 1)
    active = reg.get("linkedin.com")
    assert active is not None and active.version == 1 and active.card.actions[0].description == "v1"
    assert [h.active for h in reg.history("linkedin.com")] == [False, True]
    with pytest.raises(KeyError):
        reg.rollback("linkedin.com", 7)


def test_refresh_sees_writes_from_another_connection(reg: Registry, db_path: Path) -> None:
    other = Registry(db_path)  # e.g. the mapper process
    other.save(variant("from harmap"))
    assert reg.get("linkedin.com") is None  # in-memory copy is stale until refresh
    assert reg.refresh() == {"linkedin.com"}
    stored = reg.get("linkedin.com")
    assert stored is not None and stored.card.actions[0].description == "from harmap"
    assert reg.refresh() == set()
    other.close()


def test_round_trip_keeps_explicitly_set_fields(reg: Registry) -> None:
    card = copy.deepcopy(EXAMPLE)
    card["actions"][2]["auth_provider"] = "linkedin"
    reg.save(card)
    stored = reg.get("linkedin.com")
    assert stored is not None
    follow = stored.card.action("follow_company")
    assert follow is not None and "auth_provider" in follow.model_fields_set


def test_key_order_preserved(reg: Registry) -> None:
    reg.save(EXAMPLE)
    stored = reg.get("linkedin.com")
    assert stored is not None
    assert list(stored.card.actions[0].output.fields) == ["name", "subtitle", "url"]
