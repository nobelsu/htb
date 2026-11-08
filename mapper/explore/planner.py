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


GIVEN = ["typed", "address", "picked", "other"]


@dataclass
class TaskSpec:
    name: str
    description: str
    kind: str  # read | write
    start_path: str
    inputs: dict[str, str]  # typed or in-the-address inputs → a realistic example (replays swap in markers)
    paginates: bool = False
    # how each listed input is given in the browser: typed | address | picked | other (supplied by the
    # site itself, like the member's own id, or paging); picked ones are chosen by clicking an item
    via: dict[str, str] = field(default_factory=dict)

    @property
    def picks(self) -> list[str]:
        return [k for k, how in self.via.items() if how == "picked"]

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "start_path": self.start_path,
            "inputs": self.inputs,
            "paginates": self.paginates,
            "via": self.via,
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
        "picks": _NULLABLE_STR,
        "reason": {"type": "string"},
    },
    "required": ["action", "role", "name", "text", "key", "url", "performs_task", "picks", "reason"],
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

LISTED_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["read", "write"]},
                    "start_path": {"type": "string"},
                    "paginates": {"type": "boolean"},
                    "inputs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "given": {"type": "string", "enum": GIVEN},
                                "example": _NULLABLE_STR,
                            },
                            "required": ["name", "given", "example"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["name", "kind", "start_path", "paginates", "inputs"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tasks"],
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
- navigate: url, a path on this site (it may contain an input given "in the address")
- wait: the page is still loading
- done: the page now shows the result of the task
- give_up: the task cannot be done here, or you flagged the wrong action (the run then restarts)
picks: on the click that chooses an item from a list for a "picked" input, that input's name (else null).
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
            else f"List every action a user of this site might want an agent to do for them, up to "
            f"{max_tasks}: an extensive list, reads and writes, including ones that start on deeper pages "
            "(an item's own page, a profile, a settings or inbox page). Describe in each what it returns."
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

    # --- 1b. a person's list: work out how to do each one ------------------------------------------
    def plan_listed(self, site: str, listed: list, chunk: int = 10) -> list[TaskSpec]:
        """TaskSpecs for a person's task list (tasklist.ListedTask), one LLM call per ``chunk`` tasks."""
        from concurrent.futures import ThreadPoolExecutor

        groups = [listed[i : i + chunk] for i in range(0, len(listed), chunk)]
        with ThreadPoolExecutor(max_workers=len(groups) or 1) as pool:
            planned = list(pool.map(lambda g: self._plan_chunk(site, g), groups))
        return [spec for group in planned for spec in group]

    def _plan_chunk(self, site: str, listed: list) -> list[TaskSpec]:
        blocks = "\n\n".join(
            f"{t.name}({', '.join(i.name + ('?' if i.optional else '') for i in t.inputs)})\n{t.description}"
            for t in listed
        )
        prompt = (
            "We are turning a website into tools an AI agent can call instead of using a browser. A person "
            "listed the actions they want; each will be done once in a logged-in browser and recorded. "
            "For each action below decide:\n"
            "- kind: 'write' if it changes anything (send, post, connect, accept, follow), else 'read'.\n"
            "- start_path: the page on this site to start from (a path).\n"
            "- paginates: true if its results come in pages.\n"
            "- for every input, how a person gives it in the browser: 'typed' into a box or form; "
            "'address' as part of the page address (e.g. /company/<name>/); 'picked' by clicking one "
            "item in a list on the page (e.g. a conversation, a post, a person); 'other' if the site "
            "supplies it itself (like the member's own id) or it is paging (start, count, cursors).\n"
            "- example: for typed and address inputs, a value that certainly exists on this site (a "
            "well-known company or public figure, or the member's own profile when the action is about "
            "them). Null for picked and other inputs.\n"
            "Keep every action and its name exactly.\n\n"
            f"Actions:\n{blocks}\n\nThe site, as the logged-in member sees it:\n{site[:30000]}"
        )
        data = self._ask("plan listed tasks", prompt, LISTED_SCHEMA)
        by_name = {t["name"]: t for t in data["tasks"]}
        specs = []
        for task in listed:
            t = by_name.get(task.name)
            if t is None:
                raise PlanError(f"the plan left out {task.name!r}")
            via, inputs = {}, {}
            for i in t["inputs"]:
                via[i["name"]] = i["given"]
                if i["given"] in ("typed", "address"):
                    if not i.get("example"):
                        raise PlanError(f"{task.name}: no example for {i['given']} input {i['name']!r}")
                    inputs[i["name"]] = i["example"]
            path = t["start_path"] if t["start_path"].startswith("/") else "/" + t["start_path"]
            specs.append(TaskSpec(task.name, task.description, t["kind"], path, inputs, t["paginates"], via))
        return specs

    # --- 2. the next action, for the step-by-step driver ----------------------------------------
    def next_action(
        self, task: TaskSpec, url: str, tree: str, history: list[str], feedback: list[str]
    ) -> dict:
        how = task.via or {k: "typed" for k in task.inputs}
        given = []
        for k, way in how.items():
            if way == "typed":
                given.append(f"{k}: type exactly {task.inputs[k]!r} into the page")
            elif way == "address":
                given.append(
                    f"{k}: open an address containing exactly {task.inputs[k]!r} (e.g. a page for it)"
                )
            elif way == "picked":
                given.append(f"{k}: pick one item from a list on the page by clicking it (set picks={k!r})")
        values = "; ".join(given)
        rules = "Stay on this site. Never log out, delete anything or change settings."
        if task.kind == "read":
            rules += " This task only looks things up: do not follow, connect, post, send, save or buy."
        else:
            rules += (
                " This task changes something: do everything up to AND including the final send/submit "
                "click, and flag that click as performing the task. The browser intercepts it, so nothing "
                "is actually sent. Never enter payment, card or bank details or passwords: give_up instead."
            )
        done = "\n".join(history) or "(nothing yet)"
        earlier = ""
        if feedback:
            tries = "\n".join(f"- {f}" for f in feedback)
            earlier = f"\nEarlier attempts failed (the browser restarted):\n{tries}"
        prompt = (
            "You are operating a web browser for a logged-in user to do ONE task on this site.\n"
            f"Task: {task.description} (kind {task.kind}).\n"
            f"Inputs: {values or 'none needed'}.\n"
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
            if task.paginates and task.kind == "read"
            else "Leave next_role and next_name null."
        )
        if task.kind == "write":
            return self._ask(
                "check result",
                f"Task: {task.description} (inputs: {task.inputs or 'none'}).\n"
                "This task changes something, so its final send/submit click was intercepted on purpose: "
                "nothing was sent, and the page may show an error or nothing new. Answer ok=true only if "
                "the action flagged as performing the task is that final send/submit, done with "
                "everything the task needs filled in (exact inputs, right recipient or item). empty=false. "
                f"Explain in reason. {nxt}\n\nActions:\n" + "\n".join(history) + f"\n\nPage: {url}\n{tree}",
                CHECK_SCHEMA,
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
