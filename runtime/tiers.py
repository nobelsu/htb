"""Transport tiers. Only `http` is implemented; `impersonate` and `browser` are TODO."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from runtime.template import BuiltRequest
from schema.card import Tier


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


class Session(Protocol):
    """One execution's connection + cookie jar. Closed when the execution ends."""

    @property
    def cookies(self) -> dict[str, str]: ...

    async def send(self, req: BuiltRequest, timeout_s: float) -> HttpResponse: ...

    async def aclose(self) -> None: ...


class TierClient(Protocol):
    name: Tier

    def open(self, base_url: str, seed_cookies: dict[str, str]) -> Session: ...


class HttpSession:
    def __init__(self, base_url: str, seed_cookies: dict[str, str]) -> None:
        host = urlsplit(base_url).hostname or ""
        jar = httpx.Cookies()
        for name, value in seed_cookies.items():
            jar.set(name, value, domain=host)
        # httpx follows redirects and keeps every hop in response.history; the jar spans all steps.
        self._client = httpx.AsyncClient(cookies=jar, follow_redirects=True, timeout=None)

    @property
    def cookies(self) -> dict[str, str]:
        return {c.name: c.value or "" for c in self._client.cookies.jar}

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

    async def aclose(self) -> None:
        await self._client.aclose()


class HttpTier:
    name: Tier = "http"

    def open(self, base_url: str, seed_cookies: dict[str, str]) -> Session:
        return HttpSession(base_url, seed_cookies)


# TODO(impersonate): curl_cffi-based TierClient with a Chrome TLS/HTTP2 fingerprint.
# TODO(browser): TierClient backed by the browser worker; cookies come from the user's browser profile.
# TODO(ladder): on BLOCKED, escalate http -> impersonate -> browser per CARD_SPEC.md "Escalating on BLOCKED".
