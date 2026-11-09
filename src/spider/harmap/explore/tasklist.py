"""Task lists: the actions to map for a site, written by a person (``tasks/<domain>.txt``).

The format, one task after another::

    get_company                    <- the action's name (snake_case)
    (universal_name, start?)       <- optional: its inputs; ``?`` marks an optional one
    sign-in                        <- optional: it needs a logged-in session
    LinkedIn: fetch a company ...  <- the description, up to the next task

The description says what the action does and returns. The explorer's LLM reads it to work out how to do
the task in the browser, and the mapper uses it to choose which values of the answer to return.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

NAME = re.compile(r"^[a-z][a-z0-9_]{1,60}$")
INPUTS = re.compile(r"^\((.*)\)$")
TASKS_DIR = Path(__file__).parent / "tasks"


@dataclass
class ListedInput:
    name: str
    optional: bool = False


@dataclass
class ListedTask:
    name: str
    description: str
    inputs: list[ListedInput] = field(default_factory=list)
    sign_in: bool = False


def parse_task_list(text: str) -> list[ListedTask]:
    lines = [ln.strip() for ln in text.splitlines()]

    def starts_task(i: int) -> bool:
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        return bool(NAME.match(lines[i])) and (bool(INPUTS.match(nxt)) or nxt == "sign-in")

    tasks: list[ListedTask] = []
    i = 0
    while i < len(lines):
        if not starts_task(i):
            i += 1  # headings or blank lines before the first task
            continue
        task = ListedTask(lines[i], "")
        i += 1
        if i < len(lines) and (m := INPUTS.match(lines[i])):
            for part in m.group(1).split(","):
                part = part.strip()
                if part:
                    task.inputs.append(ListedInput(part.rstrip("?").strip(), part.endswith("?")))
            i += 1
        if i < len(lines) and lines[i] == "sign-in":
            task.sign_in = True
            i += 1
        text_lines = []
        while i < len(lines) and not starts_task(i):
            if lines[i]:
                text_lines.append(lines[i])
            i += 1
        task.description = " ".join(text_lines)
        tasks.append(task)
    return tasks


def load_task_list(path: str | Path) -> list[ListedTask]:
    return parse_task_list(Path(path).read_text(encoding="utf-8"))


def task_list_for(domain: str) -> Path | None:
    """The bundled task list for a site (``tasks/<domain>.txt``), if there is one."""
    path = TASKS_DIR / f"{domain}.txt"
    return path if path.exists() else None
