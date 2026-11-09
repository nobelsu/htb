"""Capture: replay a flow in Playwright and record what the site sent and showed.

One capture session is several runs of the same flow:
- **marker runs** (two, when the flow has marker inputs): each marker input gets a fresh unlikely string, so
  inference can find where it travels; comparing the two runs separates templated values from constants;
- **real runs** (at least two): the inputs' real example values, for matching outputs to what the page shows,
  for page 2 (``next_page``) and for the card's examples.

Every xhr, fetch and document request is recorded in full (method, URL, headers, body, status, response
headers and text body) and tagged with the flow step that was running when it started. The visible text of
``results_selector`` is recorded after the flow, and again on page 2.

Live session values never reach disk: every cookie value and every auth/csrf/token header value is
replaced, wherever it appears (URLs, headers, bodies), by ``secret_hash(value)``. The same value always gets
the same hash, so inference can still see that a header carries a cookie's value. Saved to
``captures/{domain}/{action}/{run_id}.json`` (0600; ``captures/`` is gitignored).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from spider.flowmap.record.browser import BrowserSetup, new_page
from spider.flowmap.record.flows import Click, Flow, FlowError, play, resolve
from spider.flowmap.record.holdback import WRITES, HoldBack

RECORDED_TYPES = {"xhr", "fetch", "document"}
TEXT_TYPES = re.compile(r"json|text|javascript|xml|html|x-www-form-urlencoded", re.I)
MAX_BODY = 2_000_000
# Copied from harmap/helpers/checks.py (SENSITIVE_NAME, MIN_SECRET_LEN), see DECISIONS M3.
SENSITIVE_NAME = re.compile(r"cookie|csrf|xsrf|auth|token|session|secret|password|api[-_]?key|bearer", re.I)
MIN_SECRET_LEN = 8
SECRET = re.compile(r"secret_[0-9a-f]{16}")
_LETTERS = "bcdfghjklmnpqrstvwxz"


def new_marker() -> str:
    """A fresh, unlikely string like ``qzvx4821``. Copied from harmap/capture/replay.py (M3)."""
    return "".join(secrets.choice(_LETTERS) for _ in range(4)) + f"{secrets.randbelow(10**4):04d}"


def secret_hash(value: str) -> str:
    """What a live session value is replaced by in a capture."""
    return "secret_" + hashlib.sha256(value.encode()).hexdigest()[:16]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Exchange(_Model):
    id: int
    step: int  # the flow step running when the request started; len(flow.steps) for the next-page click
    phase: Literal["flow", "next_page"] = "flow"
    resource_type: str
    method: str
    url: str
    request_headers: list[tuple[str, str]] = []
    post_data: str | None = None
    status: int | None = None
    response_headers: list[tuple[str, str]] = []
    body: str | None = None
    body_truncated: bool = False
    redirected_from: int | None = None
    failure: str | None = None
    started_ms: float = 0.0
    finished_ms: float | None = None


class CookieRecord(_Model):
    name: str
    value: str  # hashed
    domain: str
    path: str = "/"


class Run(_Model):
    run_id: str
    session: str  # every run of one capture session shares it
    domain: str
    action: str
    kind: Literal["marker", "real"]
    index: int
    values: dict[str, str]  # what was typed for each input
    markers: dict[str, str] = {}  # input → marker, for the marker inputs of a marker run
    started_at: str
    final_url: str = ""
    exchanges: list[Exchange] = []
    cookies: list[CookieRecord] = []
    visible: list[str] = []
    visible_page2: list[str] | None = None
    secrets_hashed: int = 0
    error: str | None = None  # the flow failed at a step; what was recorded until then is kept


# ---------- secrets ----------


# A cookie whose value is just a host name ("www.linkedin.com") is no secret, and hashing it would also hash
# the site's own address out of every URL (nothing would look same-site any more).
HOSTNAME = re.compile(r"^\.?(?:[a-z0-9-]+\.)+[a-z]{2,}$", re.I)


def secret_values(cookies: Iterable[str], headers: Iterable[tuple[str, str]]) -> set[str]:
    """Live values to hash: cookie values (quotes stripped, so a header carrying the unquoted value hashes the
    same) and sensitive header values (and the credential after an auth scheme like ``Bearer``).
    Adapted from harmap/helpers/checks.py ``secret_values`` (M3)."""
    out: set[str] = set()
    for value in cookies:
        v = value.strip('"')
        if len(v) >= MIN_SECRET_LEN and not HOSTNAME.match(v):
            out.add(v)
    for name, value in headers:
        if name.lower() in ("cookie", "set-cookie") or not SENSITIVE_NAME.search(name):
            continue  # cookie headers are covered by the cookie values themselves
        for v in (value, value.split(" ", 1)[-1]):
            v = v.strip().strip('"')
            if len(v) >= MIN_SECRET_LEN and not HOSTNAME.match(v):
                out.add(v)
    return out


def cookie_header_values(headers: Iterable[tuple[str, str]]) -> list[str]:
    """Cookie values in Cookie and Set-Cookie headers."""
    out = []
    for name, value in headers:
        low = name.lower()
        if low == "cookie":
            out += [p.split("=", 1)[1] for p in value.split(";") if "=" in p]
        elif low == "set-cookie":
            for line in value.split("\n"):
                first = line.split(";", 1)[0]
                if "=" in first:
                    out.append(first.split("=", 1)[1])
    return out


def scrub(text: str, values: Iterable[str]) -> str:
    for v in sorted(values, key=len, reverse=True):
        if v in text:
            text = text.replace(v, secret_hash(v))
    return text


def scrub_run(run: Run, values: set[str]) -> Run:
    """Replace every live value in the run, everywhere, by its hash."""
    raw = run.model_dump_json()
    for v in sorted(values, key=len, reverse=True):
        encoded = json.dumps(v)[1:-1]  # as it appears inside the JSON text
        raw = raw.replace(encoded, secret_hash(v))
    scrubbed = Run.model_validate_json(raw)
    scrubbed.secrets_hashed = len(values)
    return scrubbed


# ---------- recording ----------


class _Recorder:
    def __init__(self, page: Any, t0: float) -> None:
        self.t0 = t0
        self.step = 0
        self.phase: Literal["flow", "next_page"] = "flow"
        self.exchanges: list[Exchange] = []
        self.ids: dict[Any, int] = {}
        self.pending: set[asyncio.Task[None]] = set()
        self.inflight: set[Any] = set()
        self.last_activity = time.monotonic()
        page.on("request", self._on_request)
        page.on("requestfinished", lambda r: self._finish(r, None))
        page.on("requestfailed", lambda r: self._finish(r, r.failure or "failed"))

    def _ms(self) -> float:
        return round((time.monotonic() - self.t0) * 1000, 1)

    def _on_request(self, request: Any) -> None:
        if request.resource_type not in RECORDED_TYPES:
            return
        self.last_activity = time.monotonic()
        prev = request.redirected_from
        ex = Exchange(
            id=len(self.exchanges),
            step=self.step,
            phase=self.phase,
            resource_type=request.resource_type,
            method=request.method,
            url=request.url,
            post_data=_post_data(request),
            redirected_from=self.ids.get(prev) if prev is not None else None,
            started_ms=self._ms(),
        )
        self.ids[request] = ex.id
        self.exchanges.append(ex)
        self.inflight.add(request)

    def _finish(self, request: Any, failure: str | None) -> None:
        if request not in self.ids:
            return
        task = asyncio.ensure_future(self._complete(request, failure))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def _complete(self, request: Any, failure: str | None) -> None:
        ex = self.exchanges[self.ids[request]]
        try:
            ex.request_headers = [(h["name"], h["value"]) for h in await request.headers_array()]
            response = await request.response()
            if response is not None:
                ex.status = response.status
                ex.response_headers = [(h["name"], h["value"]) for h in await response.headers_array()]
                ctype = await response.header_value("content-type") or ""
                if failure is None and TEXT_TYPES.search(ctype) and not 300 <= response.status < 400:
                    try:
                        data = await response.body()
                        ex.body_truncated = len(data) > MAX_BODY
                        ex.body = data[:MAX_BODY].decode("utf-8", "replace")
                    except Exception:  # body gone (navigation), or never sent
                        ex.body = None
        except Exception as e:  # page closed mid-request
            failure = failure or f"{type(e).__name__}"
        ex.failure = failure
        ex.finished_ms = self._ms()
        self.inflight.discard(request)
        self.last_activity = time.monotonic()

    async def settle(self, quiet_ms: int = 700, max_ms: int = 8000) -> None:
        """Wait until no recorded request is in flight and nothing new started for ``quiet_ms``, or ``max_ms``
        passed (long-polls never go quiet)."""
        deadline = time.monotonic() + max_ms / 1000
        while time.monotonic() < deadline:
            quiet = time.monotonic() - self.last_activity >= quiet_ms / 1000
            if quiet and not self.inflight and not self.pending:
                return
            await asyncio.sleep(0.05)

    async def drain(self) -> None:
        if self.pending:
            await asyncio.gather(*list(self.pending), return_exceptions=True)


def _post_data(request: Any) -> str | None:
    try:
        data = request.post_data
    except Exception:  # binary body
        return None
    return data if data is None or len(data) <= MAX_BODY else data[:MAX_BODY]


def _normalise(text: str) -> str:
    return " ".join(text.split())


async def _visible(page: Any, flow: Flow, wait_ms: int) -> list[str]:
    if flow.results_selector is None:
        return []
    loc = resolve(page, flow.results_selector)
    try:
        await loc.first.wait_for(state="visible", timeout=wait_ms)
    except Exception:
        return []  # nothing shown (a marker search finds nothing)
    return [t for t in (_normalise(s) for s in await loc.all_inner_texts()) if t]


async def capture_run(
    context: Any,
    flow: Flow,
    *,
    kind: Literal["marker", "real"],
    index: int,
    values: dict[str, str],
    markers: dict[str, str],
    session: str,
    timeout_ms: int = 10_000,
    hold_back: bool = False,
    block: Callable[[str, str], bool] | None = None,
) -> Run:
    """Play ``flow`` once in a new page of ``context`` (a Playwright BrowserContext) and record it.
    ``hold_back``: writes never reach the site (flowmap/record/holdback.py), armed from ``commit_step``."""
    hold = HoldBack() if hold_back else None
    if hold is not None:
        await hold.install(context)
    if block is not None:  # the blocking test: does the page still show its results without this request?

        async def blocked(route: Any, request: Any) -> None:
            if block(request.method, request.url):
                await route.abort()
            else:
                await route.fallback()

        await context.route("**/*", blocked)
    commit = commit_step(flow)
    page = await new_page(context)
    t0 = time.monotonic()
    rec = _Recorder(page, t0)
    started = datetime.now(UTC)
    run = Run(
        run_id=f"{session}-{kind}{index}",
        session=session,
        domain=flow.domain,
        action=flow.action,
        kind=kind,
        index=index,
        values=values,
        markers=markers,
        started_at=started.isoformat(timespec="seconds"),
    )

    def on_step(i: int, _step: object) -> None:
        rec.step = i
        if hold is not None and i >= commit:
            hold.armed = True

    try:
        await play(page, flow, values, on_step=on_step, timeout_ms=timeout_ms)
        await rec.settle()
        # A marker search finds nothing, so don't wait long for results there.
        run.visible = await _visible(page, flow, timeout_ms if kind == "real" else 500)
        if kind == "real" and flow.next_page is not None and run.visible:
            nxt = resolve(page, flow.next_page)
            if await nxt.count() and await nxt.first.is_visible():  # no next link: one page of results
                rec.step, rec.phase = len(flow.steps), "next_page"
                before = run.visible
                await nxt.first.click(timeout=timeout_ms)
                await rec.settle()
                run.visible_page2 = await _wait_changed(page, flow, before, timeout_ms)
    except FlowError as e:
        if hold is not None and hold.held and rec.step > commit:
            pass  # waiting for the result of a write that was held back: the page never gets one
        else:
            run.error = str(e)
    except Exception as e:  # the next-page click, or the page went away
        run.error = f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
    await rec.drain()
    run.final_url = page.url
    run.exchanges = rec.exchanges
    jar = await context.cookies()
    run.cookies = [
        CookieRecord(name=c["name"], value=c["value"], domain=c["domain"], path=c["path"]) for c in jar
    ]
    await page.close()
    live = secret_values(
        [c.value for c in run.cookies]
        + [v for ex in run.exchanges for v in cookie_header_values(ex.request_headers + ex.response_headers)],
        [h for ex in run.exchanges for h in ex.request_headers],
    )
    return scrub_run(run, live)


def commit_step(flow: Any) -> int:
    """Where a write's replay starts holding everything back: the first click that reads like a change
    ("Follow", "Send", "Add to cart"), or the last click if none does. The earliest, not the last: a
    recording can click the change more than once (a first try, then the same from a search), and every
    one of those must be held back."""
    clicks = [i for i, s in enumerate(flow.steps) if isinstance(s, Click)]
    changes = [i for i in clicks if WRITES.search(_label(flow.steps[i].target))]
    return min(changes[:1] + clicks[-1:], default=len(flow.steps))


def _label(target: Any) -> str:
    """A click target as text: a selector, or its locator calls' arguments."""
    if isinstance(target, str):
        return target.replace("_", " ")
    return " ".join(json.dumps(c.model_dump(mode="json")) for c in target).replace("_", " ")


