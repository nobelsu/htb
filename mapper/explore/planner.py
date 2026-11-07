"""The explorer's LLM steps: decide a site's main tasks, write a click/type script per task, and find
the next-page control. All replies are structured JSON and are checked before use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from mapper.capture import driver
from mapper.llm import LLM

TASK_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
STEP_TYPES = ["navigate", "input", "click", "key", "wait", "wait_for", "mark"]
_NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}


class PlanError(ValueError):
    pass


@dataclass
class TaskSpec:
    name: str
    description: str
    kind: str  # read | write
    start_path: str
    inputs: dict[str, str]  # param name → realistic example
    paginates: bool = False

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "start_path": self.start_path,
            "inputs": self.inputs,
            "paginates": self.paginates,
        }


TASKS_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "kind": {"type": "string", "enum": ["read", "write"]},
                    "start_path": {"type": "string"},
                    "inputs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}, "example": {"type": "string"}},
                            "required": ["name", "example"],
                            "additionalProperties": False,
                        },
                    },
                    "paginates": {"type": "boolean"},
                },
                "required": ["name", "description", "kind", "start_path", "inputs", "paginates"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tasks"],
    "additionalProperties": False,
}

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": STEP_TYPES},
                    "url": _NULLABLE_STR,
                    "selector": _NULLABLE_STR,
                    "value": _NULLABLE_STR,
                    "key": _NULLABLE_STR,
                },
                "required": ["type", "url", "selector", "value", "key"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["steps", "notes"],
    "additionalProperties": False,
}

NEXT_SCHEMA = {
    "type": "object",
    "properties": {"selector": _NULLABLE_STR, "reason": {"type": "string"}},
    "required": ["selector", "reason"],
    "additionalProperties": False,
}

STEP_FORMAT = """Step format (JSON objects, run in order by a Playwright driver):
- {"type": "navigate", "url": "/path"}             go to a page (relative to the site)
- {"type": "input", "selector": "#q", "value": "{{params.query}}"}   fill a field
- {"type": "click", "selector": "button[type=\\"submit\\"]"}
- {"type": "key", "selector": "#q", "key": "Enter"}
- {"type": "wait_for", "selector": "#results li"}   wait until something appears
- {"type": "mark"}   goal mark: right AFTER the click/Enter that performs the task
Unused fields are null. Use {{params.NAME}} exactly where each user input goes."""


class Planner:
    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    def _ask(self, purpose: str, prompt: str, schema: dict) -> dict:
        if not self.llm.enabled:
            raise PlanError("the explorer needs an LLM: set ANTHROPIC_API_KEY and install the [llm] extra")
        data = self.llm.ask_json(purpose, prompt, schema, max_tokens=8000)
        if data is None:
            detail = self.llm.calls[-1].detail if self.llm.calls else "no reply"
            raise PlanError(f"{purpose}: LLM call failed ({detail})")
        return data

    # --- 1. which tasks ----------------------------------------------------------------------
    def discover(self, site: str, max_tasks: int = 6, wanted: list[str] | None = None) -> list[TaskSpec]:
        focus = (
            "The user asked for exactly these tasks (describe each one): " + "; ".join(wanted)
            if wanted
            else f"List the 3 to {max_tasks} tasks people most often come to this site for."
        )
        prompt = (
            "We are turning a website into tools an AI agent can call instead of using a browser. Below are "
            "snapshots of the site's main pages, seen by a logged-in user.\n"
            f"{focus}\n"
            "Pick tasks that fit what this site is for (a professional network: search people or companies, "
            "view a profile, follow a company; a shop: search products, view a product, add to cart). "
            "Each task must be doable by typing and clicking, starting from start_path (a path on this "
            "site). inputs are what "
            "the user would type, each with a realistic example value (none if the task needs no input). "
            "kind is 'write' if the task changes anything for the user (follow, post, send, add to "
            "cart, save), "
            "else 'read'. paginates is true if the results come in pages. name is short snake_case.\n\n"
            f"{site}"
        )
        data = self._ask("task discovery", prompt, TASKS_SCHEMA)
        return [self._spec(t) for t in data["tasks"]][: max(len(wanted or []), max_tasks)]

    @staticmethod
    def _spec(t: dict) -> TaskSpec:
        name = re.sub(r"[^a-z0-9_]+", "_", t["name"].lower()).strip("_")
        if not TASK_NAME_RE.match(name):
            raise PlanError(f"bad task name {t['name']!r}")
        inputs = {}
        for i in t["inputs"]:
            pname = re.sub(r"[^a-z0-9_]+", "_", i["name"].lower()).strip("_")
            if not pname or pname.startswith("_") or pname == "page":
                raise PlanError(f"task {name}: bad input name {i['name']!r}")
            inputs[pname] = i["example"]
        path = t["start_path"] if t["start_path"].startswith("/") else "/" + t["start_path"]
        return TaskSpec(name, t["description"], t["kind"], path, inputs, bool(t["paginates"]))

    # --- 2. how to do one task ------------------------------------------------------------------
    def write_script(self, task: TaskSpec, page: str, feedback: list[str]) -> list[dict]:
        tries = ""
        if feedback:
            tries = "\nEarlier attempts failed. Fix the script:\n" + "\n".join(f"- {f}" for f in feedback)
        inputs = ", ".join(f"{{{{params.{k}}}}} (e.g. {v!r})" for k, v in task.inputs.items())
        prompt = (
            f"Task: {task.description} (name {task.name!r}, kind {task.kind}).\n"
            f"Inputs: {inputs or 'none'}.\n"
            "Write the script that performs the task on this site, starting on the page below. "
            "Prefer selectors "
            "from the snapshot. Do not log in, do not leave the site.\n"
            f"{STEP_FORMAT}{tries}\n\nPage:\n{page}"
        )
        data = self._ask("script", prompt, SCRIPT_SCHEMA)
        return self.check_script(task, data["steps"])

    @staticmethod
    def check_script(task: TaskSpec, raw: list[dict]) -> list[dict]:
        steps = [{k: v for k, v in s.items() if v is not None} for s in raw]
        try:
            driver.load_script(steps)
        except driver.ScriptError as e:
            raise PlanError(str(e)) from e
        for s in steps:
            if s["type"] in ("input", "click", "key", "wait_for") and not s.get("selector"):
                raise PlanError(f"a {s['type']} step has no selector")
            if s["type"] == "navigate" and not s.get("url"):
                raise PlanError("a navigate step has no url")
            if s["type"] == "input" and "value" not in s:
                raise PlanError("an input step has no value")
        if not any(s["type"] == "mark" for s in steps):
            raise PlanError("the script has no goal mark")
        text = repr(steps)
        missing = [p for p in task.inputs if "{{params." + p + "}}" not in text]
        if missing:
            raise PlanError(f"the script never types the inputs {missing}")
        return steps

    # --- 3. the next-page control -------------------------------------------------------------
    def find_next(self, task: TaskSpec, page: str) -> str | None:
        prompt = (
            f"This page shows the results of the task {task.description!r}. "
            "Give the CSS selector of the control "
            "that loads the NEXT page of results (from the snapshot), or null if there is none.\n\n" + page
        )
        data = self._ask("next page", prompt, NEXT_SCHEMA)
        sel = data.get("selector")
        return sel or None


@dataclass
class Attempt:
    script: list[dict]
    error: str | None = None


@dataclass
class TaskPlan:
    spec: TaskSpec
    script: list[dict] | None = None
    next_selector: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
