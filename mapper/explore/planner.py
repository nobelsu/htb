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
        "empty": {"type": "boolean"},
        "reason": {"type": "string"},
        "next_role": _NULLABLE_STR,
        "next_name": _NULLABLE_STR,
    },
    "required": ["ok", "empty", "reason", "next_role", "next_name"],
    "additionalProperties": False,
}

EXAMPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "inputs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "example": {"type": "string"}},
                "required": ["name", "example"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["inputs"],
    "additionalProperties": False,
}

ACTION_HELP = """Actions (one per reply; unused fields are null):
- click: role + name of the element, exactly as in the tree (e.g. role "button", name "Search")
- type: role + name of the field, and text to put in it (replaces what is there)
- press: key (e.g. "Enter"), optionally with role + name of the element to press it in
- navigate: url, a path on this site; never with the task's input in it (type inputs into the page)
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
        # Opus 5.5 always thinks, and thinking counts toward max_tokens: leave room for it
        data = self.llm.ask_json(purpose, prompt, schema, max_tokens=16000)
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
            "the user would type, each with an example value that will certainly return results on this "
            "site (for a search, a well-known name such as a big company or a famous person, not a "
            "descriptive phrase: many sites only match names). None if the task needs no input. "
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
            "This run is recorded, then replayed with made-up input words that the site will find nothing "
            "for. So every action must work the same when there are no results: set any category or "
            "filter BEFORE you type and submit the input (e.g. open that category's own search page first, "
            "or pick it in the search form), so that submitting the input is the action that performs the "
            "task. Never rely on anything that only appears once results are shown.\n"
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
            "or different one), AND that flagged action does not depend on results being shown (the run is "
            "replayed with words that find nothing: e.g. a filter clicked on the results page after "
            "searching does not qualify; filters must be set before the input is submitted). "
            "Set empty=true (with ok=false) only when everything was done right but the "
            "site found nothing for this input (e.g. 'No results found'). Explain in reason.\n"
            f"{nxt}\n\nActions:\n" + "\n".join(history) + f"\n\nPage: {url}\n{tree}"
        )
        return self._ask("check result", prompt, CHECK_SCHEMA)

    # --- 4. a better example when the site found nothing -------------------------------------------
    def new_example(self, task: TaskSpec, tried: list[dict[str, str]]) -> dict[str, str]:
        prompt = (
            f"Task: {task.description}. It was done correctly with these example inputs, but the site "
            f"found nothing: {tried}.\nGive a different example for each input that will certainly "
            "return results on this site: for a search, a well-known name (a big company, a famous "
            "person), not a descriptive phrase. Do not repeat one that was tried.\n"
            f"Inputs: {list(task.inputs)}"
        )
        data = self._ask("new example", prompt, EXAMPLE_SCHEMA)
        new = {i["name"]: i["example"] for i in data["inputs"] if i["name"] in task.inputs and i["example"]}
        if set(new) != set(task.inputs) or new in tried:
            raise PlanError(f"no usable new example for {list(task.inputs)}")
        return new


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