async def _wait_changed(page: Any, flow: Flow, before: list[str], timeout_ms: int) -> list[str]:
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        now = await _visible(page, flow, 500)
        if now != before or time.monotonic() > deadline:
            return now
        await asyncio.sleep(0.1)


RunKind = Literal["marker", "real"]


def run_plan(
    flow: Flow, marker_runs: int = 2, real_runs: int | None = None
) -> list[tuple[RunKind, dict[str, str], dict[str, str]]]:
    """(kind, values, markers) for every run of a capture session."""
    plan: list[tuple[RunKind, dict[str, str], dict[str, str]]] = []
    if any(inp.marker for inp in flow.inputs.values()):
        for i in range(marker_runs):
            markers = {name: new_marker() for name, inp in flow.inputs.items() if inp.marker}
            values = {**flow.example_values(i), **markers}
            plan.append(("marker", values, markers))
    most = max((len(inp.examples) for inp in flow.inputs.values()), default=1)
    for i in range(real_runs if real_runs is not None else max(2, most)):
        plan.append(("real", flow.example_values(i), {}))
    return plan


async def capture_flow(
    flow: Flow,
    *,
    root: Path | None = None,
    setup: BrowserSetup | None = None,
    headless: bool = True,
    profile_dir: Path | None = None,
    storage_state: Path | None = None,
    marker_runs: int = 2,
    real_runs: int | None = None,
    pause_s: float = 2.0,
    timeout_ms: int = 10_000,
    hold_back: bool | None = None,
) -> list[Run]:
    """Run the whole capture session for ``flow``; save each run under ``root`` when given.

    ``setup`` is the browser to use (flowmap/record/browser.py: installed Chrome, a stored sign-in, its device
    identity and proxy). Without one, Playwright's Chromium is used with ``headless``, ``profile_dir`` (a
    persistent profile: a login done once stays) or ``storage_state`` (saved cookies). ``pause_s`` waits
    between runs, to stay polite to real sites.
    """
    from playwright.async_api import async_playwright

    if setup is None:
        setup = BrowserSetup(
            channel="chromium", headless=headless, profile_dir=profile_dir, storage_state=storage_state
        )
    session = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(2)
    runs: list[Run] = []
    async with async_playwright() as p:
        browser = await setup.launch(p)
        try:
            for i, (kind, values, markers) in enumerate(run_plan(flow, marker_runs, real_runs)):
                if i and pause_s:
                    await asyncio.sleep(pause_s)
                n = sum(1 for r in runs if r.kind == kind)
                # A fresh context per run (unless a persistent profile is used): cookies the site sets differ
                # between runs, so inference can tell where they come from.
                context = await browser.new_context()
                try:
                    run = await capture_run(
                        context,
                        flow,
                        kind=kind,
                        index=n,
                        values=values,
                        markers=markers,
                        session=session,
                        timeout_ms=timeout_ms,
                        hold_back=flow.kind == "write" if hold_back is None else hold_back,
                    )
                finally:
                    if browser.shared is None:
                        await context.close()
                runs.append(run)
                if root is not None:
                    save_run(run, root)
        finally:
            await browser.close()
    return runs


