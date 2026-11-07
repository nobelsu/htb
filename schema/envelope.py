"""The result envelope returned for every action call."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from schema.card import Tier


class ResultCode(StrEnum):
    BAD_PARAMS = "BAD_PARAMS"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    PAGE_CHANGED = "PAGE_CHANGED"
    EMPTY = "EMPTY"
    SUCCESS = "SUCCESS"
    VERIFY_FAILED = "VERIFY_FAILED"


class ErrorInfo(BaseModel):
    code: ResultCode
    message: str
    hint: str | None = None
    step: str | None = None  # failing step id, "verify", or None before any request
    retry_after_s: int | None = None  # RATE_LIMITED only (DECISIONS.md D4)


class Confirmation(BaseModel):
    id: str
    expires_at: str
    summary: str


class Heal(BaseModel):
    job_id: str
    retry_after_s: int


class Meta(BaseModel):
    ms: int
    tier: Tier
    cached: bool = False
    degraded: bool = False
    map_version: int | None = None
    run_id: str


class Envelope(BaseModel):
    status: ResultCode
    data: list[Any] | None = None  # dicts when output.fields is set, raw items otherwise
    next_params: dict[str, Any] | None = None
    confirmation: Confirmation | None = None
    heal: Heal | None = None
    meta: Meta
    error: ErrorInfo | None = None
