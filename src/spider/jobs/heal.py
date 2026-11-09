"""Self-healing (CARD_SPEC.md "Healing"): heal jobs, EMPTY spikes and scheduled health checks.

The mapper plugs in through `Remapper`: given the active card and the broken action names, it re-captures
the site and returns a proposed new card. Everything else (queueing, pre-checks, validating against
examples, building the new version action by action) happens here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from spider.card import Action, Card, Envelope, Heal, ResultCode, validate_card
from spider.run.executor import Executor
from spider.store.cards import Registry
from spider.store.jobs import HealJob, JobStore, Trigger

log = logging.getLogger(__name__)
R = ResultCode

# Pre-check results that cancel a heal: re-mapping can't fix these (CARD_SPEC.md Healing step 3).
CANCEL_CODES = {R.BLOCKED, R.AUTH_EXPIRED, R.RATE_LIMITED, R.AUTH_REQUIRED}
SITE_LEVEL_EXCLUDE = {"actions", "provenance"}


class RemapError(Exception):
    pass


class Remapper(Protocol):
    """Implemented by the mapper. Re-capture the site and re-map only `actions`.

    Return the whole proposed card (a `Card` or its JSON dict). Unchanged actions should be returned as-is;
    the runtime decides, action by action, what goes into the new version. Raise `RemapError` on failure.
    `user` is whose call found the break: re-capture with their sign-in.
    """

    async def remap(
        self, card: Card, actions: list[str], reason: str, user: str | None = None
    ) -> Card | dict[str, Any]: ...


class NoRemapper:
    async def remap(
        self, card: Card, actions: list[str], reason: str, user: str | None = None
    ) -> Card | dict[str, Any]:
        raise RemapError("no remapper is configured")


@dataclass
class HealConfig:
    retry_after_s: int = 30  # what PAGE_CHANGED envelopes tell agents
    empty_spike_threshold: int = 5
    cooldown_s: float = 300.0  # after a failed/cancelled heal, don't re-heal the same action this soon
    poll_s: float = 0.25  # worker idle poll
    health_interval_s: float = 0.0  # 0 = scheduled health checks off
    health_disabled_domains: frozenset[str] = field(default_factory=frozenset)


def example_passes(env: Envelope, min_items: int) -> bool:
    """A health-check / validation pass: SUCCESS or EMPTY with at least `min_items` items."""
    if env.status not in (R.SUCCESS, R.EMPTY):
        return False
    return len(env.data or []) >= min_items


class HealManager:
    """Implements the executor's `HealingHooks` and runs heal jobs."""

    def __init__(
        self,
        registry: Registry,
        state: JobStore,
        executor: Executor,
        remapper: Remapper | None = None,
        config: HealConfig | None = None,
    ) -> None:
        self.registry = registry
        self.state = state
        self.executor = executor
        self.remapper: Remapper = remapper or NoRemapper()
        self.config = config or HealConfig()
        self._wake = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        executor.healing = self

    # ---- HealingHooks ------------------------------------------------------

    def page_changed(
        self, domain: str, action: str, reason: str, user: str, trigger: Trigger = "page_changed"
    ) -> Heal | None:
        open_job = self.state.open_job_for(domain, action)
        if open_job is not None:
            return Heal(job_id=open_job.id, retry_after_s=self.config.retry_after_s)
        latest = self.state.latest_job_for(domain, action)
        if (
            latest is not None
            and latest.status in ("failed", "cancelled", "needs_review")
            and latest.finished_at
        ):
            left = self.config.cooldown_s - (time.time() - _ts(latest.finished_at))
            if left > 0:
                return Heal(job_id=latest.id, retry_after_s=max(1, int(left)))
        job = self.state.create_job(domain, [action], trigger, reason, user)
        log.info("queued heal %s for %s.%s (%s): %s", job.id, domain, action, trigger, reason)
        self._wake.set()
        return Heal(job_id=job.id, retry_after_s=self.config.retry_after_s)

    def record_result(self, domain: str, action: str, code: ResultCode, user: str) -> None:
        if code == R.SUCCESS:
            self.state.reset_empty(domain, action)
            return
        if code != R.EMPTY:
            return
        count = self.state.bump_empty(domain, action)
        if count < self.config.empty_spike_threshold:
            return
        self.state.reset_empty(domain, action)
        self._spawn(self._spike_check(domain, action, user))

    def is_degraded(self, domain: str, action: str) -> bool:
        return self.state.is_degraded(domain, action)

    # ---- public helpers ----------------------------------------------------

    def heal_status(self, job_id: str) -> dict[str, Any] | None:
        job = self.state.get_job(job_id)
        if job is None:
            return None
        out = job.to_json()
        if job.open:
            out["retry_after_s"] = self.config.retry_after_s
        return out

    async def run_forever(self) -> None:
        """The heal worker: one job at a time (so at most one per domain)."""
        orphans = self.state.fail_orphaned_jobs()
        if orphans:
            log.warning("marked %d orphaned heal jobs as failed", orphans)
        while True:
            ran = await self.run_pending()
            if not ran:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.config.poll_s)
                except TimeoutError:
                    pass

    async def run_pending(self) -> bool:
        """Run the next runnable job, if any. Returns whether one ran."""
        job = self.state.next_runnable()
        if job is None or not self.state.start_job(job.id):
            return False
        try:
            await self.run_job(job)
        except Exception as e:
            log.exception("heal %s crashed", job.id)
            self.state.finish_job(job.id, "failed", {"error": f"{type(e).__name__}: {e}"})
        return True

    async def drain(self) -> None:
        """Wait for spawned spike checks, then run every queued job (tests and CLI)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        while await self.run_pending():
            pass

    # ---- one heal job ------------------------------------------------------

    async def run_job(self, job: HealJob) -> None:
        stored = self.registry.get(job.domain)
        if stored is None:
            self.state.finish_job(job.id, "failed", {"error": f"no active card for {job.domain}"})
            return
        card, version = stored.card, stored.version
        broken = [a for a in job.actions if card.action(a) is not None]
        if not broken:
            self.state.finish_job(job.id, "failed", {"error": "the broken actions are no longer in the card"})
            return

        # 1. Pre-check: don't re-map when the problem is a block, a login or a rate limit.
        verdict = await self._precheck(card, version, broken, job.user)
        if verdict is not None:
            self.state.finish_job(job.id, "cancelled", verdict)
            log.info("heal %s cancelled: %s", job.id, verdict)
            return

        # 2. Re-map. Events with timings go to heal_events (DECISIONS M4).
        started = time.monotonic()
        self.state.add_event(job.id, job.domain, "remap_start", {"actions": broken, "reason": job.reason})
        try:
            proposed = await self.remapper.remap(card, broken, job.reason, user=job.user)
        except RemapError as e:
            ms = int((time.monotonic() - started) * 1000)
            failed = {"actions": broken, "ms": ms, "error": str(e)}
            self.state.add_event(job.id, job.domain, "remap_failed", failed)
            self._degrade(job, broken, f"re-map failed: {e}")
            self.state.finish_job(job.id, "failed", {"error": f"re-map failed: {e}", "degraded": broken})
            return
        details = getattr(self.remapper, "last_details", None)
        self.state.add_event(
            job.id,
            job.domain,
            "remap_done",
            {
                "actions": broken,
                "ms": int((time.monotonic() - started) * 1000),
                **({"details": details} if isinstance(details, dict) else {}),
            },
        )
        raw = proposed.to_json_dict() if isinstance(proposed, Card) else proposed
        report = validate_card(raw)
        if not report.ok or report.card is None:
            self._degrade(job, broken, "re-mapped card failed validation")
            self.state.finish_job(
                job.id, "failed", {"error": "re-mapped card failed validation", "errors": report.errors[:20]}
            )
            return
        new = report.card
        if new.domain != card.domain:
            self.state.finish_job(job.id, "failed", {"error": f"re-mapped card is for {new.domain}"})
            return

        # 3. Build the new version action by action, validating against examples.
        await self._activate(job, card, version, new, broken)
        finished = self.state.get_job(job.id)
        self.state.add_event(
            job.id,
            job.domain,
            "heal_done",
            {
                "status": finished.status if finished else "unknown",
                "ms": int((time.monotonic() - started) * 1000),
                "new_version": finished.new_version if finished else None,
            },
        )

    async def _precheck(
        self, card: Card, version: int, broken: list[str], user: str
    ) -> dict[str, Any] | None:
        providers = {
            p
            for name in broken
            if (a := card.action(name)) is not None
            and (p := a.effective_auth_provider(card.defaults)) is not None
        }
        for provider in sorted(providers):
            code = await self.executor.check_session(card, provider, user)
            if code is not None:
                return {"code": code.value, "reason": f"session_check for {provider!r} returned {code.value}"}
        for name in broken:
            action = card.action(name)
            if action is None or action.kind != "read" or not action.examples:
                continue
            example = action.examples[0]
            env = await self.executor.run_internal(card, version, name, dict(example.params), user)
            if env.status in CANCEL_CODES:
                return {"code": env.status.value, "reason": f"example for {name} returned {env.status.value}"}
            if example_passes(env, example.min_items):
                return {"reason": f"example for {name} passes now; nothing to heal"}
            break  # one example is enough
        return None

    async def _activate(self, job: HealJob, old: Card, version: int, new: Card, broken: list[str]) -> None:
        site_changed = _site_level(old) != _site_level(new)
        old_actions = {a.name: a for a in old.actions}
        new_actions = {a.name: a for a in new.actions}
        changed = [
            n for n, a in old_actions.items() if n in new_actions and _dump(new_actions[n]) != _dump(a)
        ]
        result: dict[str, Any] = {"site_level_changed": site_changed, "healed": [], "kept_old": {}}

        # Healed writes go live too (decision H4). They are never run to validate them: that would really
        # post/buy/follow, and writes usually have no examples. They are listed as unvalidated.
        changed_writes = [n for n in changed if old_actions[n].kind == "write"]
        changed_reads = [n for n in changed if old_actions[n].kind == "read"]
        result["unvalidated"] = changed_writes

        # Tentative card: new site-level fields + changed actions + old everything else.
        site = new if site_changed else old
        chosen: dict[str, Action] = {
            n: (new_actions[n] if n in changed_reads or n in changed_writes else a)
            for n, a in old_actions.items()
        }
        to_validate = (
            [n for n, a in chosen.items() if a.kind == "read"] if site_changed else list(changed_reads)
        )
        tentative = _compose(site, old, chosen)
        failures: dict[str, str] = {}
        for name in to_validate:
            problem = await self._validate_action(tentative, version + 1, name, job.user)
            if problem:
                failures[name] = problem
        result["validated"] = {n: failures.get(n, "pass") for n in to_validate}

        if site_changed and failures:
            # Every read inherits site-level fields, so a site-level change is all or nothing.
            self._degrade(job, broken, "healed site-level fields failed validation")
            result["error"] = "site-level change failed validation; nothing activated"
            result["degraded"] = broken
            self.state.finish_job(job.id, "failed", result)
            return

        for name in changed_reads:
            if name in failures:
                chosen[name] = old_actions[name]
                result["kept_old"][name] = failures[name]
            else:
                result["healed"].append(name)
        result["healed"].extend(changed_writes)
        for name in broken:
            if name not in changed:
                result["kept_old"].setdefault(name, "the remapper did not change it")

        degraded = [n for n in broken if n not in result["healed"]] + [
            n for n in result["kept_old"] if n not in broken and n in failures
        ]
        result["degraded"] = sorted(set(degraded))
        for name in result["healed"]:
            self.state.set_degraded(old.domain, name, False)
        self._degrade(job, result["degraded"], "heal failed; running the old definition")

        if not result["healed"] and not (site_changed and not failures):
            self.state.finish_job(job.id, "failed", result)
            log.info("heal %s failed: %s", job.id, json.dumps(result))
            return

        final = _compose(site, old, chosen, provenance=new)
        new_version = self.registry.save(
            final, note=f"heal {job.id}: {', '.join(result['healed']) or 'site-level'}"
        )
        self.state.finish_job(job.id, "succeeded", result, new_version=new_version)
        log.info("heal %s succeeded: version %d, %s", job.id, new_version, json.dumps(result))

    async def _validate_action(self, card: Card, version: int, name: str, user: str) -> str | None:
        action = card.action(name)
        assert action is not None
        if not action.examples:
            return "no examples to validate against"
        for i, example in enumerate(action.examples):
            env = await self.executor.run_internal(card, version, name, dict(example.params), user)
            if not example_passes(env, example.min_items):
                got = len(env.data or [])
                return (
                    f"examples[{i}] returned {env.status.value} with {got} items (need {example.min_items})"
                )
        return None

    def _degrade(self, job: HealJob, actions: list[str], reason: str) -> None:
        for name in actions:
            self.state.set_degraded(job.domain, name, True, f"{reason} ({job.id})")

    # ---- EMPTY spikes and health checks ------------------------------------

    async def _spike_check(self, domain: str, action_name: str, user: str) -> None:
        stored = self.registry.get(domain)
        action = stored.card.action(action_name) if stored else None
        if stored is None or action is None:
            return
        examples = [e for e in action.examples if e.min_items >= 1]
        if not examples:
            return  # its examples don't normally return items
        env = await self.executor.run_internal(
            stored.card, stored.version, action_name, dict(examples[0].params), user
        )
        if env.status == R.EMPTY:
            reason = (
                f"{self.config.empty_spike_threshold} EMPTY results in a row, and the example is EMPTY too"
            )
            self.page_changed(domain, action_name, reason, user, trigger="empty_spike")

    async def check_domain(self, domain: str) -> list[str]:
        """Run every read action's examples once (no-login sites only). Returns the actions that failed."""
        stored = self.registry.get(domain)
        if stored is None:
            return []
        card = stored.card
        if any(a.effective_auth_provider(card.defaults) is not None for a in card.actions):
            return []  # login sites rely on errors from real runs
        failed: list[str] = []
        for action in card.actions:
            if action.kind != "read" or not action.examples:
                continue
            if self.state.open_job_for(domain, action.name):
                continue
            problem = await self._validate_action(card, stored.version, action.name, "default")
            if problem:
                failed.append(action.name)
                self.page_changed(
                    domain, action.name, f"health check: {problem}", "default", trigger="health_check"
                )
        return failed

    async def health_checks_forever(self) -> None:
        if self.config.health_interval_s <= 0:
            log.info("scheduled health checks are off")
            return
        while True:
            await asyncio.sleep(self.config.health_interval_s)
            for domain in sorted(self.registry.active_cards()):
                if domain in self.config.health_disabled_domains:
                    continue
                try:
                    await self.check_domain(domain)
                except Exception:
                    log.exception("health check for %s failed", domain)

    def _spawn(self, coro: Any) -> None:
        task: asyncio.Task[None] = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _dump(model: Any) -> str:
    return json.dumps(model.model_dump(by_alias=True, mode="json"), sort_keys=True)


def _site_level(card: Card) -> str:
    data = card.model_dump(by_alias=True, mode="json", exclude=SITE_LEVEL_EXCLUDE)
    return json.dumps(data, sort_keys=True)


def _compose(site: Card, old: Card, chosen: dict[str, Action], provenance: Card | None = None) -> Card:
    """Site-level fields from `site`, actions in `old`'s order from `chosen`."""
    data = site.to_json_dict()
    data["actions"] = [
        chosen[a.name].model_dump(by_alias=True, exclude_unset=True, mode="json") for a in old.actions
    ]
    if provenance is not None:
        data["provenance"] = provenance.provenance.model_dump(mode="json")
    return Card.model_validate(data)


def _ts(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).timestamp()
