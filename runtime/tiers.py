"""Transport tiers. Only `http` is implemented; `impersonate` and `browser` are TODO."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from http.cookiejar import Cookie
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from runtime.identity import DEFAULT_IDENTITY, BrowserIdentity
from runtime.proxy import ProxySpec
from runtime.template import BuiltRequest
from schema.card import Tier


def site_cookies(jar: Iterable[Cookie], host: str) -> dict[str, str]:
    """Cookies in `jar` that `host` itself gets: its own and its parent domains', not ones set for other sites
    during cross-site redirects."""
    out: dict[str, str] = {}
    for cookie in jar:
        domain = (cookie.domain or "").lstrip(".").lower()
        if not domain or host == domain or host.endswith("." + domain):
            out[cookie.name] = cookie.value or ""
    return out


class TransportTimeout(Exception):
    """No complete response within the step's time budget."""


class TransportError(Exception):
    def __init__(self, message: str, sent: bool) -> None:
        super().__init__(message)
        self.sent = sent  # False when the request certainly never reached the site


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]  # lowercased names; repeated headers joined with ", "
    text: str
    final_url: str
    hops: list[tuple[int, str | None]] = field(default_factory=list)  # (status, Location) per redirect


@dataclass(frozen=True)
class ProfileKey:
    """Whose browser profile a browser-tier session uses. `provider` None = the site's anonymous profile."""

    user: str
    domain: str
    provider: str | None


class Session(Protocol):
    """One execution's connection + cookie jar. Closed when the execution ends."""

    @property
    def cookies(self) -> dict[str, str]: ...

    @property
    def site_cookies(self) -> dict[str, str]:
        """The cookies the site's own host would get now: what rotation saves back to the user's sign-in."""
        ...

    async def prepare(self) -> None:
        """Called once after `open`, before `cookies` is read or anything is sent."""
        ...

    async def send(self, req: BuiltRequest, timeout_s: float) -> HttpResponse: ...

    async def aclose(self) -> None: ...


class TierClient(Protocol):
    name: Tier

    def open(
        self,
        base_url: str,
        seed_cookies: dict[str, str],
        profile: ProfileKey | None = None,
        identity: BrowserIdentity | None = None,
        proxy: ProxySpec | None = None,
    ) -> Session: ...


class HttpSession:
    def __init__(
        self,
        base_url: str,
        seed_cookies: dict[str, str],
        identity: BrowserIdentity | None = None,
        proxy: ProxySpec | None = None,
    ) -> None:
        host = urlsplit(base_url).hostname or ""
        jar = httpx.Cookies()
        for name, value in seed_cookies.items():
            jar.set(name, value, domain=host)
        # The device identity's headers go on every request; a card's own headers still win per request.
        headers = (identity or DEFAULT_IDENTITY).headers()
        # httpx follows redirects and keeps every hop in response.history; the jar spans all steps.
        self._client = httpx.AsyncClient(
            cookies=jar, headers=headers, follow_redirects=True, timeout=None, proxy=proxy.url if proxy else None
        )
        self._host = host

    @property
    def cookies(self) -> dict[str, str]:
        return {c.name: c.value or "" for c in self._client.cookies.jar}

    @property
    def site_cookies(self) -> dict[str, str]:
        return site_cookies(self._client.cookies.jar, self._host)

    async def send(self, req: BuiltRequest, timeout_s: float) -> HttpResponse:
        kwargs: dict[str, object] = {"headers": req.headers}
        if req.has_body:
            if req.content is not None:
                kwargs["content"] = req.content
            else:
                kwargs["json"] = req.json_body
        try:
            async with asyncio.timeout(max(timeout_s, 0.001)):
                resp = await self._client.request(req.method, httpx.URL(req.url), **kwargs)  # type: ignore[arg-type]
        except TimeoutError:
            raise TransportTimeout(f"no response within {timeout_s:.1f}s") from None
        except httpx.TimeoutException:
            raise TransportTimeout(f"no response within {timeout_s:.1f}s") from None
        except httpx.ConnectError as e:
            raise TransportError(f"could not connect: {e}", sent=False) from None
        except httpx.HTTPError as e:
            raise TransportError(f"transport error: {type(e).__name__}: {e}", sent=True) from None
        headers: dict[str, str] = {}
        for k, v in resp.headers.multi_items():
            k = k.lower()
            headers[k] = f"{headers[k]}, {v}" if k in headers else v
        return HttpResponse(
            status=resp.status_code,
            headers=headers,
            text=resp.text,
            final_url=str(resp.url),
            hops=[(h.status_code, h.headers.get("location")) for h in resp.history],
        )

    async def prepare(self) -> None:
        return None

    async def aclose(self) -> None:
        await self._client.aclose()


