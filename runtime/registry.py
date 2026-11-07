"""SQLite storage for action maps: one row per version, one active version per domain."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from schema import Card, load_card

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS action_maps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT    NOT NULL,
    version     INTEGER NOT NULL,
    map_json    TEXT    NOT NULL,
    active      INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    created_at  TEXT    NOT NULL,
    note        TEXT,
    UNIQUE (domain, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS action_maps_one_active
    ON action_maps (domain) WHERE active = 1;
"""

ChangeListener = Callable[[str], None]


@dataclass(frozen=True)
class StoredCard:
    domain: str
    version: int
    card: Card
    active: bool
    created_at: str
    note: str | None


@dataclass(frozen=True)
class VersionInfo:
    version: int
    active: bool
    created_at: str
    note: str | None


class Registry:
    def __init__(self, path: str | Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA_SQL)
        self._active: dict[str, StoredCard] = {}
        self._listeners: list[ChangeListener] = []
        self._data_version = -1
        self.refresh()

    # ---- reads -------------------------------------------------------------

    def active_cards(self) -> dict[str, StoredCard]:
        """In-memory copy of every domain's active card."""
        with self._lock:
            return dict(self._active)

    def get(self, domain: str, version: int | None = None) -> StoredCard | None:
        """The active card for `domain`, or a specific version."""
        if version is None:
            with self._lock:
                return self._active.get(domain)
        row = self._conn.execute(
            "SELECT * FROM action_maps WHERE domain = ? AND version = ?", (domain, version)
        ).fetchone()
        return _row_to_stored(row) if row else None

    def history(self, domain: str) -> list[VersionInfo]:
        """Every version for `domain`, newest first."""
        rows = self._conn.execute(
            "SELECT version, active, created_at, note FROM action_maps WHERE domain = ? ORDER BY version DESC",
            (domain,),
        ).fetchall()
        return [VersionInfo(r["version"], bool(r["active"]), r["created_at"], r["note"]) for r in rows]

    # ---- writes ------------------------------------------------------------

    def save(self, card_data: dict[str, Any] | Card, note: str | None = None, activate: bool = True) -> int:
        """Validate, then insert as the domain's next version. Returns the new version number.

        Raises `schema.CardInvalid` (nothing is written) if the card fails validation.
        """
        raw = card_data.to_json_dict() if isinstance(card_data, Card) else card_data
        card = load_card(raw)
        # Key order is kept: it is meaningful (e.g. output.fields order = envelope field order).
        map_json = json.dumps(card.to_json_dict(), separators=(",", ":"))
        with self._lock:
            with self._tx() as cur:
                row = cur.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM action_maps WHERE domain = ?", (card.domain,)
                ).fetchone()
                version = int(row[0]) + 1
                if activate:
                    cur.execute("UPDATE action_maps SET active = 0 WHERE domain = ? AND active = 1", (card.domain,))
                cur.execute(
                    "INSERT INTO action_maps (domain, version, map_json, active, created_at, note) VALUES (?,?,?,?,?,?)",
                    (card.domain, version, map_json, int(activate), _now(), note),
                )
            self.refresh()
        return version

    def rollback(self, domain: str, version: int) -> None:
        """Make an existing version the active one (no new row is written)."""
        with self._lock:
            with self._tx() as cur:
                exists = cur.execute(
                    "SELECT 1 FROM action_maps WHERE domain = ? AND version = ?", (domain, version)
                ).fetchone()
                if not exists:
                    raise KeyError(f"{domain} has no version {version}")
                cur.execute("UPDATE action_maps SET active = 0 WHERE domain = ? AND active = 1", (domain,))
                cur.execute("UPDATE action_maps SET active = 1 WHERE domain = ? AND version = ?", (domain, version))
            self.refresh()

    # ---- change tracking ---------------------------------------------------

    def on_change(self, listener: ChangeListener) -> None:
        """Call `listener(domain)` whenever a domain's active card changes."""
        self._listeners.append(listener)

    def refresh(self) -> set[str]:
        """Reload active cards if the database changed (including writes by other processes).

        Returns the domains whose active card changed, after notifying listeners.
        """
        with self._lock:
            dv = self._conn.execute("PRAGMA data_version").fetchone()[0]
            total = self._conn.execute("SELECT total_changes()").fetchone()[0]
            stamp = (dv << 32) + total
            if stamp == self._data_version:
                return set()
            self._data_version = stamp
            rows = self._conn.execute("SELECT * FROM action_maps WHERE active = 1").fetchall()
            fresh = {r["domain"]: _row_to_stored(r) for r in rows}
            changed = {
                d
                for d in set(fresh) | set(self._active)
                if (fresh.get(d) and fresh[d].version) != (self._active.get(d) and self._active[d].version)
            }
            self._active = fresh
        for domain in sorted(changed):
            for listener in self._listeners:
                listener(domain)
        return changed

    def close(self) -> None:
        self._conn.close()

    def _tx(self) -> _Transaction:
        return _Transaction(self._conn)


class _Transaction:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> sqlite3.Cursor:
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn.cursor()

    def __exit__(self, exc_type: object, *_: object) -> None:
        self._conn.execute("ROLLBACK" if exc_type else "COMMIT")


def _row_to_stored(row: sqlite3.Row) -> StoredCard:
    return StoredCard(
        domain=row["domain"],
        version=row["version"],
        card=Card.model_validate(json.loads(row["map_json"])),
        active=bool(row["active"]),
        created_at=row["created_at"],
        note=row["note"],
    )


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
