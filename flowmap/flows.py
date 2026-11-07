"""Flows: one person-recorded action, stored as JSON at ``flows/{domain}/{action}.json``.

A flow is a short list of browser steps (goto, fill, press, click, select, wait_for) with the action's inputs
written as ``{{name}}`` placeholders. Each input is either

- ``marker: true``: capture plants a fresh unlikely string (a marker) in its place, so inference can find
  where the value travels in the site's requests. It still needs one real value in ``examples`` (marker
  searches find nothing, so the outputs come from a real run, and the card needs an example);
- ``marker: false``: for values a marker can't stand in for (a dropdown, an enum); ``examples`` then holds
  at least two different real values, and inference compares the two runs instead.

Optional fields: ``results_selector`` (each result's visible text, to match against responses),
``next_page`` (what to click for page 2) and ``auth_provider`` (the login the action needs).

``from_codegen`` turns a ``playwright codegen --target python`` script into a flow.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FLOW_VERSION = 1
NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# Locator methods a target may chain, in the order Playwright offers them.
LOCATOR_METHODS = (
    "locator",
    "get_by_role",
    "get_by_text",
    "get_by_label",
    "get_by_placeholder",
    "get_by_alt_text",
    "get_by_title",
    "get_by_test_id",
    "filter",
    "nth",
    "first",
    "last",
)
PROPERTIES = {"first", "last"}  # used as attributes, not calls


class FlowError(ValueError):
    """A flow that can't be loaded, converted or played."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


Scalar = str | int | float | bool | None


class LocatorCall(_Model):
    """One link of a locator chain, e.g. ``get_by_role("link", name="Next", exact=True)``."""

    method: Literal[
        "locator",
        "get_by_role",
        "get_by_text",
        "get_by_label",
        "get_by_placeholder",
        "get_by_alt_text",
        "get_by_title",
        "get_by_test_id",
        "filter",
        "nth",
        "first",
        "last",
    ]
    args: list[Scalar] = []
    kwargs: dict[str, Scalar] = {}


# A target is a selector string for ``page.locator`` or a locator chain from codegen.
Target = str | list[LocatorCall]


class Goto(_Model):
    op: Literal["goto"] = "goto"
    url: str


class Fill(_Model):
    op: Literal["fill"] = "fill"
    target: Target
    value: str


class Press(_Model):
    op: Literal["press"] = "press"
    target: Target | None = None  # None presses on the page (the focused element)
    key: str


class Click(_Model):
    op: Literal["click"] = "click"
    target: Target


class Select(_Model):
    op: Literal["select"] = "select"
    target: Target
    value: str


class WaitFor(_Model):
    """Wait for an element to show, a URL, or a load state. At least one."""

    op: Literal["wait_for"] = "wait_for"
    target: Target | None = None
    url: str | None = None  # a glob, as Playwright's wait_for_url takes
    load_state: Literal["load", "domcontentloaded", "networkidle"] | None = None
    timeout_ms: int = Field(default=10_000, ge=100, le=60_000)

    @model_validator(mode="after")
    def _something(self) -> WaitFor:
        if self.target is None and self.url is None and self.load_state is None:
            raise ValueError("wait_for needs a target, a url or a load_state")
        return self


Step = Annotated[Goto | Fill | Press | Click | Select | WaitFor, Field(discriminator="op")]


class Input(_Model):
    marker: bool = True
    examples: list[str] = Field(min_length=1)
    description: str | None = None

    @model_validator(mode="after")
    def _examples(self) -> Input:
        if not self.marker and len(set(self.examples)) < 2:
            raise ValueError("an input with marker: false needs two different real values in examples")
        return self


class Flow(_Model):
    version: Literal[1] = 1
    domain: str
    action: str
    description: str | None = None
    kind: Literal["read", "write"] | None = None  # a hint; inference decides, and a write is never downgraded
    auth_provider: str | None = None
    inputs: dict[str, Input] = {}
    steps: list[Step] = Field(min_length=1)
    results_selector: Target | None = None
    next_page: Target | None = None

    @field_validator("action")
    @classmethod
    def _action(cls, v: str) -> str:
        if not NAME.match(v):
            raise ValueError(
                "action must be snake_case: lowercase letters, digits and _, starting with a letter"
            )
        return v

    @field_validator("domain")
    @classmethod
    def _domain(cls, v: str) -> str:
        if not v or "/" in v or ":" in v or v != v.lower():
            raise ValueError("domain is a lowercase host name like example.com")
        return v

    @model_validator(mode="after")
    def _placeholders(self) -> Flow:
        if not isinstance(self.steps[0], Goto):
            raise ValueError("a flow starts with a goto step")
        for name in self.inputs:
            if not NAME.match(name):
                raise ValueError(f"input {name!r} must be snake_case")
        used = {name for step in self.steps for name in placeholders(step)}
        unknown = used - set(self.inputs)
        if unknown:
            raise ValueError(f"steps use undeclared inputs: {', '.join(sorted(unknown))}")
        unused = set(self.inputs) - used
        if unused:
            raise ValueError(f"inputs never used in a step: {', '.join(sorted(unused))}")
        return self

    def example_values(self, index: int = 0) -> dict[str, str]:
        """The real values of example run ``index`` (each input's examples, cycling when one has fewer)."""
        return {name: inp.examples[index % len(inp.examples)] for name, inp in self.inputs.items()}


