"""Capture bundle layout: ``captures/<domain>/<task>/<run>/`` (see docs/harmap.md)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

HAR_FILE = "traffic.har"
STATE_FILE = "state.json"
ACTIONS_FILE = "actions.jsonl"
META_FILE = "meta.json"

RUNS = ("m1", "m2", "real", "page2")
MARKER_RUNS = ("m1", "m2")
REAL_RUNS = ("real", "page2")


def bundle_dir(root: str | Path, domain: str, task: str, run: str) -> Path:
    return Path(root) / domain / task / run


def iso_now() -> str:
    """UTC timestamp in the same shape as JS ``toISOString()``: ``2026-11-07T15:02:11.512Z``."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
