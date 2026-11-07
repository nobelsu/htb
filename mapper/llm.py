"""Optional LLM steps (Claude Sonnet 5.5): goal tie-break, output field choice, descriptions.

Off unless ``ANTHROPIC_API_KEY`` is set and the ``anthropic`` package (the ``[llm]`` extra) is installed;
``MAPPER_NO_LLM=1`` forces it off. Every method returns ``None`` when the LLM is off or the call fails,
and callers then use their deterministic fallback, so the whole pipeline runs offline.

Only site *content* is sent (endpoint paths, response field names, sample items, param names and the
operator's example inputs). Never headers, cookies or tokens.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

MODEL = "claude-sonnet-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"  # pairs with fallbacks="default"


@dataclass
class LLMCall:
    purpose: str
    ok: bool
    detail: str = ""


def sdk_available() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass
class LLM:
    client: Any = None
    enabled: bool | None = None
    calls: list[LLMCall] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.enabled is None:
            self.enabled = bool(os.environ.get("ANTHROPIC_API_KEY")) and not os.environ.get("MAPPER_NO_LLM")
        if self.enabled and self.client is None and not sdk_available():
            self.enabled = False

    def _json(self, purpose: str, prompt: str, schema: dict, max_tokens: int = 4000) -> dict | None:
        if not self.enabled:
            return None
        try:
            import anthropic

            client = self.client or anthropic.Anthropic()
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:  # any SDK/network failure → deterministic fallback
            self.calls.append(LLMCall(purpose, False, f"{type(e).__name__}: {e}"[:200]))
            return None
        if response.stop_reason == "refusal":
            self.calls.append(LLMCall(purpose, False, "refused"))
            return None
        text = next((b.text for b in response.content if b.type == "text"), None)
        try:
            data = json.loads(text or "")
        except ValueError:
            self.calls.append(LLMCall(purpose, False, "response was not JSON"))
            return None
        self.calls.append(LLMCall(purpose, True, f"{MODEL}"))
        return data

    # --- the three optional steps ------------------------------------------------------------

    def pick_goal(self, task: str, candidates: list[str]) -> int | None:
        """Index of the candidate request that performs ``task``."""
        schema = {
            "type": "object",
            "properties": {"index": {"type": "integer"}, "reason": {"type": "string"}},
            "required": ["index", "reason"],
            "additionalProperties": False,
        }
        listing = "\n".join(f"{i}. {c}" for i, c in enumerate(candidates))
        prompt = (
            f"A person performed the task {task!r} on a website. These requests all carried their input. "
            f"Which one actually performs the task (not autocomplete, logging or prefetch)?\n{listing}"
        )
        data = self._json("goal tie-break", prompt, schema, max_tokens=1000)
        if data is None or not 0 <= int(data.get("index", -1)) < len(candidates):
            return None
        return int(data["index"])

    def pick_output_fields(self, task: str, item: Any, candidates: list[str]) -> list[str] | None:
        """3–8 of ``candidates`` (JSONPaths relative to one result item) worth returning to an agent."""
        schema = {
            "type": "object",
            "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
            "required": ["paths"],
            "additionalProperties": False,
        }
        prompt = (
            f"Task: {task!r}. One result item from the site's response:\n"
            f"{json.dumps(item, indent=1)[:6000]}\n\n"
            "Pick 3 to 8 of these JSONPaths (relative to the item) that an AI agent would want back: "
            f"names, titles, identifiers, URLs, short descriptive fields. Candidates: {candidates}"
        )
        data = self._json("output fields", prompt, schema)
        if data is None:
            return None
        picked = [p for p in data.get("paths", []) if p in candidates]
        return picked[:8] if len(picked) >= 1 else None

    def describe(self, summary: dict) -> dict | None:
        """``{"description": str, "params": {name: str}}`` for an action's MCP tool."""
        schema = {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "params": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}, "description": {"type": "string"}},
                        "required": ["name", "description"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["description", "params"],
            "additionalProperties": False,
        }
        safety = (
            "It is read-only and safe to retry; say so."
            if summary.get("kind") == "read"
            else "It changes state on the user's account; say so and that the user must approve it first."
        )
        prompt = (
            "Write an MCP tool description (one or two sentences) for an AI agent, and a short description "
            f"for each parameter. {safety}\n{json.dumps(summary, indent=1)}"
        )
        data = self._json("descriptions", prompt, schema)
        if data is None or not isinstance(data.get("description"), str):
            return None
        params = {p["name"]: p["description"] for p in data.get("params", []) if isinstance(p, dict)}
        return {"description": data["description"].strip(), "params": params}