def placeholders(step: Goto | Fill | Press | Click | Select | WaitFor) -> list[str]:
    """Input names a step uses (in its URL or value)."""
    texts = [getattr(step, f) for f in ("url", "value") if isinstance(getattr(step, f, None), str)]
    return [m.group(1) for text in texts for m in PLACEHOLDER.finditer(text)]


def render(text: str, values: dict[str, str], *, url: bool = False) -> str:
    """Fill ``{{name}}`` placeholders. In a URL each value is percent-encoded."""

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in values:
            raise FlowError(f"no value for input {name!r}")
        return quote(values[name], safe="") if url else values[name]

    return PLACEHOLDER.sub(sub, text)


# ---------- files ----------


def flow_path(root: Path, domain: str, action: str) -> Path:
    return root / domain / f"{action}.json"


def load_flow(path: Path) -> Flow:
    try:
        return Flow.model_validate(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError) as e:
        raise FlowError(f"can't read flow {path}: {e}") from e
    except ValueError as e:
        raise FlowError(f"flow {path} is not valid: {e}") from e


def save_flow(flow: Flow, root: Path) -> Path:
    path = flow_path(root, flow.domain, flow.action)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_compact(flow.model_dump(mode="json", exclude_none=True)), indent=2) + "\n")
    return path


def _compact(value: Any) -> Any:
    """Drop empty locator args/kwargs so saved flows stay short to read."""
    if isinstance(value, dict):
        return {k: _compact(v) for k, v in value.items() if not (k in ("args", "kwargs") and not v)}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    return value


def list_flows(root: Path, domain: str | None = None) -> Iterator[Path]:
    dirs = [root / domain] if domain else sorted(p for p in root.glob("*") if p.is_dir())
    for d in dirs:
        yield from sorted(d.glob("*.json"))


# ---------- playing a flow ----------


def resolve(page: Any, target: Target) -> Any:
    """The Playwright locator a target names (``page`` is a Playwright Page)."""
    if isinstance(target, str):
        return page.locator(target)
    loc = page
    for call in target:
        attr = getattr(loc, call.method)
        loc = attr if call.method in PROPERTIES else attr(*call.args, **call.kwargs)
    return loc


StepHook = Callable[[int, Goto | Fill | Press | Click | Select | WaitFor], Awaitable[None] | None]


async def play(
    page: Any,
    flow: Flow,
    values: dict[str, str],
    *,
    on_step: StepHook | None = None,
    timeout_ms: int = 10_000,
) -> None:
    """Run every step of ``flow`` on ``page`` with ``values`` for its inputs. ``on_step(i, step)`` runs before
    step ``i`` (capture uses it to tag traffic). Raises FlowError naming the step that failed."""
    for i, step in enumerate(flow.steps):
        if on_step is not None:
            r = on_step(i, step)
            if r is not None:
                await r
        try:
            await _play_step(page, step, values, timeout_ms)
        except FlowError:
            raise
        except Exception as e:  # Playwright's TimeoutError and friends
            raise FlowError(
                f"step {i + 1} ({step.op}) failed: {type(e).__name__}: {str(e).splitlines()[0]}"
            ) from e


async def _play_step(page: Any, step: Any, values: dict[str, str], timeout_ms: int) -> None:
    if isinstance(step, Goto):
        await page.goto(render(step.url, values, url=True), timeout=timeout_ms * 3)
    elif isinstance(step, Fill):
        await resolve(page, step.target).fill(render(step.value, values), timeout=timeout_ms)
    elif isinstance(step, Press):
        if step.target is None:
            await page.keyboard.press(step.key)
        else:
            await resolve(page, step.target).press(step.key, timeout=timeout_ms)
    elif isinstance(step, Click):
        await resolve(page, step.target).click(timeout=timeout_ms)
    elif isinstance(step, Select):
        await resolve(page, step.target).select_option(render(step.value, values), timeout=timeout_ms)
    elif isinstance(step, WaitFor):
        if step.target is not None:
            await resolve(page, step.target).first.wait_for(state="visible", timeout=step.timeout_ms)
        if step.url is not None:
            await page.wait_for_url(render(step.url, values), timeout=step.timeout_ms)
        if step.load_state is not None:
            await page.wait_for_load_state(step.load_state, timeout=step.timeout_ms)


