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
import re
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
    model: str = MODEL  # the mapper's own steps use these defaults; the explorer asks for more
    effort: str = "low"

    def __post_init__(self) -> None:
        if self.enabled is None:
            self.enabled = bool(os.environ.get("ANTHROPIC_API_KEY")) and not os.environ.get("MAPPER_NO_LLM")
        if self.enabled and self.client is None and not sdk_available():
            self.enabled = False

    def ask_json(self, purpose: str, prompt: str, schema: dict, max_tokens: int = 4000) -> dict | None:
        if not self.enabled:
            return None
        try:
            import anthropic

            client = self.client or anthropic.Anthropic()
            response = client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:  # any SDK/network failure → deterministic fallback
            self.calls.append(LLMCall(purpose, False, f"{type(e).__name__}: {e}"[:200]))
            return None
        if response.stop_reason == "refusal":
            self.calls.append(LLMCall(purpose, False, "refused"))
            return None
        if response.stop_reason == "max_tokens":
            self.calls.append(LLMCall(purpose, False, f"ran out of tokens ({max_tokens})"))
            return None
        text = next((b.text for b in response.content if b.type == "text"), None)
        try:
            data = json.loads(text or "")
        except ValueError:
            self.calls.append(LLMCall(purpose, False, "response was not JSON"))
            return None
        self.calls.append(LLMCall(purpose, True, f"{self.model} ({self.effort})"))
        return data

    # --- the three optional steps ------------------------------------------------------------

    def pick_goal(
        self, task: str, candidates: list[str], description: str = "", typed: bool = True
    ) -> int | None:
        """Index of the candidate request that performs ``task``."""
        schema = {
            "type": "object",
            "properties": {"index": {"type": "integer"}, "reason": {"type": "string"}},
            "required": ["index", "reason"],
            "additionalProperties": False,
        }
        listing = "\n".join(f"{i}. {c}" for i, c in enumerate(candidates))
        about = f" It returns: {description[:1500]}" if description else ""
        if typed:
            prompt = (
                f"A person performed the task {task!r} on a website.{about} These requests all carried "
                "their input. Which one actually performs the task (not autocomplete, logging or "
                f"prefetch)?\n{listing}"
            )
        else:
            prompt = (
                f"A person performed the task {task!r} on a website; it takes no input.{about} These "
                "answers all hold what the page then showed. Which request should an agent call to get "
                "that data? Prefer a data answer (JSON) that holds the fields named above over a page "
                f"or a big component answer that only happens to show them.\n{listing}"
            )
        data = self.ask_json("goal tie-break", prompt, schema, max_tokens=1000)
        if data is None or not 0 <= int(data.get("index", -1)) < len(candidates):
            return None
        return int(data["index"])

    def pick_output_fields(
        self, task: str, item: Any, candidates: list[str], description: str = ""
    ) -> list[str] | None:
        """The ``candidates`` (JSONPaths relative to one result item) relevant to what the action returns."""
        schema = {
            "type": "object",
            "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
            "required": ["paths"],
            "additionalProperties": False,
        }
        about = f" It returns: {description[:1500]}" if description else ""
        prompt = (
            f"Task: {task!r}.{about} One result item from the site's response:\n"
            f"{json.dumps(item, indent=1)[:6000]}\n\n"
            "Pick EVERY one of these JSONPaths (relative to the item) whose value is relevant to what this "
            "action returns: names, titles, identifiers, URLs, counts, dates, short descriptive fields. As "
            "many as apply; skip only what is unrelated to the action (styling, tracking, UI labels). "
            f"Candidates: {candidates}"
        )
        data = self.ask_json("output fields", prompt, schema)
        if data is None:
            return None
        picked = [p for p in data.get("paths", []) if p in candidates]
        return picked[:30] if len(picked) >= 1 else None

    def name_output_fields(
        self, task: str, candidates: list[tuple[str, list[Any]]], description: str = ""
    ) -> dict[str, str] | None:
        """For streamed answers: pick the ``candidates`` (path, example values from a few results) relevant
        to what the action returns, and name them. Returns ``{name: path}``."""
        schema = {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"index": {"type": "integer"}, "name": {"type": "string"}},
                        "required": ["index", "name"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["fields"],
            "additionalProperties": False,
        }
        listing = "\n".join(f"{i}. {[str(v)[:80] for v in vals]}" for i, (_, vals) in enumerate(candidates))
        about = f" It returns: {description[:1500]}" if description else ""
        prompt = (
            f"Task: {task!r}.{about} Each line holds one value from each of a few results the site "
            "returned (the same spot in every result). Pick EVERY line relevant to what this action "
            "returns (names, links, ids, counts, dates, descriptions: as many as apply, using the names the "
            "description gives where they fit) and give each a short snake_case name. Skip only button "
            f"labels, repeated boilerplate and anything not about the result itself.\n{listing}"
        )
        data = self.ask_json("output field names", prompt, schema, max_tokens=1500)
        if data is None:
            return None
        out: dict[str, str] = {}
        for f in data.get("fields", []):
            name, i = str(f.get("name", "")), f.get("index", -1)
            if re.fullmatch(r"[a-z][a-z0-9_]{0,40}", name) and name not in out and 0 <= i < len(candidates):
                out[name] = candidates[i][0]
        return out if out else None

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
        data = self.ask_json("descriptions", prompt, schema)
        if data is None or not isinstance(data.get("description"), str):
            return None
        params = {p["name"]: p["description"] for p in data.get("params", []) if isinstance(p, dict)}
        return {"description": data["description"].strip(), "params": params}