async def visible_without(
    flow: Flow,
    block: Callable[[str, str], bool],
    *,
    setup: BrowserSetup | None = None,
    timeout_ms: int = 10_000,
) -> list[str]:
    """The results the page shows for the first real example with the requests ``block`` matches blocked:
    none means one of them is where the results come from."""
    from playwright.async_api import async_playwright

    setup = setup or BrowserSetup(channel="chromium", headless=True)
    async with async_playwright() as p:
        browser = await setup.launch(p)
        try:
            context = await browser.new_context()
            run = await capture_run(
                context,
                flow,
                kind="real",
                index=0,
                values=flow.example_values(0),
                markers={},
                session="blocking",
                timeout_ms=timeout_ms,
                hold_back=flow.kind == "write",
                block=block,
            )
        finally:
            await browser.close()
    return run.visible


# ---------- files ----------


def run_path(root: Path, run: Run) -> Path:
    return root / run.domain / run.action / f"{run.run_id}.json"


def save_run(run: Run, root: Path) -> Path:
    path = run_path(root, run)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(run.model_dump_json(indent=1))
    return path


def load_runs(root: Path, domain: str, action: str, session: str | None = None) -> list[Run]:
    """The runs of one capture session (the latest unless ``session`` is given), marker runs first."""
    runs = [Run.model_validate_json(p.read_text()) for p in sorted((root / domain / action).glob("*.json"))]
    if not runs:
        return []
    chosen = session or max(r.session for r in runs)
    picked = [r for r in runs if r.session == chosen]
    return sorted(picked, key=lambda r: (r.kind != "marker", r.index))
