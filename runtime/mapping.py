"""Map jobs (DECISIONS M8): any signed-up user can ask for a site to be mapped, and the gateway maps it.

Only when the site has no card yet; when it has one and an action is broken, the request starts the heal
instead. Jobs run inside the gateway process, one at a time, apart from heal jobs. The mapper plugs in through
`Mapper` (flowmap's is `flowmap.server.FlowMapper`); the runtime checks the card it returns (validation, every
read example run through the executor, and its sign-in check with the requester's sign-in) before saving it as
the site's first version.

A site that wants a sign-in: the job stops at ``needs_sign_in`` and its status carries a Connect link for the
requester. Once they've signed in (``ConnectionManager.on_connected``) the job is queued again and maps signed
in with their stored sign-in, under the provider id `provider_for` gives the site.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from runtime.connect import ConnectionManager
from runtime.executor import Executor
from runtime.healing import HealManager, example_passes
from runtime.registry import Registry
from runtime.state import MapJob, StateStore
from schema import Card, validate_card

log = logging.getLogger("runtime.mapping")

ACTION_NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
MAX_TASK = 500
MAX_INPUTS = 8
MAX_WAIT_S = 45.0
# Wording that does something on the site (the runtime's own copy: the mapper is pluggable). A task like this
# with no action name is one specific write, not a site to explore: the caller is asked to name it.
CHANGE = re.compile(
    r"\b(follow|connect|send|post|publish|like|subscribe|message|invite|endorse|apply|reply|comment|share"
    r"|repost|add to (?:cart|basket|bag)|save|bookmark|join|accept|vote|rate|review)\b",
    re.I,
)
READING = re.compile(r"\s*(search|find|look up|list|show|open|view|get|browse|see|read|check)\b", re.I)


class MapError(Exception):
    """Mapping failed. ``log`` holds the steps taken, for the requester."""

    def __init__(self, message: str, log_lines: list[str] | None = None) -> None:
        super().__init__(message)
        self.log = log_lines or []


class SignInNeeded(MapError):
    """The site wants a sign-in before the task can be done. ``login_url`` is the page that asked for it."""

    def __init__(
        self, message: str, log_lines: list[str] | None = None, login_url: str | None = None
    ) -> None:
        super().__init__(message, log_lines)
        self.login_url = login_url


@dataclass(frozen=True)
class MapRequest:
    job_id: str
    domain: str
    url: str
    task: str
    action: str
    inputs: dict[str, str]
    user: str
    # Set when the requester has signed in for this job: map signed in with their stored sign-in under this
    # provider id, and give the card a connect provider of that id whose login page is ``login_url``.
    sign_in: str | None = None
    login_url: str | None = None
    # The site's card when it has one: add to it (an action it lacks, or what an explore finds), never
    # re-map what's there.
    existing: dict[str, Any] | None = None
    # A named write (send, follow, post...): recorded with the committing request held back, so nothing
    # reaches the site while mapping; saved marked never run until a confirmed call succeeds.
    write: bool = False


def provider_for(domain: str) -> str:
    """The connect provider id of a site mapped while signed in: its name (linkedin for linkedin.com)."""
    return re.sub(r"[^a-z0-9]+", "_", domain.split(".")[0].lower()).strip("_") or "site"


@dataclass
class MapOutcome:
    card: dict[str, Any]
    report: dict[str, Any] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)


class Mapper(Protocol):
    """Implemented by the mapper: map one action of a site that has no card, starting at ``request.url``.

    Return the site's new card. Raise `SignInNeeded` when the site wants a sign-in, `MapError` otherwise.
    ``progress`` takes a short line per step, shown to the requester while the job runs."""

    def domain_for(self, host: str) -> str: ...

    async def map_site(self, request: MapRequest, progress: Callable[[str], None]) -> MapOutcome: ...


@dataclass
class MapConfig:
    allow_local: bool = False  # map sites on this machine or a private network (tests and demos only)
    poll_s: float = 0.5
    retry_after_s: int = 30


class MapManager:
    def __init__(
        self,
        registry: Registry,
        state: StateStore,
        executor: Executor,
        mapper: Mapper,
        healer: HealManager | None = None,
        config: MapConfig | None = None,
        connections: ConnectionManager | None = None,
    ) -> None:
        self.registry = registry
        self.state = state
        self.executor = executor
        self.mapper = mapper
        self.healer = healer
        self.config = config or MapConfig()
        self.connections = connections
        self._wake = asyncio.Event()
        if connections is not None:
            connections.on_connected.append(self._signed_in)

    # ---- requests ----------------------------------------------------------

    async def request(
        self,
        user: str,
        site: str,
        task: str = "",
        action: str = "",
        inputs: dict[str, Any] | None = None,
        *,
        write: bool = False,
    ) -> dict[str, Any]:
        """Queue a map job, or say why not (``{"error": ...}``). With no ``action`` the job explores the site
        (``task``, optional, says what the person wants to do there)."""
        url = site.strip()
        if "://" not in url:
            url = "https://" + url
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host:
            return {"error": f"not a website: {site!r}"}
        if not self.config.allow_local and await _private(host):
            return {"error": f"{host} is on a private network; only public websites can be mapped"}
        task = " ".join(task.split())
        if len(task) > MAX_TASK or (action and not task):
            return {"error": f"task must say what the action does, in at most {MAX_TASK} characters"}
        if action and not ACTION_NAME.match(action):
            return {"error": "action must be a short snake_case name, e.g. search_companies"}
        if not action and inputs:
            return {"error": "inputs go with an action; to explore the site, give neither"}
        if write and not action:
            return {"error": "write goes with a named action; exploring maps writes by itself"}
        if not action and CHANGE.search(task) and not READING.match(task):
            return {
                "error": "that's one specific change, not a site to explore: name it",
                "hint": "call request_map again with action (a snake_case name, e.g. follow_company), "
                'inputs with the values to use (e.g. {"company": "anthropic"}: who or what it acts on) and '
                "write: true. Nothing reaches the site while it's set up.",
            }
        values: dict[str, str] = {}
        for name, value in (inputs or {}).items():
            if not ACTION_NAME.match(name) or isinstance(value, (dict, list)) or value is None:
                return {"error": 'inputs must map snake_case names to the values to use, e.g. {"query": "x"}'}
            values[name] = str(value).lower() if isinstance(value, bool) else str(value)
            if re.match(r"\s*https?://", values[name]):
                return {
                    "error": f"input {name} is a whole address; give the part that identifies it",
                    "hint": "e.g. for https://www.linkedin.com/company/anthropicresearch/ use "
                    '{"company_slug": "anthropicresearch"}, and pass the address as site',
                }
        if len(values) > MAX_INPUTS:
            return {"error": f"at most {MAX_INPUTS} inputs"}

        domain = self.mapper.domain_for(host)
        stored = self.registry.get(domain)
        if stored is not None:
            answer = self._existing(user, stored.card, action)
            if answer is not None:
                return answer  # it's there (or being healed); otherwise the job adds to the card
        open_job = self.state.open_map_job_for(domain)
        if open_job is not None:
            return self._status(open_job, user)
        start = f"{parts.scheme}://{parts.netloc}{parts.path or '/'}" + (
            f"?{parts.query}" if parts.query else ""
        )
        job = self.state.create_map_job(domain, start, task, action, values, user, write=write)
        log.info("queued map %s for %s (%s) by %s", job.id, domain, action, user)
        self._wake.set()
        return self._status(job, user)

    def _existing(self, user: str, card: Card, action: str) -> dict[str, Any] | None:
        """The site has a card. A broken action is healed instead (and the heal's status returned); an action
        that's there says so; anything else (an action it lacks, or exploring for more) is mapped and added
        to the card (None: queue the job)."""
        domain = card.domain
        if action and card.action(action) is not None:
            names = [action]
        elif action:
            return None
        else:
            names = [a.name for a in card.actions]
        broken = [n for n in names if self.state.is_degraded(domain, n)]
        healing = [n for n in names if self.state.open_job_for(domain, n) is not None]
        if self.healer is not None and (healing or broken):
            name = (healing or broken)[0]
            heal = self.healer.page_changed(domain, name, "a map was asked for while it is broken", user)
            if heal is not None:
                return {
                    "status": "healing",
                    "domain": domain,
                    "heal": heal.model_dump(mode="json"),
                    "user_message": f"Part of {domain} stopped working and is being repaired.",
                    "hint": "check heal_status(heal.job_id); once it succeeds, use find_actions as usual",
                }
        if not action:
            return None  # explore again: whatever the card doesn't have yet is added
        return {
            "status": "exists",
            "domain": domain,
            "actions": [a.name for a in card.actions],
            "user_message": f"{domain} can already do that.",
            "hint": f"use find_actions(site={domain!r}), then run_action",
        }

    # ---- status ------------------------------------------------------------

    async def status(self, job_id: str, user: str, wait_s: float = 0.0) -> dict[str, Any] | None:
        """The job's status; with ``wait_s``, hold until it finishes (or the time is up)."""
        started = time.monotonic()
        wait_s = max(0.0, min(wait_s, MAX_WAIT_S))
        while True:
            job = self.state.get_map_job(job_id)
            if job is None:
                return None
            waiting = job.open or (job.status == "needs_sign_in" and self._sign_in_pending(job))
            if not waiting or time.monotonic() - started >= wait_s:
                return self._status(job, user)
            await asyncio.sleep(min(self.config.poll_s, 1.0))

    def jobs_for(self, user: str) -> list[dict[str, Any]]:
        return [j.to_json() for j in self.state.map_jobs_for(user)]

    def _status(self, job: MapJob, user: str) -> dict[str, Any]:
        owner = job.user == user
        out = job.to_json(owner=owner)
        error = (job.result or {}).get("error")
        if job.open:
            ahead = "" if job.status == "running" else " It's in the queue."
            doing = "Exploring" if not job.action else "Setting up"
            minutes = "up to ten minutes" if not job.action else "a few minutes"
            out["user_message"] = f"{doing} {job.domain}; this takes {minutes}.{ahead}"
            out["retry_after_s"] = self.config.retry_after_s
            out["hint"] = "call map_status(job_id, wait_seconds=45) until the status is succeeded"
        elif job.status == "succeeded":
            out["user_message"] = f"{job.domain} is ready."
            out["hint"] = f"use find_actions(site={job.domain!r}), then run_action"
        elif job.status == "needs_sign_in":
            link = self._sign_in_link(job) if owner else None
            if link is not None and link.get("url"):
                out["user_message"] = f"{job.domain} needs you to sign in before it can be set up. " + str(
                    link["user_message"]
                )
                out["sign_in"] = {"url": link["url"], "expires_at": link.get("expires_at")}
                out["retry_after_s"] = self.config.retry_after_s
                out["hint"] = (
                    "The user can't see this result: write the user_message (it holds the sign-in link) "
                    "in your reply, then call map_status(job_id, wait_seconds=45) until the status is "
                    "succeeded. The job carries on by itself once they've signed in; their password is never "
                    "shared with you."
                )
            elif owner:
                out["user_message"] = (
                    f"{job.domain} needs you to be signed in, and signing in isn't available on this server."
                )
            else:
                out["user_message"] = f"{job.domain} needs a sign-in before it can be set up."
        else:
            out["user_message"] = f"Setting up {job.domain} didn't work" + (
                f": {error}" if owner and error else "."
            )
        if not owner:
            out["note"] = "someone else asked for this site first; it becomes usable for everyone once mapped"
        return out

    # ---- the worker ----------------------------------------------------------

    async def run_forever(self) -> None:
        """One map job at a time, apart from heal jobs."""
        orphans = self.state.fail_orphaned_map_jobs()
        if orphans:
            log.warning("marked %d interrupted map jobs as failed", orphans)
        while True:
            ran = await self.run_pending()
            if not ran:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self.config.poll_s)

    async def run_pending(self) -> bool:
        job = self.state.next_map_job()
        if job is None or not self.state.start_map_job(job.id):
            return False
        try:
            await self.run_job(job)
        except Exception as e:
            log.exception("map %s crashed", job.id)
            self.state.finish_map_job(job.id, "failed", {"error": f"internal error ({type(e).__name__})"})
        return True

    async def drain(self) -> None:
        while await self.run_pending():
            pass

    async def run_job(self, job: MapJob) -> None:
        stored = self.registry.get(job.domain)
        if stored is not None and job.action and stored.card.action(job.action) is not None:
            self.state.finish_map_job(
                job.id, "succeeded", {"actions": [], "note": "it was added in the meantime"}
            )
            return
        base_version = stored.version if stored is not None else None
        existing = stored.card.to_json_dict() if stored is not None else None
        before = {a.name for a in stored.card.actions} if stored is not None else set()
        started = time.monotonic()
        providers = stored.card.connect_providers if stored is not None else []
        provider = providers[0].id if providers else provider_for(job.domain)
        signed_in = self.executor.sessions.get(job.user, job.domain, provider) is not None
        request = MapRequest(
            job.id,
            job.domain,
            job.url,
            job.task,
            job.action,
            dict(job.inputs),
            job.user,
            sign_in=provider if signed_in else None,
            login_url=(job.result or {}).get("login_url")
            or (providers[0].login_url if providers else None)
            or (job.url if signed_in else None),
            existing=existing,
            write=job.write,
        )
        try:
            outcome = await self.mapper.map_site(request, lambda line: self.state.map_progress(job.id, line))
        except SignInNeeded as e:
            stop: dict[str, Any] = {
                "error": str(e),
                "log": e.log[-20:],
                "login_url": e.login_url or request.login_url or job.url,
                "provider": provider,
            }
            self.state.finish_map_job(job.id, "needs_sign_in", stop)
            log.info("map %s needs a sign-in: %s", job.id, e)
            stopped = self.state.get_map_job(job.id)
            if stopped is not None:
                self._sign_in_link(stopped)  # so the link is waiting (and on the account page) right away
            return
        except MapError as e:
            self.state.finish_map_job(job.id, "failed", {"error": str(e), "log": e.log[-20:]})
            log.info("map %s failed: %s", job.id, e)
            return
        ms = int((time.monotonic() - started) * 1000)
        report = validate_card(outcome.card)
        if not report.ok or report.card is None:
            self.state.finish_map_job(
                job.id,
                "failed",
                {"error": "the mapped card isn't valid", "errors": report.errors[:20], "ms": ms},
            )
            return
        card = report.card
        if card.domain != job.domain:
            self.state.finish_map_job(job.id, "failed", {"error": f"the mapped card is for {card.domain}"})
            return
        new = [a for a in card.actions if a.name not in before]
        if not new:
            self.state.finish_map_job(job.id, "failed", {"error": "nothing new could be added", "ms": ms})
            return
        problems = await self._check_examples(card, job.user, {a.name for a in new})
        if problems and len(problems) == len([a for a in new if a.kind == "read"]):
            self.state.finish_map_job(
                job.id,
                "failed",
                {"error": "the mapped actions don't work yet", "examples": problems, "ms": ms},
            )
            return
        if problems:  # keep what passes (an explore maps several actions)
            kept = dict(
                outcome.card, actions=[a for a in outcome.card["actions"] if a["name"] not in problems]
            )
            again = validate_card(kept)
            if not again.ok or again.card is None:
                self.state.finish_map_job(
                    job.id, "failed", {"error": "the mapped card isn't valid", "ms": ms}
                )
                return
            card = again.card
        for p in card.connect_providers if stored is None else []:  # a new card's sign-in check
            code = await self.executor.check_session(card, p.id, job.user)
            if code is not None:
                self.state.finish_map_job(
                    job.id,
                    "failed",
                    {"error": f"the mapped sign-in check fails with your sign-in ({code.value})", "ms": ms},
                )
                return
        now = self.registry.get(job.domain)
        if (now.version if now is not None else None) != base_version:
            self.state.finish_map_job(
                job.id, "failed", {"error": f"{job.domain} changed while this ran; ask again to add to it"}
            )
            return
        added = [a.name for a in card.actions if a.name not in before]
        version = self.registry.save(card, note=f"map {job.id}: {', '.join(added)}")
        names = [a.name for a in card.actions]
        result: dict[str, Any] = {"actions": names, "added": added, "ms": ms, "report": outcome.report}
        if problems:
            result["dropped"] = problems
        for action in card.actions:
            if action.kind == "write" and action.name in added:  # from a held-back request: never sent
                self.state.set_never_run(job.domain, action.name, True)
        self.state.finish_map_job(job.id, "succeeded", result, new_version=version)
        log.info("map %s succeeded: %s version %d in %d ms", job.id, job.domain, version, ms)

    # ---- signing in ------------------------------------------------------------

    def _sign_in_link(self, job: MapJob) -> dict[str, Any] | None:
        """The requester's Connect link for a job that stopped for a sign-in (a new one once it expires)."""
        if self.connections is None or job.status != "needs_sign_in":
            return None
        result = job.result or {}
        parts = urlsplit(job.url)
        return self.connections.start_for_new_site(
            job.user,
            job.domain,
            result.get("provider") or provider_for(job.domain),
            result.get("login_url") or job.url,
            f"{parts.scheme}://{parts.netloc}",
        )

    def _sign_in_pending(self, job: MapJob) -> bool:
        conn = self.connections.latest(job.user, job.domain) if self.connections is not None else None
        return conn is not None and conn.status == "pending"

    def _signed_in(self, user: str, domain: str, provider: str) -> None:
        """A sign-in was stored: carry on with the user's jobs that stopped for it (decision 4)."""
        if provider != provider_for(domain) or self.registry.get(domain) is not None:
            return
        resumed = self.state.resume_map_jobs(user, domain)
        if resumed:
            log.info("%s signed in to %s: resuming %s", user, domain, ", ".join(resumed))
            self._wake.set()

    async def _check_examples(self, card: Card, user: str, only: set[str] | None = None) -> dict[str, str]:
        """Every read action's examples (``only``: those actions'), run through the executor as agents will
        call them."""
        problems: dict[str, str] = {}
        for action in card.actions:
            if action.kind != "read" or (only is not None and action.name not in only):
                continue
            for i, example in enumerate(action.examples):
                env = await self.executor.run_internal(card, 0, action.name, dict(example.params), user)
                if not example_passes(env, example.min_items):
                    got = len(env.data or [])
                    problems[action.name] = f"examples[{i}] gave {env.status.value} with {got} items"
                    break
        return problems


async def _private(host: str) -> bool:
    """A name or address on this machine or a private network (what the server's browser must not visit for
    someone else). Names are resolved, so a public-looking name that points inside counts too."""
    host = host.strip("[]")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False  # doesn't resolve: the browser will say so
    for info in infos:
        try:
            if not ipaddress.ip_address(info[4][0]).is_global:
                return True
        except ValueError:
            continue
    return False
