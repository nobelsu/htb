"""The browser flowmap drives, set up the way the gateway's own browser is (DECISIONS M8).

Capture, the agent recorder and heal all open their browser through a ``BrowserSetup``:

- installed Google Chrome by default (``channel="chrome"``), not Playwright's bundled Chromium, and without
  the automation switch, so sign-in pages that refuse automated browsers (Google's "this browser may not be
  secure") and bot checks see an ordinary Chrome; falls back to Chromium when Chrome isn't installed;
- a user's stored sign-in from the gateway's session store (``for_sign_in``): its cookies go into every fresh
  context, never through a file on disk;
- that sign-in's device identity (user-agent, client hints, language), as every runtime tier presents it;
- the sign-in's residential proxy (``PROXY_URL``), so mapping leaves from the same IP the card is later called
  from.

Cookie values never appear in logs or errors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

VIEWPORT = {"width": 1280, "height": 800}
NO_AUTOMATION = ["--disable-blink-features=AutomationControlled"]


class SignInMissing(Exception):
    """The site needs a sign-in the store doesn't have for this user."""


@dataclass
class BrowserSetup:
    channel: str = "chrome"  # "chrome" = installed Google Chrome; "chromium" = Playwright's Chromium
    headless: bool = True
    cookies: dict[str, str] = field(default_factory=dict, repr=False)  # a stored sign-in; values are secret
    cookie_url: str | None = None  # the site the cookies belong to
    identity: Any = None  # spider.signin.identity.BrowserIdentity
    proxy: Any = None  # spider.signin.proxy.ProxySpec
    storage_state: Path | None = None  # a Playwright storage state file (tests, older callers)
    profile_dir: Path | None = None  # a persistent profile: one context for every run (signing in by hand)

    def __repr__(self) -> str:  # never print cookie values or the proxy password
        return (
            f"BrowserSetup(channel={self.channel!r}, headless={self.headless}, cookies={len(self.cookies)}, "
            f"identity={'yes' if self.identity else 'no'}, proxy={self.proxy!r}, profile={self.profile_dir})"
        )

    # ---------- from the gateway's store ----------

    @classmethod
    def for_sign_in(
        cls,
        sessions: Any,
        user: str,
        domain: str,
        provider: str,
        base_url: str,
        *,
        proxies: Any = None,
        channel: str = "chrome",
        headless: bool = True,
    ) -> BrowserSetup:
        """``user``'s sign-in to ``domain`` through ``provider``, from the gateway's session store, with its
        device identity and proxy. Raises SignInMissing when there is none."""
        cookies = sessions.get(user, domain, provider) if sessions is not None else None
        if not cookies:
            raise SignInMissing(
                f"{user} has no sign-in for {domain} ({provider}): sign in through the gateway first"
            )
        lookup = getattr(sessions, "identity", None)
        identity = lookup(user, domain, provider) if lookup is not None else None
        proxy = proxies.for_sign_in(user, domain, provider, base_url) if proxies is not None else None
        return cls(
            channel=channel,
            headless=headless,
            cookies=dict(cookies),
            cookie_url=base_url,
            identity=identity,
            proxy=proxy,
        )

    # ---------- launching ----------

    def _launch_args(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "headless": self.headless,
            "ignore_default_args": ["--enable-automation"],
            "args": list(NO_AUTOMATION),
        }
        if self.channel != "chromium":
            kwargs["channel"] = self.channel
        if self.proxy is not None:
            kwargs["proxy"] = {
                "server": self.proxy.server,
                "username": self.proxy.username,
                "password": self.proxy.password,
            }
            if getattr(self.proxy, "local", False):
                kwargs["args"].append("--proxy-bypass-list=<-loopback>")
        return kwargs

    def _context_args(self, user_agent: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"viewport": VIEWPORT, "locale": "en-US"}
        if self.identity is not None:
            kwargs["user_agent"] = self.identity.user_agent
            kwargs["extra_http_headers"] = {"accept-language": self.identity.accept_language}
        elif user_agent:
            kwargs["user_agent"] = user_agent
        return kwargs

    async def launch(self, pw: Any) -> Browser:
        """Start the browser (installed Chrome, else Chromium). Use ``Browser.new_context`` for each run."""
        try:
            return await self._launch(pw)
        except Exception as e:
            if self.channel == "chromium":
                raise
            log.warning("couldn't start %s (%s); using Playwright's Chromium", self.channel, type(e).__name__)
            self.channel = "chromium"
            return await self._launch(pw)

    async def _launch(self, pw: Any) -> Browser:
        if self.profile_dir is not None:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            shared = await pw.chromium.launch_persistent_context(
                str(self.profile_dir), **self._launch_args(), **self._context_args(None)
            )
            return Browser(self, None, shared, None)
        browser = await pw.chromium.launch(**self._launch_args())
        user_agent = await _headed_user_agent(browser) if self.headless and self.identity is None else None
        return Browser(self, browser, None, user_agent)


