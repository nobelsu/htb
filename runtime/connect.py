"""The login flow behind start_connection / poll_connection / connection_status (extension capture).

start_connection returns a link to our connect page. The user logs in to the site normally in their own
browser, opens the link and clicks Connect; our browser extension reads that one site's cookies and posts them
back (runtime/connect_page.py). We verify them with the provider's session_check before storing them encrypted.
The password never reaches us or the agent.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from runtime.browser.pool import BrowserPool
from runtime.executor import Executor
from runtime.registry import Registry
from runtime.session_store import EncryptedSessionStore
from runtime.tiers import ProfileKey
from schema import Card, ResultCode

log = logging.getLogger("runtime.connect")
R = ResultCode

ConnStatus = Literal["pending", "connected", "expired"]
MAX_COOKIES = 300
MAX_COOKIE_BYTES = 16_384


@dataclass
class Connection:
    token: str
    user: str
    domain: str
    provider: str
    login_url: str
    base_url: str
    expires_at: float
    status: ConnStatus = "pending"
    message: str = "waiting for the user to connect"

    def refresh(self) -> None:
        if self.status == "pending" and time.time() > self.expires_at:
            self.status, self.message = "expired", "the link expired; start a new connection"

    def public(self, base_url: str) -> dict[str, Any]:
        self.refresh()
        out: dict[str, Any] = {
            "domain": self.domain,
            "provider": self.provider,
            "status": self.status,
            "message": self.message,
            "expires_at": _iso(self.expires_at),
        }
        if self.status == "pending":
            out["url"] = f"{base_url}/connect/{self.token}"
        return out

    def cookie_origins(self, card_domain: str) -> list[str]:
        """Host-permission patterns the extension asks the user for (the site's host and its domain)."""
        parts = urlsplit(self.base_url)
        scheme, host = parts.scheme or "https", parts.hostname or card_domain
        origins = [f"{scheme}://{host}/*"]
        if host != card_domain and host.endswith("." + card_domain):
            origins.append(f"{scheme}://*.{card_domain}/*")  # cookies set on the parent domain (e.g. .linkedin.com)
        return origins


class CookieImportError(ValueError):
    pass


class ConnectionManager:
    def __init__(
        self,
        registry: Registry,
        store: EncryptedSessionStore,
        executor: Executor,
        public_base_url: str,
        pool: BrowserPool | None = None,
        timeout_s: float = 600.0,
    ) -> None:
        self.registry = registry
        self.store = store
        self.executor = executor
        self.pool = pool
        self.public_base_url = public_base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._by_token: dict[str, Connection] = {}
        self._latest: dict[tuple[str, str], Connection] = {}  # (user, domain) -> most recent connection

    def _card(self, domain: str) -> Card | None:
        stored = self.registry.get(domain)
        return stored.card if stored else None

    # ---- tools -------------------------------------------------------------

    def start(self, user: str, domain: str, provider: str | None = None) -> dict[str, Any]:
        card = self._card(domain)
        if card is None:
            return {"error": f"unknown domain {domain!r}"}
        if not card.connect_providers:
            return {"error": f"{domain} needs no login"}
        chosen = card.provider(provider) if provider else card.connect_providers[0]
        if chosen is None:
            return {"error": f"{domain} has no provider {provider!r}"}
        current = self._latest.get((user, domain))
        if current is not None:
            current.refresh()
            if current.status == "pending" and current.provider == chosen.id:
                return self._with_instructions(current)
        conn = Connection(
            token=secrets.token_urlsafe(24),
            user=user,
            domain=domain,
            provider=chosen.id,
            login_url=chosen.login_url,
            base_url=card.base_url,
            expires_at=time.time() + self.timeout_s,
        )
        self._by_token[conn.token] = conn
        self._latest[(user, domain)] = conn
        log.info("connection started for %s on %s (%s)", user, domain, chosen.id)
        return self._with_instructions(conn)

    def _with_instructions(self, conn: Connection) -> dict[str, Any]:
        out = conn.public(self.public_base_url)
        out["instructions"] = (
            f"Give the user this link. They log in to {conn.domain} in their own browser as usual, then open the "
            "link and click Connect (it needs our browser extension; the page explains how to add it). Their "
            "password is never shared with you or us. Then call poll_connection until status is 'connected', and "
            "retry the original tool."
        )
        return out

    def poll(self, user: str, domain: str) -> dict[str, Any]:
        conn = self._latest.get((user, domain))
        if conn is None:
            return self.status(user, domain)
        out = conn.public(self.public_base_url)
        out["hint"] = {
            "connected": "connected: call the original tool again",
            "pending": "still waiting for the user; poll again in a few seconds",
            "expired": "call start_connection again for a new link",
        }[conn.status]
        return out

    def status(self, user: str, domain: str) -> dict[str, Any]:
        card = self._card(domain)
        if card is None:
            return {"error": f"unknown domain {domain!r}"}
        pending = self._latest.get((user, domain))
        if pending is not None:
            pending.refresh()
        providers = []
        for p in card.connect_providers:
            record = self.store.record(user, domain, p.id)
            state: str = record.status if record else "not_connected"
            if pending is not None and pending.provider == p.id and pending.status == "pending":
                state = "pending"
            providers.append(
                {
                    "provider": p.id,
                    "status": state,
                    "connected_at": record.connected_at if record else None,
                    "checked_at": record.checked_at if record else None,
                    "browser_warm": bool(self.pool and self.pool.is_warm(ProfileKey(user, domain, p.id))),
                }
            )
        return {"domain": domain, "login_needed": bool(card.connect_providers), "providers": providers}

    def by_token(self, token: str) -> Connection | None:
        conn = self._by_token.get(token)
        if conn is not None:
            conn.refresh()
        return conn

    # ---- cookie import (called by the connect page's endpoint) -------------

    async def import_cookies(self, token: str, cookies: Any) -> Connection:
        """Verify and store cookies the extension captured. Raises CookieImportError with a user-facing reason."""
        conn = self.by_token(token)
        if conn is None:
            raise CookieImportError("unknown login link")
        if conn.status != "pending":
            raise CookieImportError(f"this link is {conn.status}")
        jar = _parse_cookies(cookies)
        if not jar:
            raise CookieImportError(f"no cookies found for {conn.domain}; log in to {conn.domain} first")
        card = self._card(conn.domain)
        if card is None:
            raise CookieImportError("the site is no longer available")
        code = await self.executor.check_session(card, conn.provider, conn.user, cookies=jar)
        if code is not None:
            conn.message = f"those cookies are not logged in ({code.value}); log in to {conn.domain}, then try again"
            log.info("cookie import for %s on %s rejected: %s", conn.user, conn.domain, code.value)
            raise CookieImportError(conn.message)
        self.store.put(conn.user, conn.domain, conn.provider, jar)
        conn.status, conn.message = "connected", f"connected to {conn.domain}"
        log.info("%s connected to %s via the extension (%d cookies)", conn.user, conn.domain, len(jar))
        return conn

    # ---- keep-alive --------------------------------------------------------

    async def keepalive_once(self) -> dict[str, str]:
        """Run session_check for every connected session. Returns {"user/domain/provider": result}."""
        results: dict[str, str] = {}
        for record in self.store.records("connected"):
            card = self._card(record.domain)
            if card is None:
                continue
            code = await self.executor.check_session(card, record.provider, record.user)
            label = f"{record.user}/{record.domain}/{record.provider}"
            if code == R.AUTH_EXPIRED:
                self.store.mark(record.user, record.domain, record.provider, "expired")
                results[label] = "expired"
            elif code is None:
                self.store.mark(record.user, record.domain, record.provider, "connected")
                results[label] = "ok"
            else:
                results[label] = code.value  # blocked / rate limited / error: not a verdict on the login
        return results

    async def keepalive_forever(self, interval_s: float) -> None:
        import asyncio

        if interval_s <= 0:
            return
        while True:
            await asyncio.sleep(interval_s)
            try:
                results = await self.keepalive_once()
                if results:
                    log.info("keep-alive: %s", results)
            except Exception:
                log.exception("keep-alive failed")


def _parse_cookies(raw: Any) -> dict[str, str]:
    """Accept the extension's list of chrome.cookies objects (or a name->value dict); keep name -> value."""
    if isinstance(raw, dict):
        items = [{"name": k, "value": v} for k, v in raw.items()]
    elif isinstance(raw, list):
        items = raw
    else:
        raise CookieImportError("cookies must be a list")
    if len(items) > MAX_COOKIES:
        raise CookieImportError("too many cookies")
    jar: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise CookieImportError("each cookie must be an object")
        name, value = item.get("name"), item.get("value")
        if not isinstance(name, str) or not isinstance(value, str) or not name:
            raise CookieImportError("each cookie needs a string name and value")
        if len(name) + len(value) > MAX_COOKIE_BYTES:
            raise CookieImportError(f"cookie {name!r} is too large")
        jar[name] = value
    return jar


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
