"""Runtime learnings and healing records: `action_state`, `heal_jobs`, `heal_candidates`, `heal_events`.

Lives in the same SQLite file as the registry (separate connection, WAL mode).
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS action_state (
    domain             TEXT    NOT NULL,
    action             TEXT    NOT NULL,
    consecutive_empty  INTEGER NOT NULL DEFAULT 0,
    degraded           INTEGER NOT NULL DEFAULT 0 CHECK (degraded IN (0, 1)),
    degraded_reason    TEXT,
    updated_at         TEXT    NOT NULL,
    PRIMARY KEY (domain, action)
);
CREATE TABLE IF NOT EXISTS heal_jobs (
    id           TEXT PRIMARY KEY,
    domain       TEXT NOT NULL,
    actions      TEXT NOT NULL,             -- JSON list of action names
    trigger      TEXT NOT NULL,             -- page_changed | empty_spike | health_check
    reason       TEXT NOT NULL,
    user         TEXT NOT NULL,
    status       TEXT NOT NULL,             -- queued | running | succeeded | needs_review | failed | cancelled
    result       TEXT,                      -- JSON details
    new_version  INTEGER,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS heal_jobs_domain ON heal_jobs (domain, status);
CREATE TABLE IF NOT EXISTS heal_candidates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    domain       TEXT NOT NULL,
    action       TEXT NOT NULL,
    job_id       TEXT NOT NULL,
    action_json  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS heal_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id   TEXT NOT NULL,
    domain   TEXT NOT NULL,
    event    TEXT NOT NULL,                 -- remap_start | remap_done | remap_failed | heal_done
    data     TEXT NOT NULL,                 -- JSON: ms, actions, per-action timings and diffs
    at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS heal_events_job ON heal_events (job_id);
"""

JobStatus = Literal["queued", "running", "succeeded", "needs_review", "failed", "cancelled"]
Trigger = Literal["page_changed", "empty_spike", "health_check"]
OPEN_STATUSES = ("queued", "running")