# ---------- playwright codegen → flow ----------


def from_codegen(
    source: str,
    *,
    action: str,
    inputs: dict[str, str] | None = None,
    examples: dict[str, list[str]] | None = None,
    no_marker: set[str] | None = None,
    domain: str | None = None,
    description: str | None = None,
    auth_provider: str | None = None,
    results_selector: Target | None = None,
    next_page: Target | None = None,
) -> Flow:
    """Convert a ``playwright codegen --target python`` (or python-async) script into a flow.

    ``inputs`` maps an input name to the literal value typed while recording (``{"query": "serde"}``): that
    value becomes ``{{query}}`` in fills, selects and goto URLs, and is its first example. A fill or select
    whose value isn't mapped becomes an input named after its field. ``examples`` adds more real values;
    ``no_marker`` names inputs that can't take a marker (selects never do, so they need a second example).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise FlowError(f"not a Python codegen script: {e}") from e
    literal_to_name = {v: k for k, v in (inputs or {}).items()}
    if len(literal_to_name) != len(inputs or {}):
        raise FlowError("two inputs were given the same recorded value; record with different values")
    no_marker = set(no_marker or ())
    found: dict[str, str] = {}  # input name → recorded value
    steps: list[Goto | Fill | Press | Click | Select | WaitFor] = []
    auto = 0

    def name_for(value: str, target: Target, select: bool) -> str:
        nonlocal auto
        if value in literal_to_name:
            name = literal_to_name[value]
        else:
            name = _slug(_target_label(target)) or f"input_{auto + 1}"
            auto += 1
            while name in found and found[name] != value:
                name += "_2"
        found[name] = value
        if select:
            no_marker.add(name)
        return name

    for node in ast.walk(tree):
        if isinstance(node, ast.With) and any(
            "expect_popup" in ast.unparse(i.context_expr) for i in node.items
        ):
            raise FlowError("the recording opens a popup or new tab; record the action in one tab")
    for call in _page_calls(tree):
        method, chain, args = call
        if method == "goto":
            steps.append(Goto(url=_str(args, 0, "goto")))
        elif method == "wait_for_url":
            steps.append(WaitFor(url=_str(args, 0, "wait_for_url")))
        elif method == "wait_for_load_state":
            steps.append(WaitFor(load_state=_str(args, 0, "wait_for_load_state") if args else "load"))  # type: ignore[arg-type]
        elif method in ("fill", "type", "press_sequentially"):
            target = _target(chain)
            value = _str(args, 0, method)
            steps.append(Fill(target=target, value="{{" + name_for(value, target, False) + "}}"))
        elif method == "press":
            if not chain:
                raise FlowError("page.press needs a selector; record the key press on a field")
            steps.append(Press(target=_target(chain), key=_str(args, 0, "press")))
        elif method == "keyboard.press":
            steps.append(Press(key=_str(args, 0, "keyboard.press")))
        elif method in ("click", "check", "uncheck", "dblclick", "tap"):
            if method == "dblclick":
                raise FlowError("double clicks aren't supported in flows")
            steps.append(Click(target=_target(chain)))
        elif method == "select_option":
            target = _target(chain)
            value = _str(args, 0, "select_option")
            steps.append(Select(target=target, value="{{" + name_for(value, target, True) + "}}"))
        elif method == "to_be_visible":
            steps.append(WaitFor(target=_target(chain)))
        elif method in ("close", "new_page", "new_context", "launch", "set_default_timeout", "storage_state"):
            continue
        else:
            raise FlowError(f"the recording uses {method}(), which flows don't support")
    if not steps:
        raise FlowError("the recording has no steps")
    first = steps[0]
    if not isinstance(first, Goto):
        raise FlowError("the recording doesn't start by opening a page")

    # Mapped values typed into the address bar (or carried in a goto URL) become placeholders there too.
    for step in steps:
        if isinstance(step, Goto):
            step.url = _template_url(step.url, literal_to_name, found)

    missing = set(inputs or {}) - set(found)
    if missing:
        raise FlowError(f"recorded values not found in the recording: {', '.join(sorted(missing))}")
    host = urlsplit(first.url).hostname or ""
    built: dict[str, Input] = {}
    for name, value in found.items():
        extra = [v for v in (examples or {}).get(name, []) if v != value]
        marker = name not in no_marker
        if not marker and not extra:
            raise FlowError(f"input {name!r} can't take a marker: give a second real value for it")
        built[name] = Input(marker=marker, examples=[value, *extra])
    try:
        return Flow(
            domain=domain or _registrable(host),
            action=action,
            description=description,
            auth_provider=auth_provider,
            inputs=built,
            steps=steps,
            results_selector=results_selector,
            next_page=next_page,
        )
    except ValueError as e:
        raise FlowError(f"the converted flow isn't valid: {e}") from e


def _page_calls(tree: ast.AST) -> Iterator[tuple[str, list[ast.Call | ast.Attribute], list[ast.expr]]]:
    """Every statement-level call on ``page`` (or ``expect(page...)``), in source order:
    (method, locator chain nodes, args)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        call = node.value.value if isinstance(node.value, ast.Await) else node.value
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        method = call.func.attr
        base = call.func.value
        if method == "press" and isinstance(base, ast.Attribute) and base.attr == "keyboard":
            if _root_is_page(base.value):
                yield "keyboard.press", [], list(call.args)
            continue
        if isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "expect":
            if base.args and _root_is_page(base.args[0]):
                yield method, _chain(base.args[0]), list(call.args)
            continue
        if _root_is_page(base):
            yield method, _chain(base), list(call.args)
    # ast.walk is breadth-first, but codegen's statements are siblings in one function body, so order holds.


