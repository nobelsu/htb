"""The explorer's LLM steps: decide a site's main tasks, then (for the step-by-step driver in agent.py)
choose the next browser action and check the final page. All replies are structured JSON.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from mapper.llm import LLM

TASK_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
ACTIONS = ["click", "type", "press", "navigate", "wait", "done", "give_up"]
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

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ACTIONS},
        "role": _NULLABLE_STR,
        "name": _NULLABLE_STR,
        "text": _NULLABLE_STR,
        "key": _NULLABLE_STR,
        "url": _NULLABLE_STR,
        "performs_task": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["action", "role", "name", "text", "key", "url", "performs_task", "reason"],
    "additionalProperties": False,
}

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "reason": {"type": "string"},
        "next_role": _NULLABLE_STR,
        "next_name": _NULLABLE_STR,
    },
    "required": ["ok", "reason", "next_role", "next_name"],
    "additionalProperties": False,
}

ACTION_HELP = """Actions (one per reply; unused fields are null):
- click: role + name of the element, exactly as in the tree (e.g. role "button", name "Search")
- type: role + name of the field, and text to put in it (replaces what is there)
- press: key (e.g. "Enter"), optionally with role + name of the element to press it in
- navigate: url, a path on this site
- wait: the page is still loading
- done: the page now shows the result of the task
- give_up: the task cannot be done here, or you flagged the wrong action (the run then restarts)
performs_task: true on exactly ONE action: the one that makes the site load the final result the task
asks for (the search click or Enter, or the filter click if you filter after searching; the submit
of a form). The recording marks that moment, so flag it on that action itself, never later."""


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

    # --- 2. the next action, for the step-by-step driver ----------------------------------------
    def next_action(
        self, task: TaskSpec, url: str, tree: str, history: list[str], feedback: list[str]
    ) -> dict:
        values = "; ".join(f"{k} = {v!r}" for k, v in task.inputs.items())
        rules = "Stay on this site. Never log out, delete anything or change settings."
        if task.kind == "read":
            rules += " This task only looks things up: do not follow, connect, post, send, save or buy."
        done = "\n".join(history) or "(nothing yet)"
        earlier = ""
        if feedback:
            tries = "\n".join(f"- {f}" for f in feedback)
            earlier = f"\nEarlier attempts failed (the browser restarted):\n{tries}"
        prompt = (
            "You are operating a web browser for a logged-in user to do ONE task on this site.\n"
            f"Task: {task.description} (kind {task.kind}).\n"
            f"Type these values exactly where the task needs them: {values or 'no input needed'}.\n"
            "If the task names a category or filter (e.g. only companies), make sure it is applied.\n"
            f"{rules}\n{ACTION_HELP}\n{earlier}\n"
            f"Done so far:\n{done}\n\n"
            f"Current page: {url}\nAccessibility tree (typed field contents are hidden):\n{tree}"
        )
        return self._ask("next action", prompt, ACTION_SCHEMA)

    # --- 3. did it work? --------------------------------------------------------------------------
    def check_result(self, task: TaskSpec, url: str, tree: str, history: list[str]) -> dict:
        nxt = (
            "Also give the role and name of the control that loads the NEXT page of results "
            "(null if there is none)."
            if task.paginates
            else "Leave next_role and next_name null."
        )
        prompt = (
            f"Task: {task.description} (inputs: {task.inputs or 'none'}).\n"
            "Below is what was done in the browser and the page it ended on. Answer ok=true only if this "
            "page shows the result of the task, including any category or filter the task names, AND the "
            "action flagged as performing the task is the one that loaded this result (not an earlier "
            "or different one). Explain in reason.\n"
            f"{nxt}\n\nActions:\n" + "\n".join(history) + f"\n\nPage: {url}\n{tree}"
        )
        return self._ask("check result", prompt, CHECK_SCHEMA)


@dataclass
class Attempt:
    actions: list[dict]  # what Claude did in the live run, with each action's outcome
    error: str | None = None


@dataclass
class TaskPlan:
    spec: TaskSpec
    script: list[dict] | None = None  # the replay of the live run, used for the other runs
    next_selector: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