class Browser:
    """A started browser: ``new_context()`` gives a fresh context per run (or the shared persistent one)."""

    def __init__(self, setup: BrowserSetup, browser: Any, shared: Any, user_agent: str | None) -> None:
        self.setup = setup
        self._browser = browser
        self.shared = shared
        self._user_agent = user_agent

    async def new_context(self) -> Any:
        if self.shared is not None:
            await self._add_cookies(self.shared)
            _IDENTITIES[id(self.shared)] = self.setup.identity
            return self.shared
        s = self.setup
        kwargs = s._context_args(self._user_agent)
        if s.storage_state is not None:
            kwargs["storage_state"] = str(s.storage_state)
        context = await self._browser.new_context(**kwargs)
        await self._add_cookies(context)
        _IDENTITIES[id(context)] = s.identity
        context.on("close", lambda _c: _IDENTITIES.pop(id(context), None))
        return context

    async def _add_cookies(self, context: Any) -> None:
        s = self.setup
        if not s.cookies or not s.cookie_url:
            return
        from spider.mappers.flowmap.infer.actions import (
            registrable,  # not at the top: infer imports capture, which imports this
        )

        parts = urlsplit(s.cookie_url)
        host = parts.hostname or ""
        # Domain cookies (".linkedin.com"), so the sign-in also reaches the site's other subdomains.
        domain = host if host.replace(".", "").isdigit() or host == "localhost" else "." + registrable(host)
        secure = parts.scheme == "https"
        await context.add_cookies(
            [
                {"name": k, "value": v, "domain": domain, "path": "/", "secure": secure}
                for k, v in s.cookies.items()
            ]
        )

    async def close(self) -> None:
        if self.shared is not None:
            await self.shared.close()
        if self._browser is not None:
            await self._browser.close()


_IDENTITIES: dict[int, Any] = {}  # context id → the device identity its pages present


async def new_page(context: Any) -> Any:
    """A new page that presents the context's device identity from its first request: user-agent, client hints
    (which need DevTools per page, as the runtime's browser tier does) and language."""
    page = await context.new_page()
    identity = _IDENTITIES.get(id(context))
    if identity is not None:
        try:
            cdp = await context.new_cdp_session(page)
            await cdp.send("Network.setUserAgentOverride", identity.cdp_override())
        except Exception as e:  # a browser without DevTools: Playwright's user_agent still applies
            log.debug("client hints not applied: %s", type(e).__name__)
    return page


async def _headed_user_agent(browser: Any) -> str | None:
    """Headless Chrome says 'HeadlessChrome'; send what a headed Chrome sends (as the gateway's pool does)."""
    page = await browser.new_page()
    try:
        ua: str = await page.evaluate("navigator.userAgent")
    finally:
        await page.close()
    return ua.replace("HeadlessChrome/", "Chrome/")
