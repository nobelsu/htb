"""flowmap as the gateway's mapper (DECISIONS M8): ``MAPPER=flowmap`` maps a site when someone asks for it.

A map job is the same pipeline as ``record --agent`` followed by ``map``, run inside the gateway:

1. Claude does the task once in installed Chrome (the agent recorder, reads only);
2. the flow is saved under FLOWS_DIR, so heal can replay it later;
3. the flow is captured (marker and real runs), inferred, labelled, minimised and replayed;
4. the card goes back to the runtime, which validates it, runs its examples and saves it.

A job with no action explores instead: Claude proposes read tasks, each goes through steps 1 to 3 into one
card and is kept only if it passes, then follow-ups that use values the first actions returned
(flowmap/explore.py).

When the site wants a sign-in the job stops with `SignInNeeded` (and the page that asked); the runtime gets
the requester signed in through Connect and runs the job again with ``request.sign_in`` set. Then every
browser here starts with their stored sign-in, the action gets that ``auth_provider``, and the card's connect
provider (its ``session_check`` and ``logged_out_signals``) is worked out from the mapped action
(flowmap/signin.py).

Raw captures are kept in a temporary folder and deleted when the job ends (decision 2): they can hold
whatever the site showed the requester.
"""

from __future__ import annotations

import logging
import tempfile
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flowmap.agent import AgentError, record_with_agent, settle, snapshot
from flowmap.assemble import AssembleError, map_action
from flowmap.browser import BrowserSetup, SignInMissing, new_page
from flowmap.capture import capture_flow, visible_without
from flowmap.explore import MAX_ACTIONS, MAX_DEPTH, Task, links, page_kind, propose_tasks, samples
from flowmap.flows import flow_path, load_flow, save_flow
from flowmap.infer import InferError, candidates, registrable, request_sig
from flowmap.minimise import executor_replayer
from flowmap.privacy import PrivacyReport, public_examples
from flowmap.signin import SignInCheckError, infer_sign_in, placeholder_provider
from runtime.executor import Executor
from runtime.mapping import MapError, MapOutcome, MapRequest, SignInNeeded
from schema import validate_card

log = logging.getLogger("flowmap.server")

NOBODY_TO_SIGN_IN = (
    "Nobody can sign in here. If the task can be done without signing in, carry on; otherwise call give_up."
)