class HttpTier:
    name: Tier = "http"

    def open(
        self,
        base_url: str,
        seed_cookies: dict[str, str],
        profile: ProfileKey | None = None,
        identity: BrowserIdentity | None = None,
        proxy: ProxySpec | None = None,
    ) -> Session:
        return HttpSession(base_url, seed_cookies, identity, proxy)




class ImpersonateSession:
    """Looks like real Chrome at the network level (TLS/HTTP2 fingerprint and browser headers) via curl_cffi."""

    def __init__(
        self,
        base_url: str,
        seed_cookies: dict[str, str],
        identity: BrowserIdentity | None = None,
        proxy: ProxySpec | None = None,
    ) -> None:
        from curl_cffi.requests import AsyncSession

        identity = identity or DEFAULT_IDENTITY
        self._host = urlsplit(base_url).hostname or ""
        # The closest real Chrome fingerprint (TLS, HTTP/2, header order), with the identity's own header values.
        self.target = identity.impersonate_target()
        self._client: AsyncSession[Any] = AsyncSession(
            impersonate=self.target, headers=identity.headers(), proxy=proxy.url if proxy else None
        )
        for name, value in seed_cookies.items():
            self._client.cookies.set(name, value, domain=self._host)

    @property
    def cookies(self) -> dict[str, str]:
        return {c.name: c.value or "" for c in self._client.cookies.jar}

    @property
    def site_cookies(self) -> dict[str, str]:
        return site_cookies(self._client.cookies.jar, self._host)

    async def send(self, req: BuiltRequest, timeout_s: float) -> HttpResponse:
        from curl_cffi.requests import exceptions as cx

        kwargs: dict[str, Any] = {
            "headers": req.headers,
            "allow_redirects": True,
            "quote": False,  # the URL is already encoded by our own rules
        }
        if req.url.startswith("http://"):
            kwargs["http_version"] = "v1"  # Chrome never sends an h2c upgrade over plain http
        if req.has_body:
            if req.content is not None:
                kwargs["data"] = req.content
            else:
                kwargs["json"] = req.json_body
        try:
            async with asyncio.timeout(max(timeout_s, 0.001)):
                resp = await self._client.request(req.method, req.url, **kwargs)  # type: ignore[arg-type]
        except TimeoutError:
            raise TransportTimeout(f"no response within {timeout_s:.1f}s") from None
        except cx.ConnectionError as e:  # includes DNS, TLS and connect timeouts: nothing was sent
            raise TransportError(f"could not connect: {e}", sent=False) from None
        except cx.Timeout:
            raise TransportTimeout(f"no response within {timeout_s:.1f}s") from None
        except cx.RequestException as e:
            raise TransportError(f"transport error: {type(e).__name__}: {e}", sent=True) from None
        headers: dict[str, str] = {}
        for k, v in resp.headers.multi_items():
            k = k.lower()
            headers[k] = f"{headers[k]}, {v}" if k in headers else v
        return HttpResponse(
            status=resp.status_code,
            headers=headers,
            text=resp.text,
            final_url=str(resp.url),
            hops=[(h.status_code, h.headers.get("location")) for h in resp.history],
        )

    async def prepare(self) -> None:
        return None

    async def aclose(self) -> None:
        await self._client.close()


class ImpersonateTier:
    name: Tier = "impersonate"

    def open(
        self,
        base_url: str,
        seed_cookies: dict[str, str],
        profile: ProfileKey | None = None,
        identity: BrowserIdentity | None = None,
        proxy: ProxySpec | None = None,
    ) -> Session:
        return ImpersonateSession(base_url, seed_cookies, identity, proxy)


LADDER: tuple[Tier, ...] = ("http", "impersonate", "browser")


def default_tiers() -> dict[str, TierClient]:
    """http + impersonate. The browser tier needs a running BrowserPool; the gateway adds it."""
    return {"http": HttpTier(), "impersonate": ImpersonateTier()}
