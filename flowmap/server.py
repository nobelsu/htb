"""flowmap as the gateway's mapper (DECISIONS M8): ``MAPPER=flowmap`` maps a site when someone asks for it.

A map job is the same pipeline as ``record --agent`` followed by ``map``, run inside the gateway:

1. Claude does the task once in installed Chrome (the agent recorder, reads only);
2. the flow is saved under FLOWS_DIR, so heal can replay it later;
3. the flow is captured (marker and real runs), inferred, labelled, minimised and replayed;
4. the card goes back to the runtime, which validates it, runs its examples and saves it.

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
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flowmap.agent import AgentError, record_with_agent
from flowmap.assemble import AssembleError, map_action
from flowmap.browser import BrowserSetup, SignInMissing, new_page
from flowmap.capture import capture_flow
from flowmap.flows import save_flow
from flowmap.infer import InferError, registrable
from flowmap.minimise import executor_replayer
from flowmap.signin import SignInCheckError, infer_sign_in, placeholder_provider
from runtime.executor import Executor
from runtime.mapping import MapError, MapOutcome, MapRequest, SignInNeeded

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
        from playwright.async_api import async_playwright

        lines: list[str] = []

        def step(line: str) -> None:
            lines.append(line)
            progress(line)

        wanted_sign_in: list[str] = []  # the page that asked for it
        page: Any = None

        async def sign_in() -> None:
            wanted_sign_in.append(page.url if page is not None else request.url)
            raise AgentError(NOBODY_TO_SIGN_IN)

        t0 = time.monotonic()
        try:
            setup = self._setup(request)
        except SignInMissing as e:
            raise SignInNeeded(str(e), lines, request.login_url) from e
        step(f"opening {request.url}" + (" signed in" if request.sign_in else ""))
        try:
            async with async_playwright() as p:
                browser = await setup.launch(p)
                try:
                    page = await new_page(await browser.new_context())
                    recording = await record_with_agent(
                        page,
                        url=request.url,
                        task=request.task,
                        action=request.action,
                        inputs=request.inputs,
                        domain=request.domain,
                        description=request.task,
                        auth_provider=request.sign_in,
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
        flow = recording.flow
        save_flow(flow, self.flows_dir)
        t_record = time.monotonic()
        step(f"recorded in {recording.turns} turns; replaying it to see the site's requests")
        with tempfile.TemporaryDirectory(prefix="flowmap-") as captures:
            try:
                runs = await capture_flow(flow, root=Path(captures), setup=setup, pause_s=self.pause_s)
            except Exception as e:
                raise MapError(f"replaying the recording failed: {type(e).__name__}", lines) from e
            failed = [r.error for r in runs if r.error]
            if failed and len(failed) == len(runs):
                raise MapError(f"every replay of the recording failed: {failed[0]}", lines)
            step("working out the action from the requests")
            try:
                mapped = await map_action(
                    flow,
                    runs,
                    replay=executor_replayer(self._executor_for, request.user),
                    llm_client=self.client,
                    use_llm=self.use_llm,
                    pause_s=self.pause_s,
                    providers=_providers(request),
                )
            except (InferError, AssembleError) as e:
                raise MapError(f"{e} ({'; '.join(e.reasons[:3])})" if e.reasons else str(e), lines) from e
        card = mapped.final.card
        report = mapped.report()
        if request.sign_in:
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
            report["sign_in"] = check.report()
        report["ms"] = {
            "record": int((t_record - t0) * 1000),
            "map": int((time.monotonic() - t_record) * 1000),
        }
        report["turns"] = recording.turns
        step(f"mapped {request.action} (confidence {report['confidence']})")
        return MapOutcome(card=card, report=report, log=lines)



def _providers(request: MapRequest) -> list[dict[str, Any]] | None:
    """A signed-in card's provider while it is mapped; its real check is worked out afterwards."""
    if not request.sign_in:
        return None
    return [placeholder_provider(request.sign_in, request.login_url or request.url, request.url)]


def _login_page(asked: str, request: MapRequest) -> str:
    """The page that asked for a sign-in, if it's on the site (else the start page): Connect opens it."""
    host = urlsplit(asked).hostname or ""
    if urlsplit(asked).scheme in ("http", "https") and host and registrable(host) == registrable(
        urlsplit(request.url).hostname or ""
    ):
        return asked
    return request.url
