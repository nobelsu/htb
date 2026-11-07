"""Warm, persistent browser profiles: one Chrome context per (user, site, provider), kept running while in use
and closed after an idle period. The profile folder on disk *is* the browser-tier session."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Playwright, async_playwright

from runtime.tiers import ProfileKey

log = logging.getLogger("runtime.browser")

VIEWPORT = {"width": 1280, "height": 800}
ANON = "_anon"


@dataclass
class BrowserConfig:
    profiles_dir: Path
    channel: str = "chrome"  # "chrome" = installed Google Chrome; "chromium" = Playwright's Chromium
    headless: bool = True
    idle_s: float = 600.0  # close a profile's browser after this long unused
    launch_timeout_s: float = 30.0


@dataclass
class _Entry:
    context: BrowserContext
    leases: int = 0
    last_used: float = 0.0


def _safe(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", part) or "_"


class BrowserPool:
    def __init__(self, config: BrowserConfig) -> None:
        self.config = config
        self._pw: Playwright | None = None
        self._user_agent: str | None = None
        self._entries: dict[ProfileKey, _Entry] = {}
        self._locks: dict[ProfileKey, asyncio.Lock] = {}
        self._start_lock = asyncio.Lock()

    def profile_dir(self, key: ProfileKey) -> Path:
        return self.config.profiles_dir / _safe(key.user) / _safe(key.domain) / _safe(key.provider or ANON)

    async def _playwright(self) -> Playwright:
        async with self._start_lock:
            if self._pw is None:
                self._pw = await async_playwright().start()
                self._user_agent = await self._real_user_agent(self._pw)
            return self._pw

    async def _real_user_agent(self, pw: Playwright) -> str | None:
        """Headless Chrome advertises itself as 'HeadlessChrome'; use the same string a headed Chrome sends."""
        if not self.config.headless:
            return None
        browser = await pw.chromium.launch(channel=self.config.channel, headless=True)
        try:
            page = await browser.new_page()
            ua: str = await page.evaluate("navigator.userAgent")
        finally:
            await browser.close()
        return ua.replace("HeadlessChrome/", "Chrome/")

    async def _context(self, key: ProfileKey) -> _Entry:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._entries.get(key)
            if entry is not None:
                return entry
            pw = await self._playwright()
            path = self.profile_dir(key)
            path.mkdir(parents=True, exist_ok=True)
            started = time.monotonic()
            kwargs: dict[str, Any] = {
                "channel": self.config.channel,
                "headless": self.config.headless,
                "viewport": VIEWPORT,
                "locale": "en-US",
                "timeout": self.config.launch_timeout_s * 1000,
            }
            if self._user_agent:
                kwargs["user_agent"] = self._user_agent
            context = await pw.chromium.launch_persistent_context(str(path), **kwargs)
            log.info("browser for %s/%s/%s up in %.1fs", key.user, key.domain, key.provider or ANON,
                     time.monotonic() - started)
            entry = _Entry(context=context, last_used=time.monotonic())

            def forget(_ctx: BrowserContext, k: ProfileKey = key) -> None:
                self._entries.pop(k, None)

            context.on("close", forget)
            self._entries[key] = entry
            return entry

    @asynccontextmanager
    async def lease(self, key: ProfileKey) -> AsyncIterator[BrowserContext]:
        """Use a profile's context; it stays warm afterwards until idle."""
        entry = await self._context(key)
        entry.leases += 1
        try:
            yield entry.context
        finally:
            entry.leases -= 1
            entry.last_used = time.monotonic()

    def is_warm(self, key: ProfileKey) -> bool:
        return key in self._entries

    async def close_idle(self) -> list[ProfileKey]:
        now = time.monotonic()
        idle = [k for k, e in self._entries.items() if e.leases == 0 and now - e.last_used >= self.config.idle_s]
        for key in idle:
            entry = self._entries.pop(key, None)
            if entry is not None:
                await entry.context.close()
        return idle

    async def delete_profile(self, key: ProfileKey) -> bool:
        """Close the profile's browser (even mid-use) and delete its folder. Returns whether a folder existed."""
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._entries.pop(key, None)
            if entry is not None:
                try:
                    await entry.context.close()
                except Exception:
                    pass
            path = self.profile_dir(key)
            if not path.exists():
                return False
            await asyncio.to_thread(shutil.rmtree, path, True)
            return True

    async def reap_forever(self, every_s: float = 30.0) -> None:
        while True:
            await asyncio.sleep(every_s)
            try:
                closed = await self.close_idle()
                if closed:
                    log.info("closed %d idle browsers", len(closed))
            except Exception:
                log.exception("closing idle browsers failed")

    async def close(self) -> None:
        for key in list(self._entries):
            entry = self._entries.pop(key, None)
            if entry is not None:
                try:
                    await entry.context.close()
                except Exception:
                    pass
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None
