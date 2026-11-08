"""Encrypted storage for connected sessions (the cookie copy the http/impersonate tiers use).

The browser tier uses the browser profile itself; this is the copy taken from it after login.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken

from runtime.identity import BrowserIdentity
from runtime.sessions import Cookies, SessionStore

SessionStatus = Literal["connected", "expired"]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    user          TEXT NOT NULL,
    domain        TEXT NOT NULL,
    provider      TEXT NOT NULL,
    cookies_enc   BLOB NOT NULL,      -- Fernet(SESSION_KEY) of the cookie JSON
    status        TEXT NOT NULL,      -- connected | expired
    connected_at  TEXT NOT NULL,
    checked_at    TEXT,
    identity      TEXT,               -- JSON BrowserIdentity of the browser the user signed in with (not secret)
    PRIMARY KEY (user, domain, provider)
);
"""


@dataclass(frozen=True)
class SessionRecord:
    user: str
    domain: str
    provider: str
    status: SessionStatus
    connected_at: str
    checked_at: str | None
    cookie_names: list[str]  # never the values


def new_session_key() -> str:
    return Fernet.generate_key().decode()


class EncryptedSessionStore:
    def __init__(self, path: str | Path, key: str) -> None:
        if not key:
            raise ValueError(
                "SESSION_KEY must be set (generate one with: uv run python -m runtime.session_store)"
            )
        self._fernet = Fernet(key.encode())
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA_SQL)
        columns = {r[1] for r in self._conn.execute("PRAGMA table_info(sessions)")}
        if "identity" not in columns:  # databases made before device identity
            self._conn.execute("ALTER TABLE sessions ADD COLUMN identity TEXT")

    def close(self) -> None:
        self._conn.close()

    def put(
        self,
        user: str,
        domain: str,
        provider_id: str,
        cookies: Cookies,
        identity: BrowserIdentity | None = None,
    ) -> None:
        """A new sign-in: its cookies and the identity of the browser it was made with."""
        blob = self._fernet.encrypt(json.dumps(cookies).encode())
        ident = json.dumps(identity.to_json()) if identity is not None else None
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (user, domain, provider, cookies_enc, status, connected_at, "
                "checked_at, identity) VALUES (?, ?, ?, ?, 'connected', ?, ?, ?) ON CONFLICT (user, domain, "
                "provider) DO UPDATE SET cookies_enc = excluded.cookies_enc, status = 'connected', "
                "connected_at = excluded.connected_at, checked_at = excluded.checked_at, identity = "
                "excluded.identity",
                (user, domain, provider_id, blob, now, now, ident),
            )

    def identity(self, user: str, domain: str, provider_id: str) -> BrowserIdentity | None:
        """The browser this sign-in was made with (None for sign-ins made before device identity)."""
        row = self._conn.execute(
            "SELECT identity FROM sessions WHERE user = ? AND domain = ? AND provider = ?",
            (user, domain, provider_id),
        ).fetchone()
        if row is None or not row[0]:
            return None
        try:
            return BrowserIdentity.from_json(json.loads(row[0]))
        except (ValueError, TypeError, KeyError):
            return None

    def get(self, user: str, domain: str, provider_id: str) -> Cookies | None:
        row = self._conn.execute(
            "SELECT cookies_enc FROM sessions WHERE user = ? AND domain = ? AND provider = ?",
            (user, domain, provider_id),
        ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(self._fernet.decrypt(row[0]))
        except InvalidToken:
            return None  # written with a different SESSION_KEY: treat as not connected
        return {str(k): str(v) for k, v in data.items()}

    def refresh(self, user: str, domain: str, provider_id: str, before: Cookies, after: Cookies) -> bool:
        """Cookie rotation: replace the cookies only if they are still `before` (the user didn't sign in again
        and no other run saved first meanwhile). Keeps the status and when the user connected."""
        with self._lock:
            if self.get(user, domain, provider_id) != before:
                return False
            blob = self._fernet.encrypt(json.dumps(after).encode())
            self._conn.execute(
                "UPDATE sessions SET cookies_enc = ?, checked_at = ? WHERE user = ? AND domain = ? AND "
                "provider = ?",
                (blob, _now(), user, domain, provider_id),
            )
            return True

    def mark(self, user: str, domain: str, provider_id: str, status: SessionStatus) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status = ?, checked_at = ? WHERE user = ? AND domain = ? AND provider = "
                "?",
                (status, _now(), user, domain, provider_id),
            )

    def record(self, user: str, domain: str, provider_id: str) -> SessionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE user = ? AND domain = ? AND provider = ?",
            (user, domain, provider_id),
        ).fetchone()
        return self._to_record(row) if row else None

    def records(self, status: SessionStatus | None = None) -> list[SessionRecord]:
        if status is None:
            rows = self._conn.execute("SELECT * FROM sessions ORDER BY domain, user").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE status = ? ORDER BY domain, user", (status,)
            ).fetchall()
        return [self._to_record(r) for r in rows]

    def delete(self, user: str, domain: str, provider_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM sessions WHERE user = ? AND domain = ? AND provider = ?",
                (user, domain, provider_id),
            )

    def _to_record(self, row: sqlite3.Row) -> SessionRecord:
        cookies = self.get(row["user"], row["domain"], row["provider"]) or {}
        return SessionRecord(
            user=row["user"],
            domain=row["domain"],
            provider=row["provider"],
            status=row["status"],
            connected_at=row["connected_at"],
            checked_at=row["checked_at"],
            cookie_names=sorted(cookies),
        )


class ChainedSessionStore:
    """First store that has a session wins (e.g. the encrypted store, then dev cookies from .env)."""

    def __init__(self, *stores: SessionStore) -> None:
        self.stores = stores

    def get(self, user: str, domain: str, provider_id: str) -> Cookies | None:
        for store in self.stores:
            found = store.get(user, domain, provider_id)
            if found is not None:
                return found
        return None

    def identity(self, user: str, domain: str, provider_id: str) -> BrowserIdentity | None:
        """The identity saved with the session the executor would use."""
        for store in self.stores:
            if store.get(user, domain, provider_id) is not None:
                ident = getattr(store, "identity", None)
                return ident(user, domain, provider_id) if ident is not None else None
        return None

    def refresh(self, user: str, domain: str, provider_id: str, before: Cookies, after: Cookies) -> bool:
        """Rotate in the store the session came from."""
        for store in self.stores:
            if store.get(user, domain, provider_id) is not None:
                refresh = getattr(store, "refresh", None)
                return (
                    bool(refresh(user, domain, provider_id, before, after)) if refresh is not None else False
                )
        return False


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    print(new_session_key())
