"""Runs one action of a card and builds the result envelope (CARD_SPEC.md "Result codes")."""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from runtime.confirm import Confirmer, canonical_params
from runtime.extract import ExtractError, ResponseView, jsonpath_all, project, run_extract
from runtime.redact import redact_headers, scrub
from runtime.registry import StoredCard
from runtime.sessions import SessionStore
from runtime.signals import content_type_ok, match_signal
from runtime.template import (
    BuiltRequest,
    Context,
    MissingCookie,
    StepValueError,
    TemplateError,
    to_int,
    build_request,
    merge_headers,
)
from runtime.tiers import HttpResponse, HttpTier, Session, TierClient, TransportError, TransportTimeout
from schema.card import Action, Card, Expect, Request, Step
from schema.envelope import Envelope, ErrorInfo, Heal, Meta, ResultCode
from schema.params import CONFIRMATION_ARG, coerce_params, confirmation_summary, format_field_errors
from schema.placeholders import parse_step_ref

log = logging.getLogger("runtime.executor")

ACTION_CAP_S = 30.0
DEFAULT_TIMEOUT_MS = 10_000

R = ResultCode


@dataclass
class Stop(Exception):
    """Ends an execution with a result code."""

    code: ResultCode
    message: str = ""
    step: str | None = None
    hint: str | None = None
    retry_after_s: int | None = None
    http_status: int | None = None  # the 4xx behind an ERROR, used by the probe


class HealingHooks(Protocol):
    """What the executor tells the healing system about agent-facing runs (see runtime/healing.py)."""

    def page_changed(self, domain: str, action: str, reason: str, user: str) -> Heal | None:
        """Queue (or join) a heal job for a PAGE_CHANGED result."""
        ...

    def record_result(self, domain: str, action: str, code: ResultCode, user: str) -> None:
        """Every fresh agent-facing result, for EMPTY-spike tracking."""
        ...

    def is_degraded(self, domain: str, action: str) -> bool: ...


class NoHealing:
    """No healing: PAGE_CHANGED returns `heal: null`, nothing is tracked."""

    def page_changed(self, domain: str, action: str, reason: str, user: str) -> Heal | None:
        return None

    def record_result(self, domain: str, action: str, code: ResultCode, user: str) -> None:
        return None

    def is_degraded(self, domain: str, action: str) -> bool:
        return False


@dataclass
class _Run:
    card: Card
    action: Action
    version: int
    user: str
    params: dict[str, Any]
    session: Session
    deadline: float
    primary: bool  # an agent-facing run (False for probes, health checks and heal validation)
    auth: str | None
    steps: dict[tuple[str, str], list[Any]] = field(default_factory=dict)

    def context(self) -> Context:
        return Context(self.card.base_url, self.params, self.session.cookies, self.steps)

    def headers_for(self, req: Request) -> dict[str, str]:
        return merge_headers(self.card.defaults.headers, self.action.headers, req.headers)

    def timeout_ms(self, own: int | None) -> int:
        return own or self.action.effective_timeout_ms(self.card.defaults) or DEFAULT_TIMEOUT_MS

    def remaining_s(self) -> float:
        return self.deadline - time.monotonic()


