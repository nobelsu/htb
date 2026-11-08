"""Pydantic v2 models for an action map ("card"), spec v0.4.

These models check shape and types only. Cross-field rules (placeholder
references, step ordering, pagination consistency...) live in `validate.py`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "0.4"

Tier = Literal["http", "impersonate", "browser"]
ContentType = Literal["json", "html", "text", "any"]
ParamType = Literal["string", "integer", "number", "boolean", "enum"]
ExtractSource = Literal["body", "headers", "cookies"]


class _Model(BaseModel):
    # Unknown fields are rejected so mapper typos surface at save time.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Provenance(_Model):
    mapped_at: str
    source: str
    mapper_version: str


class Request(_Model):
    method: str
    url: str
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None


class Expect(_Model):
    status: list[int]
    content_type: ContentType
    paths: list[str] = Field(default_factory=list)


class SessionCheck(Request):
    expect: Expect


class ConnectProvider(_Model):
    id: str
    login_url: str
    session_check: SessionCheck
    logged_out_signals: list[str] = Field(default_factory=list)


class Defaults(_Model):
    tier: Tier = "http"
    auth_provider: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_ms: int | None = Field(default=None, gt=0)
    cache_ttl_s: int | None = Field(default=None, ge=0)
    rate_limit_per_min: int | None = Field(default=None, gt=0)


class Param(_Model):
    type: ParamType
    required: bool = False
    default: Any = None
    description: str | None = None
    example: Any = None
    values: list[Any] | None = None

    @property
    def has_default(self) -> bool:
        return "default" in self.model_fields_set


class Extract(_Model):
    from_: ExtractSource = Field(alias="from")
    type: Literal["jsonpath", "regex", "css"]
    expr: str
    parse: Literal["json"] | None = None
    # Streamed data rows (CARD_SPEC.md "Extraction", Streamed rows): fill in each match's row pointers.
    resolve: Literal["rows"] | None = None
    then: str | None = None

    @model_validator(mode="after")
    def _resolve_needs_parsed_body_regex(self) -> Extract:
        needs = self.type == "regex" and self.parse == "json" and self.from_ == "body"
        if self.resolve is not None and not needs:
            raise ValueError('resolve: "rows" needs type "regex", parse "json" and from "body"')
        return self


class Step(_Model):
    id: str
    request: Request
    timeout_ms: int | None = Field(default=None, gt=0)
    expect: Expect
    extract: dict[str, Extract] = Field(default_factory=dict)


class NextParams(_Model):
    param: str
    increment: int
    page_size: int = Field(gt=0)
    until: str | None = None


class Output(_Model):
    from_: str | None = Field(alias="from")
    fields: dict[str, str] = Field(default_factory=dict)
    limit: int | None = Field(default=None, gt=0)
    next_params: NextParams | None = None


class VerifyAssert(_Model):
    type: Literal["jsonpath"]
    expr: str
    equals: Any


class Verify(_Model):
    request: Request
    timeout_ms: int | None = Field(default=None, gt=0)
    expect: Expect
    assert_: VerifyAssert = Field(alias="assert")


class Example(_Model):
    params: dict[str, Any]
    min_items: int = Field(default=1, ge=0)


class Action(_Model):
    name: str
    description: str
    kind: Literal["read", "write"]
    spends: bool
    idempotent: bool
    requires_confirmation: bool
    params: dict[str, Param] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    steps: list[Step] = Field(min_length=1)
    output: Output
    verify: Verify | None = None
    examples: list[Example]
    tier: Tier | None = None
    auth_provider: str | None = None
    timeout_ms: int | None = Field(default=None, gt=0)
    cache_ttl_s: int | None = Field(default=None, ge=0)
    rate_limit_per_min: int | None = Field(default=None, gt=0)

    # For these fields an explicit value on the action (even null) wins over defaults.

    def effective_auth_provider(self, defaults: Defaults) -> str | None:
        return self.auth_provider if "auth_provider" in self.model_fields_set else defaults.auth_provider

    def effective_tier(self, defaults: Defaults) -> Tier:
        return self.tier if self.tier is not None else defaults.tier

    def effective_timeout_ms(self, defaults: Defaults) -> int | None:
        return self.timeout_ms if self.timeout_ms is not None else defaults.timeout_ms

    def effective_cache_ttl_s(self, defaults: Defaults) -> int | None:
        return self.cache_ttl_s if "cache_ttl_s" in self.model_fields_set else defaults.cache_ttl_s

    def effective_rate_limit_per_min(self, defaults: Defaults) -> int | None:
        if "rate_limit_per_min" in self.model_fields_set:
            return self.rate_limit_per_min
        return defaults.rate_limit_per_min

    def write_step_index(self) -> int | None:
        """Index of the first step whose method is not GET/HEAD, or None."""
        for i, step in enumerate(self.steps):
            if step.request.method.upper() not in ("GET", "HEAD"):
                return i
        return None


class Card(_Model):
    schema_version: Literal["0.4"]
    domain: str
    tool_prefix: str
    base_url: str
    blocked_signals: list[str] = Field(default_factory=list)
    changed_signals: list[str] = Field(default_factory=list)
    provenance: Provenance
    connect_providers: list[ConnectProvider] = Field(default_factory=list)
    defaults: Defaults = Field(default_factory=Defaults)
    actions: list[Action]

    def action(self, name: str) -> Action | None:
        return next((a for a in self.actions if a.name == name), None)

    def provider(self, provider_id: str | None) -> ConnectProvider | None:
        if provider_id is None:
            return None
        return next((p for p in self.connect_providers if p.id == provider_id), None)

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_unset=True, mode="json")