class FlowMapper:
    def __init__(
        self,
        executor: Executor,
        flows_dir: str | Path = "flows",
        *,
        proxies: Any = None,
        channel: str = "chrome",
        pause_s: float = 1.0,
        client: Any = None,
        use_llm: bool | None = None,
    ) -> None:
        self.executor = executor  # the gateway's: its tiers replay the mapped action
        self.flows_dir = Path(flows_dir)
        self.proxies = proxies
        self.channel = channel
        self.pause_s = pause_s
        self.client = client  # an anthropic.AsyncAnthropic (tests pass a stand-in)
        self.use_llm = use_llm

    def domain_for(self, host: str) -> str:
        return registrable(host)

    def _executor_for(self, tier: str) -> Executor:
        """Only this tier, so a replay can't quietly climb the ladder; no healing hooks."""
        e = self.executor
        return Executor(e.sessions, e.confirmer, tiers={tier: e.tiers[tier]}, proxies=e.proxies)

    def _setup(self, request: MapRequest) -> BrowserSetup:
        origin = "{0.scheme}://{0.netloc}".format(urlsplit(request.url))
        if request.sign_in:
            return BrowserSetup.for_sign_in(
                self.executor.sessions,
                request.user,
                request.domain,
                request.sign_in,
                origin,
                proxies=self.proxies,
                channel=self.channel,
            )
        proxy = self.proxies.for_sign_in(request.user, request.domain, None, origin) if self.proxies else None
        return BrowserSetup(channel=self.channel, headless=True, proxy=proxy)

    async def map_site(self, request: MapRequest, progress: Callable[[str], None]) -> MapOutcome:
        """Map ``request.action``; with no action, explore the site (flowmap/explore.py)."""
        lines: list[str] = []

        def step(line: str) -> None:
            lines.append(line)
            progress(line)

        try:
            setup = self._setup(request)
        except SignInMissing as e:
            raise SignInNeeded(str(e), lines, request.login_url) from e
        if not request.action:
            return await self._explore(request, setup, step, lines)
        task = Task(request.action, request.task, request.url, dict(request.inputs))
        card, report = await self._map_task(task, request, setup, request.existing, step, lines)
        card, private = await self._public_examples(card, {request.action}, request, step)
        report["privacy"] = private.to_json()
        if request.action in private.dropped:
            raise MapError(f"{request.action} wasn't saved: {private.dropped[request.action]}", lines)
        if request.sign_in and not _has_sign_in(request.existing):
            report["sign_in"] = await self._add_sign_in(card, request, step, lines)
        return MapOutcome(card=card, report=report, log=lines)

    async def _map_task(
        self,
        task: Task,
        request: MapRequest,
        setup: BrowserSetup,
        existing: dict[str, Any] | None,
        step: Callable[[str], None],
        lines: list[str],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Record one task, capture and map it into ``existing`` (None: a new card). Returns the card and the
        action's report. Raises SignInNeeded or MapError."""
        t0 = time.monotonic()
        recording = await self._record(task, request, setup, step, lines)
        flow = recording.flow
        save_flow(flow, self.flows_dir)
        t_record = time.monotonic()
        step(f"recorded {task.action} in {recording.turns} turns; replaying it to see the site's requests")
        with tempfile.TemporaryDirectory(prefix="flowmap-") as captures:
            try:
                runs = await capture_flow(flow, root=Path(captures), setup=setup, pause_s=self.pause_s)
            except Exception as e:
                raise MapError(f"replaying the recording failed: {type(e).__name__}", lines) from e
            failed = [r.error for r in runs if r.error]
            if failed and len(failed) == len(runs):
                raise MapError(f"every replay of the recording failed: {failed[0]}", lines)
            prefer = await self._confirm_source(flow, runs, setup, step) if not task.write else None
            step(f"working out {task.action} from the requests")
            try:
                mapped = await map_action(
                    flow,
                    runs,
                    existing=existing,
                    replay=executor_replayer(self._executor_for, request.user),
                    llm_client=self.client,
                    use_llm=self.use_llm,
                    pause_s=self.pause_s,
                    providers=_providers(request) if existing is None else None,
                    prefer=prefer,
                )
            except (InferError, AssembleError) as e:
                raise MapError(f"{e} ({'; '.join(e.reasons[:3])})" if e.reasons else str(e), lines) from e
        report = mapped.report()
        report["ms"] = {
            "record": int((t_record - t0) * 1000),
            "map": int((time.monotonic() - t_record) * 1000),
        }
        report["turns"] = recording.turns
        step(f"mapped {task.action} (confidence {report['confidence']})")
        return mapped.final.card, report

    async def _confirm_source(
        self, flow: Any, runs: list[Any], setup: BrowserSetup, step: Callable[[str], None]
    ) -> tuple[str, str, str] | None:
        """The blocking test: replay the task with each likely request blocked; the one whose absence makes
        the results disappear is where they come from (a tracking ping or a live update isn't). Only when
        there's a choice to make, and only for reads that showed results. A document (the page itself) is
        tested last: blocking it empties any page."""
        if flow.results_selector is None or not any(r.kind == "real" and r.visible for r in runs):
            return None
        tops = candidates(flow, runs)
        if len(tops) < 2:
            return None
        values = [v for v in flow.example_values(0).values() if v]
        documents = {
            request_sig(ex.method, ex.url, values)
            for r in runs
            for ex in r.exchanges
            if ex.resource_type == "document"
        }
        ordered = [t for t in tops if t not in documents] + [t for t in tops if t in documents]
        for sig in ordered:
            if sig in documents:
                step(f"none of the others carries the results: it's the page itself ({sig[2]})")
                return sig
            step(f"blocking {sig[0]} {sig[2]} to see whether the results still appear")
            try:
                shown = await visible_without(flow, _matching(sig, values), setup=setup)
            except Exception as e:
                log.info("blocking test failed: %s", type(e).__name__)
                return None
            if not shown:
                step(f"confirmed: the results come from {sig[0]} {sig[2]}")
                return sig
        return None

    async def _record(
        self,
        task: Task,
        request: MapRequest,
        setup: BrowserSetup,
        step: Callable[[str], None],
        lines: list[str],
    ) -> Any:
        from playwright.async_api import async_playwright

        wanted_sign_in: list[str] = []  # the page that asked for it
        page: Any = None

        async def sign_in() -> None:
            wanted_sign_in.append(page.url if page is not None else task.url)
            raise AgentError(NOBODY_TO_SIGN_IN)

        step(f"opening {task.url}" + (" signed in" if request.sign_in else ""))
        try:
            async with async_playwright() as p:
                browser = await setup.launch(p)
                try:
                    page = await new_page(await browser.new_context())
                    return await record_with_agent(
                        page,
                        url=task.url,
                        task=task.task,
                        action=task.action,
                        inputs=task.inputs,
                        domain=request.domain,
                        description=task.task,
                        auth_provider=request.sign_in,
                        write=task.write,
                        client=self.client,
                        sign_in=sign_in,
                        on_step=step,
                    )
                finally:
                    await browser.close()
        except AgentError as e:
            if wanted_sign_in:
                login_url = _login_page(wanted_sign_in[0], request)
                if request.sign_in:
                    message = f"{request.domain} still wants a sign-in after signing in"
                else:
                    message = f"{request.domain} wants a sign-in for this"
                raise SignInNeeded(message, lines, login_url) from e
            raise MapError(str(e), lines) from e

    async def _add_sign_in(
        self, card: dict[str, Any], request: MapRequest, step: Callable[[str], None], lines: list[str]
    ) -> dict[str, Any]:
        """Give a card mapped while signed in its real connect provider (flowmap/signin.py)."""
        assert request.sign_in
        step("working out how to check the sign-in")
        try:
            check = await infer_sign_in(
                card,
                request.sign_in,
                login_url=request.login_url or request.url,
                start_url=request.url,
                user=request.user,
                executor=self.executor,
            )
        except SignInCheckError as e:
            raise MapError(f"couldn't find a way to check the sign-in: {e}", lines) from e
        card["connect_providers"] = [check.provider]
        card.setdefault("defaults", {})["tier"] = check.tier  # session_check runs on the default tier
        return check.report()

    # ---- explore ---------------------------------------------------------------

    async def _explore(
        self, request: MapRequest, setup: BrowserSetup, step: Callable[[str], None], lines: list[str]
    ) -> MapOutcome:
        """A crawl over kinds of page. Claude looks at the start page and proposes read tasks; each is mapped
        into one card and kept only if it passes. The kept actions' results link to new kinds of pages (a
        search links to profiles; a profile to posts; a post to its comments): one page of each new kind is
        looked at and its reads proposed and mapped the same way, up to ``MAX_DEPTH`` links from the start
        and ``MAX_ACTIONS`` new actions. A task that wants a sign-in stops the whole job (it explores again
        once the requester has signed in). Adding to a card: what it has is never re-mapped."""
        t0 = time.monotonic()
        card: dict[str, Any] | None = request.existing
        had = {a["name"] for a in card["actions"]} if card is not None else set()
        tried: list[dict[str, Any]] = []
        frontier: deque[tuple[str, int, str | None]] = deque([(request.url, 0, None)])
        kinds: set[str] = set()
        added = 0
        no_links: list[str] = []  # kept actions whose results carry ids, not links (a JSON API's)
        while frontier and added < MAX_ACTIONS:
            url, depth, via = frontier.popleft()
            kind = page_kind(url)
            if kind in kinds:
                continue
            kinds.add(kind)
            step(f"looking at {url}" + (f" (from {via})" if via else ""))
            try:
                page = await self._look(url, setup)
            except Exception as e:
                if via is None:
                    raise MapError(f"couldn't open {url}: {type(e).__name__}", lines) from e
                continue
            taken = had | {a["name"] for a in (card or {}).get("actions", [])} | {t["action"] for t in tried}
            tasks = await self._propose(request, page=page, lines=lines, start=url, via=via, taken=taken)
            if tasks:
                step(f"depth {depth}: " + ", ".join(t.action for t in tasks))
            for task in tasks:
                if added >= MAX_ACTIONS:
                    break
                entry: dict[str, Any] = {"action": task.action, "task": task.task, "depth": depth}
                before = card
                try:
                    card, mapped_report = await self._map_task(task, request, setup, card, step, lines)
                except SignInNeeded:
                    raise
                except MapError as e:
                    entry.update(kept=False, reason=str(e))
                    step(f"dropped {task.action}: {e}")
                    tried.append(entry)
                    continue
                same = _same_requests(card, task.action)
                if same:
                    card = before
                    entry.update(kept=False, reason=f"it sends the same requests as {same}")
                    step(f"dropped {task.action}: the same requests as {same}")
                    tried.append(entry)
                    continue
                added += 1
                entry.update(kept=True, confidence=mapped_report["confidence"], tier=mapped_report["tier"])
                tried.append(entry)
                if depth < MAX_DEPTH and not task.write:
                    found = await self._links(card, task.action, request)
                    if not found:
                        no_links.append(task.action)
                    for link in found:
                        if page_kind(link) not in kinds:
                            frontier.append((link, depth + 1, task.action))
        if card is not None and no_links and added < MAX_ACTIONS:
            # Results without links: follow-ups that take a value copied from what they returned instead.
            mapped = await self._samples(card, request.user, set(no_links))
            taken = had | {a["name"] for a in card["actions"]} | {t["action"] for t in tried}
            tasks = await self._propose(
                request, page="", lines=lines, start=request.url, mapped=mapped, taken=taken
            )
            for task in tasks if mapped else []:
                if added >= MAX_ACTIONS:
                    break
                entry = {"action": task.action, "task": task.task, "depth": 1}
                before = card
                try:
                    card, mapped_report = await self._map_task(task, request, setup, card, step, lines)
                except MapError as e:
                    entry.update(kept=False, reason=str(e))
                    tried.append(entry)
                    continue
                same = _same_requests(card, task.action)
                if same:
                    card = before
                    entry.update(kept=False, reason=f"it sends the same requests as {same}")
                else:
                    added += 1
                    entry.update(
                        kept=True, confidence=mapped_report["confidence"], tier=mapped_report["tier"]
                    )
                tried.append(entry)
        privacy: dict[str, Any] = {}
        if card is not None:
            new = {a["name"] for a in card["actions"]} - had
            card, private = await self._public_examples(card, new, request, step)
            privacy = private.to_json()
            for t in tried:
                if t["action"] in private.dropped:
                    t.update(kept=False, reason=private.dropped[t["action"]])
        if card is None or {a["name"] for a in card["actions"]} == had:
            reasons = "; ".join(f"{t['action']}: {t.get('reason')}" for t in tried) or "nothing new proposed"
            raise MapError(f"none of the explored actions could be mapped ({reasons})", lines)
        report: dict[str, Any] = {
            "explored": tried,
            "pages": sorted(kinds),
            "privacy": privacy,
            "actions": [a["name"] for a in card["actions"]],
            "ms": int((time.monotonic() - t0) * 1000),
        }
        if request.sign_in and not _has_sign_in(request.existing):
            report["sign_in"] = await self._add_sign_in(card, request, step, lines)
        step(f"explored {request.domain}: kept {', '.join(a for a in report['actions'] if a not in had)}")
        return MapOutcome(card=card, report=report, log=lines)

    async def _public_examples(
        self, card: dict[str, Any], names: set[str], request: MapRequest, step: Callable[[str], None]
    ) -> tuple[dict[str, Any], PrivacyReport]:
        """The new actions' examples made public before the card is shared (flowmap/privacy.py)."""
        step("checking the examples hold no personal data")
        card, report = await public_examples(
            card, names, user=request.user, executor=self.executor, client=self.client, use_llm=self.use_llm
        )
        for name, values in report.replaced.items():  # heal replays the saved recording: same values there
            path = flow_path(self.flows_dir, request.domain, name)
            if path.exists():
                flow = load_flow(path)
                for param, value in values.items():
                    if param in flow.inputs:
                        flow.inputs[param].examples = [value]
                save_flow(flow, self.flows_dir)
        for name in report.dropped:
            flow_path(self.flows_dir, request.domain, name).unlink(missing_ok=True)
        return card, report

    async def _samples(self, card: dict[str, Any], user: str, only: set[str]) -> list[dict[str, Any]]:
        """Some of what the ``only`` actions return (their first example run through the executor), for
        follow-ups that take a value from it."""
        report = validate_card(card)
        if not report.ok or report.card is None:
            return []
        out = []
        for action in card["actions"]:
            if action["name"] not in only or action["kind"] != "read" or not action.get("examples"):
                continue
            params = dict(action["examples"][0]["params"])
            env = await self.executor.run_internal(report.card, 0, action["name"], params, user)
            if env.data:
                out.append(samples(action, list(env.data)))
        return out

    async def _links(self, card: dict[str, Any], name: str, request: MapRequest) -> list[str]:
        """The pages on this site that ``name``'s results link to, one per kind (its first example's run)."""
        report = validate_card(card)
        action = next((a for a in card["actions"] if a["name"] == name), None)
        if not report.ok or report.card is None or action is None or not action.get("examples"):
            return []
        params = dict(action["examples"][0]["params"])
        env = await self.executor.run_internal(report.card, 0, name, params, request.user)
        site = registrable(urlsplit(card["base_url"]).hostname or "")
        return links(list(env.data or []), card["base_url"], site)

    async def _look(self, url: str, setup: BrowserSetup) -> str:
        """The start page as an accessibility tree, for the first round of proposals."""
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await setup.launch(p)
            try:
                page = await new_page(await browser.new_context())
                await page.goto(url, timeout=30_000)
                await settle(page)
                return await snapshot(page)
            finally:
                await browser.close()

    async def _propose(
        self,
        request: MapRequest,
        *,
        page: str,
        lines: list[str],
        start: str,
        via: str | None = None,
        taken: set[str] | None = None,
        mapped: list[dict[str, Any]] | None = None,
    ) -> list[Task]:
        try:
            return await propose_tasks(
                self.client,
                site=request.domain,
                start_url=start,
                page=page,
                goal=request.task,
                taken=taken,
                via=via,
                mapped=mapped,
            )
        except AgentError as e:
            if via or mapped:
                return []  # nothing for this page or these results: the explore goes on
            raise MapError(str(e), lines) from e


def _matching(sig: tuple[str, str, str], values: list[str]) -> Callable[[str, str], bool]:
    """Does a live request (method, url) belong to the candidate ``sig``?"""

    def match(method: str, url: str) -> bool:
        return request_sig(method, url, values) == sig

    return match


def _same_requests(card: dict[str, Any], name: str) -> str | None:
    """Another action of ``card`` that sends exactly the requests ``name`` does (method, URL and query names):
    the same action under another name, e.g. "list books by author" mapped onto the plain search."""

    def shape(action: dict[str, Any]) -> list[tuple[str, str, tuple[str, ...]]]:
        return [
            (s["request"]["method"], s["request"]["url"], tuple(sorted(s["request"].get("query") or {})))
            for s in action["steps"]
        ]

    new = next(a for a in card["actions"] if a["name"] == name)
    return next((a["name"] for a in card["actions"] if a["name"] != name and shape(a) == shape(new)), None)


def _has_sign_in(card: dict[str, Any] | None) -> bool:
    """The card already says how to check a sign-in (adding to it keeps that)."""
    return bool(card and card.get("connect_providers"))


def _providers(request: MapRequest) -> list[dict[str, Any]] | None:
    """A signed-in card's provider while it is mapped; its real check is worked out afterwards."""
    if not request.sign_in:
        return None
    return [placeholder_provider(request.sign_in, request.login_url or request.url, request.url)]


def _login_page(asked: str, request: MapRequest) -> str:
    """The page that asked for a sign-in, if it's on the site (else the start page): Connect opens it."""
    host = urlsplit(asked).hostname or ""
    if (
        urlsplit(asked).scheme in ("http", "https")
        and host
        and registrable(host) == registrable(urlsplit(request.url).hostname or "")
    ):
        return asked
    return request.url