class Executor:
    def __init__(
        self,
        sessions: SessionStore,
        confirmer: Confirmer,
        tiers: dict[str, TierClient] | None = None,
        healing: HealingHooks | None = None,
        action_cap_s: float = ACTION_CAP_S,
    ) -> None:
        self.sessions = sessions
        self.confirmer = confirmer
        self.tiers: dict[str, TierClient] = tiers or {"http": HttpTier()}
        self.healing: HealingHooks = healing or NoHealing()
        self.action_cap_s = action_cap_s

    async def run(self, stored: StoredCard, action_name: str, args: dict[str, Any], user: str = "default") -> Envelope:
        action = stored.card.action(action_name)
        if action is None:
            raise KeyError(f"{stored.domain} has no action {action_name!r}")
        # TODO(cache): look up (domain, action, canonical_params, session_id) for SUCCESS/EMPTY reads.
        # TODO(rate-limit): per-(domain, session) bucket; every outgoing request counts.
        env, _ = await self._execute(stored.card, action, stored.version, args, user, primary=True)
        return env

    async def run_internal(
        self, card: Card, version: int, action_name: str, args: dict[str, Any], user: str = "default"
    ) -> Envelope:
        """Run an action for the runtime itself (examples for health checks and heal validation).

        No 4xx probe, no heal queueing, no EMPTY tracking. Confirmation is bypassed: only read
        actions should be run this way.
        """
        action = card.action(action_name)
        if action is None:
            raise KeyError(f"{card.domain} has no action {action_name!r}")
        env, _ = await self._execute(card, action, version, args, user, primary=False)
        return env

    async def check_session(self, card: Card, provider_id: str, user: str) -> ResultCode | None:
        """Run a provider's `session_check`. Returns None if it passes, else the result code."""
        provider = card.provider(provider_id)
        if provider is None:
            return R.ERROR
        cookies = self.sessions.get(user, card.domain, provider_id)
        if cookies is None:
            return R.AUTH_REQUIRED
        check = provider.session_check
        session = self.tiers["http"].open(card.base_url, cookies)
        try:
            ctx = Context(card.base_url, {}, session.cookies, {})
            try:
                built = build_request(check, merge_headers(card.defaults.headers, check.headers), ctx)
            except MissingCookie:
                return R.AUTH_EXPIRED
            except TemplateError:
                return R.ERROR
            try:
                resp = await session.send(built, DEFAULT_TIMEOUT_MS / 1000)
            except (TransportTimeout, TransportError):
                return R.ERROR
        finally:
            await session.aclose()
        listed = resp.status in check.expect.status
        if not listed and resp.status == 429:
            return R.RATE_LIMITED
        if (not listed and resp.status == 401) or match_signal(provider.logged_out_signals, resp):
            return R.AUTH_EXPIRED
        if match_signal(card.blocked_signals, resp):
            return R.BLOCKED
        if not listed or not content_type_ok(check.expect.content_type, resp.headers.get("content-type")):
            return R.ERROR
        if _missing_paths(check.expect.paths, resp.text):
            return R.ERROR
        return None

    # ---- one execution -----------------------------------------------------

    async def _execute(
        self,
        card: Card,
        action: Action,
        version: int,
        args: dict[str, Any],
        user: str,
        primary: bool,
        deadline: float | None = None,
    ) -> tuple[Envelope, int | None]:
        started = time.monotonic()
        run_id = "r_" + secrets.token_hex(6)
        # D7: only the http tier exists; impersonate/browser cards run on http for now.
        tier = self.tiers["http"]
        secrets_seen: list[str] = []

        def envelope(code: ResultCode, data: list[Any] | None = None, **kw: Any) -> Envelope:
            ms = int((time.monotonic() - started) * 1000)
            degraded = self.healing.is_degraded(card.domain, action.name)
            meta = Meta(ms=ms, tier=tier.name, degraded=degraded, map_version=version, run_id=run_id)
            env = Envelope(status=code, data=data, meta=meta, **kw)
            if primary:
                self.healing.record_result(card.domain, action.name, code, user)
            if env.error is not None:
                env.error.message = scrub(env.error.message, secrets_seen)
            log.info("%s %s.%s -> %s in %dms", run_id, card.domain, action.name, code, ms)
            return env

        def failed(stop: Stop, data: list[Any] | None = None) -> Envelope:
            heal = None
            if stop.code == R.PAGE_CHANGED and primary:
                heal = self.healing.page_changed(card.domain, action.name, stop.message, user)
            error = ErrorInfo(
                code=stop.code, message=stop.message, hint=stop.hint, step=stop.step, retry_after_s=stop.retry_after_s
            )
            return envelope(stop.code, data, error=error, heal=heal)

        # Row 0: params and confirmation id.
        params, errors = coerce_params(action.params, args)
        conf_id = args.get(CONFIRMATION_ARG)
        if conf_id is not None and not action.requires_confirmation:
            errors[CONFIRMATION_ARG] = "this action does not take a confirmation id"
        elif conf_id is not None and not isinstance(conf_id, str):
            errors[CONFIRMATION_ARG] = "must be a string"
        elif conf_id is not None and not errors:
            reason = self.confirmer.check(conf_id, user, card.domain, action.name, params)
            if reason:
                errors[CONFIRMATION_ARG] = reason
        if errors:
            return failed(Stop(R.BAD_PARAMS, format_field_errors(errors))), None

        # Row 0: login.
        auth = action.effective_auth_provider(card.defaults)
        cookies: dict[str, str] = {}
        if auth is not None:
            stored_cookies = self.sessions.get(user, card.domain, auth)
            if stored_cookies is None:
                return failed(
                    Stop(
                        R.AUTH_REQUIRED,
                        f"{card.domain} needs a connected {auth!r} account",
                        hint=f"call start_connection(domain={card.domain!r})",
                    )
                ), None
            cookies = stored_cookies
            secrets_seen.extend(cookies.values())

        # Row 0: confirmation.
        if action.requires_confirmation and conf_id is None:
            summary = confirmation_summary(action, canonical_params(params))
            conf = self.confirmer.issue(user, card.domain, action.name, params, summary)
            return envelope(R.NEEDS_CONFIRMATION, confirmation=conf), None

        session = tier.open(card.base_url, cookies)
        run = _Run(
            card=card,
            action=action,
            version=version,
            user=user,
            params=params,
            session=session,
            deadline=deadline if deadline is not None else started + self.action_cap_s,
            primary=primary,
            auth=auth,
        )
        try:
            try:
                await self._run_steps(run)
                code, data, next_params = self._output(run)
            except Stop as stop:
                if stop.code == R.EMPTY:
                    return envelope(R.EMPTY, [] if action.output.from_ is not None else None), None
                if stop.code == R.UNKNOWN_OUTCOME and action.verify is not None:
                    verdict = await self._verify(run)
                    if verdict is None:
                        return envelope(R.SUCCESS, self._output_or_none(run)), None
                    stop.message += f"; verify: {verdict}"
                return failed(stop), stop.http_status

            if action.verify is not None:
                verdict = await self._verify(run)
                if verdict is not None:
                    return failed(Stop(R.VERIFY_FAILED, verdict, step="verify"), data), None
            return envelope(code, data, next_params=next_params), None
        finally:
            await session.aclose()

    # ---- steps -------------------------------------------------------------

    async def _run_steps(self, run: _Run) -> None:
        action = run.action
        write_idx = action.write_step_index()
        for i, step in enumerate(action.steps):
            is_write = (
                not action.idempotent
                and write_idx is not None
                and i >= write_idx
                and step.request.method.upper() not in ("GET", "HEAD")
            )
            built = self._build(run, step.request, step.id)
            resp = await self._send(run, step, built, is_write)
            view = ResponseView(resp.text, resp.headers, run.session.cookies)
            for key, ex in step.extract.items():
                try:
                    run.steps[(step.id, key)] = run_extract(ex, view)
                except ExtractError as e:
                    raise Stop(R.PAGE_CHANGED, f"extract {key!r}: {e}", step=step.id) from None

    def _build(self, run: _Run, req: Request, step_id: str) -> BuiltRequest:
        try:
            return build_request(req, run.headers_for(req), run.context())
        except StepValueError as e:
            if e.count == 0:  # Row 7: structure fine, item not found.
                raise Stop(R.EMPTY, str(e), step=e.step_id) from None
            raise Stop(R.PAGE_CHANGED, f"{e} (the mapping is ambiguous)", step=e.step_id) from None
        except MissingCookie as e:
            raise Stop(
                R.AUTH_EXPIRED, f"session has no {e.name!r} cookie", step=step_id, hint=self._reconnect(run)
            ) from None
        except TemplateError as e:
            raise Stop(R.ERROR, f"could not build request: {e}", step=step_id) from None

    async def _send(self, run: _Run, step: Step, built: BuiltRequest, is_write: bool) -> HttpResponse:
        """Send one step, applying result-code rows 1-6. Retries timeouts/5xx once where allowed."""
        where = f"{built.method} {urlsplit(built.url).path}"
        for attempt in (1, 2):
            budget = min(run.timeout_ms(step.timeout_ms) / 1000, run.remaining_s())
            log.debug("%s attempt %d headers=%s", where, attempt, redact_headers(built.headers))
            problem: str
            sent = True
            if budget <= 0:
                problem, sent = "the action's 30 s budget is used up", False
            else:
                try:
                    resp = await run.session.send(built, budget)
                except TransportTimeout as e:
                    problem = f"timeout on {where}: {e}"
                except TransportError as e:
                    problem, sent = f"{where}: {e}", e.sent
                else:
                    if not await self._check(run, step.id, step.expect, resp, where):
                        return resp
                    problem = f"HTTP {resp.status} from {where}"
            # Row 4: timeout / 5xx (and transport failures).
            if is_write:
                if sent:
                    raise Stop(R.UNKNOWN_OUTCOME, f"{problem}; the write may or may not have happened", step=step.id)
                raise Stop(R.ERROR, f"{problem}; the write was not sent", step=step.id)
            if attempt == 1 and run.remaining_s() > 0:
                continue
            raise Stop(R.ERROR, problem if attempt == 1 else f"{problem} (after one retry)", step=step.id)
        raise AssertionError("unreachable")

    async def _check(self, run: _Run, step_id: str, expect: Expect, resp: HttpResponse, where: str) -> bool:
        """Rows 1-3, 5, 6 for one response. Returns True for a retryable 5xx, False if the response is good;
        raises Stop otherwise."""
        card, status = run.card, resp.status
        listed = status in expect.status  # D5: a listed status is expected, never an error by itself

        if not listed and status == 429:
            raise Stop(
                R.RATE_LIMITED,
                f"HTTP 429 from {where}",
                step=step_id,
                hint="back off and retry later",
                retry_after_s=_retry_after(resp.headers.get("retry-after")),
            )
        provider = card.provider(run.auth)
        logged_out = match_signal(provider.logged_out_signals, resp) if provider else None
        if (not listed and status == 401) or logged_out:
            why = f"HTTP {status} from {where}" + (f" (matched {logged_out})" if logged_out else "")
            if run.auth is None:
                raise Stop(R.ERROR, f"{why}; this action has no login configured", step=step_id)
            raise Stop(R.AUTH_EXPIRED, why, step=step_id, hint=self._reconnect(run))
        blocked = match_signal(card.blocked_signals, resp)
        if blocked:
            # TODO(ladder): escalate tiers instead of returning BLOCKED straight away.
            raise Stop(R.BLOCKED, f"{where} looks bot-blocked (matched {blocked})", step=step_id)
        if not listed and status >= 500:
            return True
        if not listed and 400 <= status < 500:
            changed = match_signal(card.changed_signals, resp)
            if changed:
                raise Stop(R.PAGE_CHANGED, f"HTTP {status} from {where} (matched {changed})", step=step_id)
            raise await self._probe_verdict(run, status, where, step_id)
        if not listed:
            raise Stop(R.PAGE_CHANGED, f"unexpected HTTP {status} from {where}, expected {expect.status}", step=step_id)
        if not content_type_ok(expect.content_type, resp.headers.get("content-type")):
            got = resp.headers.get("content-type") or "none"
            raise Stop(
                R.PAGE_CHANGED, f"{where} returned content type {got!r}, expected {expect.content_type}", step=step_id
            )
        missing = _missing_paths(expect.paths, resp.text)
        if missing:
            raise Stop(R.PAGE_CHANGED, f"{where} response is missing {missing}", step=step_id)
        return False

    async def _probe_verdict(self, run: _Run, status: int, where: str, step_id: str) -> Stop:
        """Row 5: a 4xx. Reads with examples run one example (once, never probed again)."""
        base = f"HTTP {status} from {where}"
        a = run.action
        if not (run.primary and a.kind == "read" and a.examples):
            return Stop(R.ERROR, base, step=step_id, http_status=status)
        example = a.examples[0]
        env, probe_status = await self._execute(
            run.card, a, run.version, dict(example.params), run.user, primary=False, deadline=run.deadline
        )
        if env.status == R.ERROR and probe_status == status:
            return Stop(
                R.PAGE_CHANGED,
                f"{base}; the action's example gets the same HTTP {status}, so the endpoint itself has changed",
                step=step_id,
            )
        got = f"{env.status}" + (f" (HTTP {probe_status})" if probe_status else "")
        return Stop(
            R.ERROR,
            f"{base}; the action's example returned {got}, so the input was probably bad",
            step=step_id,
            http_status=status,
        )

    # ---- output and verify -------------------------------------------------

    def _output(self, run: _Run) -> tuple[ResultCode, list[Any] | None, dict[str, Any] | None]:
        o = run.action.output
        if o.from_ is None:
            return R.SUCCESS, None, None
        raw = run.steps.get(parse_step_ref(o.from_), [])
        items = project(raw, o.fields)
        if o.limit is not None:
            items = items[: o.limit]
        if not items:
            return R.EMPTY, [], None
        np_ = o.next_params
        next_params = None
        if np_ is not None and np_.param in run.params:
            current = run.params[np_.param]
            if np_.until is not None:
                sid, key = parse_step_ref(np_.until)
                matches = run.steps.get((sid, key), [])
                if len(matches) == 0:
                    raise Stop(R.EMPTY, f"{np_.until} has no match", step=sid)
                if len(matches) > 1:
                    raise Stop(R.PAGE_CHANGED, f"{np_.until} has {len(matches)} matches, expected 1", step=sid)
                try:
                    total = to_int(matches[0])
                except TemplateError:
                    raise Stop(R.PAGE_CHANGED, f"{np_.until} is not a number: {matches[0]!r}", step=sid) from None
                has_next = current * np_.page_size < total
            else:
                has_next = len(raw) >= np_.page_size
            if has_next:
                next_params = {**run.params, np_.param: current + np_.increment}
        return R.SUCCESS, items, next_params

    def _output_or_none(self, run: _Run) -> list[Any] | None:
        try:
            return self._output(run)[1]
        except Stop:
            return None

    async def _verify(self, run: _Run) -> str | None:
        """Run `verify`. Returns None if it passes, else why it failed."""
        v = run.action.verify
        assert v is not None
        try:
            built = build_request(v.request, run.headers_for(v.request), run.context())
        except TemplateError as e:
            return f"could not build verify request: {e}"
        where = f"{built.method} {urlsplit(built.url).path}"
        budget = min(run.timeout_ms(v.timeout_ms) / 1000, max(run.remaining_s(), 0))
        if budget <= 0:
            return "no time left in the action's 30 s budget"
        try:
            resp = await run.session.send(built, budget)
        except (TransportTimeout, TransportError) as e:
            return f"{where}: {e}"
        if resp.status not in v.expect.status:
            return f"HTTP {resp.status} from {where}, expected {v.expect.status}"
        if not content_type_ok(v.expect.content_type, resp.headers.get("content-type")):
            return f"{where} returned content type {resp.headers.get('content-type')!r}"
        missing = _missing_paths(v.expect.paths, resp.text)
        if missing:
            return f"{where} response is missing {missing}"
        try:
            values = jsonpath_all(v.assert_.expr, ResponseView(resp.text, resp.headers, {}).json())
        except ExtractError as e:
            return str(e)
        if not values or any(val != v.assert_.equals for val in values):
            return f"assert {v.assert_.expr} == {v.assert_.equals!r} failed (got {values!r})"
        return None

    def _reconnect(self, run: _Run) -> str:
        return f"reconnect: call start_connection(domain={run.card.domain!r})"


def _missing_paths(paths: list[str], text: str) -> list[str]:
    if not paths:
        return []
    try:
        data = ResponseView(text, {}, {}).json()
    except ExtractError:
        return list(paths)
    return [p for p in paths if not jsonpath_all(p, data)]


def _retry_after(value: str | None) -> int | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0, int((when - datetime.now(UTC)).total_seconds()))