@dataclass(frozen=True)
class HealJob:
    id: str
    domain: str
    actions: list[str]
    trigger: Trigger
    reason: str
    user: str
    status: JobStatus
    result: dict[str, Any] | None
    new_version: int | None
    created_at: str
    started_at: str | None
    finished_at: str | None

    @property
    def open(self) -> bool:
        return self.status in OPEN_STATUSES

    def to_json(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "domain": self.domain,
            "actions": self.actions,
            "trigger": self.trigger,
            "status": self.status,
            "reason": self.reason,
            "result": self.result,
            "new_version": self.new_version,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        self._conn.close()

    # ---- action_state ------------------------------------------------------

    def _ensure(self, domain: str, action: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO action_state (domain, action, updated_at) VALUES (?, ?, ?)",
            (domain, action, _now()),
        )

    def bump_empty(self, domain: str, action: str) -> int:
        """Count one more consecutive fresh EMPTY. Returns the new count."""
        with self._lock:
            self._ensure(domain, action)
            self._conn.execute(
                "UPDATE action_state SET consecutive_empty = consecutive_empty + 1, updated_at = ? "
                "WHERE domain = ? AND action = ?",
                (_now(), domain, action),
            )
            return self.empty_count(domain, action)

    def reset_empty(self, domain: str, action: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE action_state SET consecutive_empty = 0, updated_at = ? "
                "WHERE domain = ? AND action = ? AND consecutive_empty != 0",
                (_now(), domain, action),
            )

    def empty_count(self, domain: str, action: str) -> int:
        row = self._conn.execute(
            "SELECT consecutive_empty FROM action_state WHERE domain = ? AND action = ?", (domain, action)
        ).fetchone()
        return int(row[0]) if row else 0

    def set_degraded(self, domain: str, action: str, degraded: bool, reason: str | None = None) -> None:
        with self._lock:
            self._ensure(domain, action)
            self._conn.execute(
                "UPDATE action_state SET degraded = ?, degraded_reason = ?, updated_at = ? "
                "WHERE domain = ? AND action = ?",
                (int(degraded), reason if degraded else None, _now(), domain, action),
            )

    def is_degraded(self, domain: str, action: str) -> bool:
        row = self._conn.execute(
            "SELECT degraded FROM action_state WHERE domain = ? AND action = ?", (domain, action)
        ).fetchone()
        return bool(row and row[0])

    # ---- heal_jobs ---------------------------------------------------------

    def create_job(self, domain: str, actions: list[str], trigger: Trigger, reason: str, user: str) -> HealJob:
        job_id = "h_" + secrets.token_hex(6)
        with self._lock:
            self._conn.execute(
                "INSERT INTO heal_jobs (id, domain, actions, trigger, reason, user, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
                (job_id, domain, json.dumps(actions), trigger, reason, user, _now()),
            )
        job = self.get_job(job_id)
        assert job is not None
        return job

    def get_job(self, job_id: str) -> HealJob | None:
        row = self._conn.execute("SELECT * FROM heal_jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def open_job_for(self, domain: str, action: str) -> HealJob | None:
        """A queued or running job for this domain that covers `action`."""
        rows = self._conn.execute(
            "SELECT * FROM heal_jobs WHERE domain = ? AND status IN ('queued', 'running') ORDER BY created_at",
            (domain,),
        ).fetchall()
        for row in rows:
            job = _row_to_job(row)
            if action in job.actions:
                return job
        return None

    def latest_job_for(self, domain: str, action: str) -> HealJob | None:
        rows = self._conn.execute(
            "SELECT * FROM heal_jobs WHERE domain = ? ORDER BY created_at DESC, rowid DESC LIMIT 50", (domain,)
        ).fetchall()
        for row in rows:
            job = _row_to_job(row)
            if action in job.actions:
                return job
        return None

    def next_runnable(self) -> HealJob | None:
        """The oldest queued job whose domain has no running job (one job per domain at a time)."""
        row = self._conn.execute(
            "SELECT * FROM heal_jobs q WHERE status = 'queued' AND NOT EXISTS ("
            "  SELECT 1 FROM heal_jobs r WHERE r.domain = q.domain AND r.status = 'running'"
            ") ORDER BY created_at, rowid LIMIT 1"
        ).fetchone()
        return _row_to_job(row) if row else None

    def start_job(self, job_id: str) -> bool:
        """Mark queued -> running. False if another worker took it."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE heal_jobs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
                (_now(), job_id),
            )
            return cur.rowcount == 1

    def finish_job(
        self, job_id: str, status: JobStatus, result: dict[str, Any], new_version: int | None = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE heal_jobs SET status = ?, result = ?, new_version = ?, finished_at = ? WHERE id = ?",
                (status, json.dumps(result), new_version, _now(), job_id),
            )

    def fail_orphaned_jobs(self) -> int:
        """Jobs left 'running' by a crashed process are marked failed on startup."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE heal_jobs SET status = 'failed', result = ?, finished_at = ? WHERE status = 'running'",
                (json.dumps({"error": "runtime restarted while the job was running"}), _now()),
            )
            return cur.rowcount

    # ---- heal_events -------------------------------------------------------

    def add_event(self, job_id: str, domain: str, event: str, data: dict[str, Any]) -> None:
        """One step of a heal job, with timings (DECISIONS M4): what the heal benchmark reads."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO heal_events (job_id, domain, event, data, at) VALUES (?, ?, ?, ?, ?)",
                (job_id, domain, event, json.dumps(data), _now()),
            )

    def events(self, job_id: str | None = None, domain: str | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        if job_id is not None:
            where.append("job_id = ?")
            args.append(job_id)
        if domain is not None:
            where.append("domain = ?")
            args.append(domain)
        filters = " WHERE " + " AND ".join(where) if where else ""
        sql = f"SELECT * FROM heal_events{filters} ORDER BY id"
        return [
            {"job_id": r["job_id"], "domain": r["domain"], "event": r["event"], "data": json.loads(r["data"]),
             "at": r["at"]}
            for r in self._conn.execute(sql, args).fetchall()
        ]

    # ---- heal_candidates ---------------------------------------------------

    def add_candidate(self, domain: str, action: str, job_id: str, action_json: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO heal_candidates (domain, action, job_id, action_json, created_at) VALUES (?,?,?,?,?)",
                (domain, action, job_id, json.dumps(action_json), _now()),
            )

    def candidates(self, domain: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM heal_candidates WHERE domain = ? ORDER BY id", (domain,)
        ).fetchall()
        return [
            {
                "id": r["id"],
                "action": r["action"],
                "job_id": r["job_id"],
                "status": r["status"],
                "action_json": json.loads(r["action_json"]),
            }
            for r in rows
        ]


def _row_to_job(row: sqlite3.Row) -> HealJob:
    return HealJob(
        id=row["id"],
        domain=row["domain"],
        actions=json.loads(row["actions"]),
        trigger=row["trigger"],
        reason=row["reason"],
        user=row["user"],
        status=row["status"],
        result=json.loads(row["result"]) if row["result"] else None,
        new_version=row["new_version"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