def _root_is_page(node: ast.expr) -> bool:
    while isinstance(node, ast.Call | ast.Attribute):
        node = node.func if isinstance(node, ast.Call) else node.value
    return isinstance(node, ast.Name) and node.id == "page"


def _chain(node: ast.expr) -> list[ast.Call | ast.Attribute]:
    links: list[ast.Call | ast.Attribute] = []
    while isinstance(node, ast.Call | ast.Attribute):
        links.append(node)
        node = (
            node.func.value
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            else (node.value if isinstance(node, ast.Attribute) else node.func)
        )
    links.reverse()
    # A Call's func is an Attribute already in the chain; keep the Call, drop its Attribute.
    return [n for n in links if not (isinstance(n, ast.Attribute) and _is_called(n, links))]


def _is_called(attr: ast.Attribute, links: list[ast.Call | ast.Attribute]) -> bool:
    return any(isinstance(n, ast.Call) and n.func is attr for n in links)


def _target(chain: list[ast.Call | ast.Attribute]) -> list[LocatorCall]:
    if not chain:
        raise FlowError("an action without an element to act on")
    calls: list[LocatorCall] = []
    for node in chain:
        if isinstance(node, ast.Attribute):
            if node.attr not in PROPERTIES:
                raise FlowError(f"unsupported locator property .{node.attr}")
            calls.append(LocatorCall(method=node.attr))  # type: ignore[arg-type]
            continue
        assert isinstance(node.func, ast.Attribute)
        method = node.func.attr
        if method not in LOCATOR_METHODS:
            raise FlowError(f"unsupported locator method {method}()")
        try:
            args = [ast.literal_eval(a) for a in node.args]
            kwargs = {k.arg: ast.literal_eval(k.value) for k in node.keywords if k.arg}
        except ValueError as e:
            raise FlowError(
                f"{method}() has an argument that isn't a plain value (a regex or a nested locator)"
            ) from e
        calls.append(LocatorCall(method=method, args=args, kwargs=kwargs))  # type: ignore[arg-type]
    return calls


def _str(args: list[ast.expr], i: int, method: str) -> str:
    try:
        value = ast.literal_eval(args[i])
    except (IndexError, ValueError) as e:
        raise FlowError(f"{method}() needs a plain string") from e
    if not isinstance(value, str):
        raise FlowError(f"{method}() needs a plain string")
    return value


def _target_label(target: Target) -> str:
    if isinstance(target, str):
        return re.sub(r"[^A-Za-z0-9]+", " ", target).strip().split(" ")[-1] if target else ""
    for call in reversed(target):
        if call.method in (
            "get_by_label",
            "get_by_placeholder",
            "get_by_test_id",
            "get_by_title",
            "get_by_text",
        ):
            return str(call.args[0]) if call.args else ""
        if call.method == "get_by_role" and isinstance(call.kwargs.get("name"), str):
            return str(call.kwargs["name"])
        if call.method == "locator" and call.args:
            return _target_label(str(call.args[0]))
    return ""


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:4]
    slug = "_".join(words)
    return slug if slug and slug[0].isalpha() else ""


def _template_url(url: str, literal_to_name: dict[str, str], found: dict[str, str]) -> str:
    if "{{" in url:
        return url
    for literal, name in sorted(literal_to_name.items(), key=lambda kv: -len(kv[0])):
        for form in {literal, quote(literal, safe=""), quote(literal, safe="").replace("%20", "+")}:
            if form and form in url:
                url = url.replace(form, "{{" + name + "}}")
                found.setdefault(name, literal)
                break
    return url


def _registrable(host: str) -> str:
    return host[4:] if host.startswith("www.") else host
