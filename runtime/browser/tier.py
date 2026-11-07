"""The browser tier: each step runs as `fetch()` inside a tab on the site, in the user's warm profile."""

from __future__ import annotations

import asyncio
import json
from contextlib import AbstractAsyncContextManager
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError

from runtime.browser.pool import BrowserPool
from runtime.template import BuiltRequest
from runtime.tiers import HttpResponse, ProfileKey, Session, TransportError, TransportTimeout
from schema.card import Tier

# Headers the browser sets itself; fetch() would ignore or reject them.
FORBIDDEN = {
    "accept-charset", "accept-encoding", "connection", "content-length", "cookie", "cookie2", "date", "dnt",
    "expect", "host", "keep-alive", "origin", "referer", "te", "trailer", "transfer-encoding", "upgrade",
    "user-agent", "via",
}

FETCH_JS = """async ([method, url, headers, body]) => {
  const init = {method, headers, credentials: 'include', redirect: 'follow'};
  if (body !== null) init.body = body;
  const r = await fetch(url, init);
  const h = {};
  r.headers.forEach((v, k) => { h[k] = v; });
  return {status: r.status, headers: h, text: await r.text(), url: r.url, redirected: r.redirected};
}"""


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class BrowserSession:
    def __init__(self, pool: BrowserPool, key: ProfileKey, base_url: str, seed_cookies: dict[str, str]) -> None:
        self._pool = pool
        self._key = key
        self._base_url = base_url
        self._seed = seed_cookies
        self._lease: AbstractAsyncContextManager[BrowserContext] | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._cookies: dict[str, str] = {}

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    async def prepare(self) -> None:
        self._lease = self._pool.lease(self._key)
        self._context = await self._lease.__aenter__()
        present = await self._read_cookies()
        # Cookies carried over from a lower tier (e.g. set by an earlier step) join the profile.
        missing = [
            {"name": n, "value": v, "url": self._base_url} for n, v in self._seed.items() if n not in present
        ]
        if missing:
            await self._context.add_cookies(missing)  # type: ignore[arg-type]
        self._page = await self._context.new_page()
        await self._goto(self._base_url)
        await self._read_cookies()

    async def _goto(self, url: str) -> None:
        assert self._page is not None
        try:
            await self._page.goto(_origin(url) + "/", wait_until="domcontentloaded", timeout=15_000)
        except PlaywrightError:
            pass  # a failed landing page still gives us the origin for fetch()

    async def _read_cookies(self) -> dict[str, str]:
        assert self._context is not None
        found = await self._context.cookies([self._base_url])
        self._cookies = {c["name"]: c["value"] for c in found if "name" in c and "value" in c}
        return self._cookies

    async def send(self, req: BuiltRequest, timeout_s: float) -> HttpResponse:
        assert self._page is not None
        if _origin(self._page.url) != _origin(req.url):
            await self._goto(req.url)  # fetch from the request's own origin: same-site, no CORS
        headers = {k: v for k, v in req.headers.items() if k.lower() not in FORBIDDEN}
        body: str | None = None
        if req.has_body:
            if req.content is not None:
                body = req.content
            else:
                body = json.dumps(req.json_body)
                headers.setdefault("content-type", "application/json")
        try:
            async with asyncio.timeout(max(timeout_s, 0.001)):
                result: dict[str, Any] = await self._page.evaluate(FETCH_JS, [req.method, req.url, headers, body])
        except TimeoutError:
            raise TransportTimeout(f"no response within {timeout_s:.1f}s") from None
        except PlaywrightError as e:
            raise TransportError(f"browser fetch failed: {e.message.splitlines()[0]}", sent=True) from None
        await self._read_cookies()
        return HttpResponse(
            status=int(result["status"]),
            headers={str(k).lower(): str(v) for k, v in result["headers"].items()},
            text=str(result["text"]),
            final_url=str(result["url"]),
            hops=[],  # fetch() only exposes where a redirect ended up (DECISIONS.md)
        )

    async def aclose(self) -> None:
        if self._page is not None:
            try:
                await self._page.close()
            except PlaywrightError:
                pass
            self._page = None
        if self._lease is not None:
            await self._lease.__aexit__(None, None, None)
            self._lease = None


class BrowserTier:
    name: Tier = "browser"

    def __init__(self, pool: BrowserPool) -> None:
        self.pool = pool

    def open(self, base_url: str, seed_cookies: dict[str, str], profile: ProfileKey | None = None) -> Session:
        key = profile or ProfileKey("_shared", urlsplit(base_url).hostname or "site", None)
        return BrowserSession(self.pool, key, base_url, seed_cookies)
