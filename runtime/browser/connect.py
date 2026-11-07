"""The login flow behind start_connection / poll_connection / connection_status.

start_connection opens the site's login page in the user's warm browser profile and returns a link to a
streamed view of it (runtime/browser/stream.py). The user logs in themselves; we never see the password.
A watcher runs the provider's session_check in that profile every few seconds; once it passes, the profile's
cookies are copied (encrypted) into the session store for the http/impersonate tiers.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from runtime.browser.pool import BrowserPool
from runtime.executor import Executor
from runtime.registry import Registry
from runtime.session_store import EncryptedSessionStore
from runtime.tiers import ProfileKey
from schema import Card, ResultCode

log = logging.getLogger("runtime.connect")
R = ResultCode

ConnStatus = Literal["pending", "connected", "expired", "failed"]


@dataclass
class Connection:
    token: str
    user: str
    domain: str
    provider: str
    login_url: str
    expires_at: float
    status: ConnStatus = "pending"
    message: str = "waiting for the user to log in"
    page: Page | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def public(self, base_url: str) -> dict[str, Any]:
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


class ConnectionManager:
    def __init__(
        self,
        registry: Registry,
        pool: BrowserPool,
        store: EncryptedSessionStore,
        executor: Executor,
        public_base_url: str,
        timeout_s: float = 600.0,
        check_every_s: float = 2.0,
    ) -> None:
        self.registry = registry
        self.pool = pool
        self.store = store
        self.executor = executor
        self.public_base_url = public_base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.check_every_s = check_every_s
        self._by_token: dict[str, Connection] = {}
        self._latest: dict[tuple[str, str], Connection] = {}  # (user, domain) -> most recent connection

    # ---- tools -------------------------------------------------------------

    def _card(self, domain: str) -> Card | None:
        stored = self.registry.get(domain)
        return stored.card if stored else None

    async def start(self, user: str, domain: str, provider: str | None = None) -> dict[str, Any]:
        card = self._card(domain)
        if card is None:
            return {"error": f"unknown domain {domain!r}"}
        if not card.connect_providers:
            return {"error": f"{domain} needs no login"}
        chosen = card.provider(provider) if provider else card.connect_providers[0]
        if chosen is None:
            return {"error": f"{domain} has no provider {provider!r}"}
        current = self._latest.get((user, domain))
        if current is not None and current.status == "pending" and current.provider == chosen.id:
            return self._with_instructions(current)
        conn = Connection(
            token=secrets.token_urlsafe(24),
            user=user,
            domain=domain,
            provider=chosen.id,
            login_url=chosen.login_url,
            expires_at=time.time() + self.timeout_s,
        )
        self._by_token[conn.token] = conn
        self._latest[(user, domain)] = conn
        conn.task = asyncio.get_running_loop().create_task(self._watch(conn))
        log.info("connection started for %s on %s (%s)", user, domain, chosen.id)
        return self._with_instructions(conn)

    def _with_instructions(self, conn: Connection) -> dict[str, Any]:
        out = conn.public(self.public_base_url)
        out["instructions"] = (
            f"Give the user this link and ask them to log in to {conn.domain} there (they type their own "
            "password; it is never shared with you). Then call poll_connection until status is 'connected', "
            "and retry the original tool."
        )
        return out

    def poll(self, user: str, domain: str) -> dict[str, Any]:
        conn = self._latest.get((user, domain))
        if conn is not None:
            out = conn.public(self.public_base_url)
            if conn.status == "connected":
                out["hint"] = "connected: call the original tool again"
            elif conn.status == "pending":
                out["hint"] = "still waiting for the user to log in; poll again in a few seconds"
            else:
                out["hint"] = "call start_connection again for a new link"
            return out
        return self.status(user, domain)

    def status(self, user: str, domain: str) -> dict[str, Any]:
        card = self._card(domain)
        if card is None:
            return {"error": f"unknown domain {domain!r}"}
        providers = []
        for p in card.connect_providers:
            record = self.store.record(user, domain, p.id)
            pending = self._latest.get((user, domain))
            state: str = record.status if record else "not_connected"
            if pending is not None and pending.provider == p.id and pending.status == "pending":
                state = "pending"
            providers.append(
                {
                    "provider": p.id,
                    "status": state,
                    "connected_at": record.connected_at if record else None,
                    "checked_at": record.checked_at if record else None,
                    "browser_warm": self.pool.is_warm(ProfileKey(user, domain, p.id)),
                }
            )
        return {"domain": domain, "login_needed": bool(card.connect_providers), "providers": providers}

    def by_token(self, token: str) -> Connection | None:
        return self._by_token.get(token)

    # ---- the watcher -------------------------------------------------------

    async def _watch(self, conn: Connection) -> None:
        key = ProfileKey(conn.user, conn.domain, conn.provider)
        try:
            async with self.pool.lease(key) as context:
                conn.page = await context.new_page()
                try:
                    await conn.page.goto(conn.login_url, wait_until="domcontentloaded", timeout=30_000)
                except PlaywrightError as e:
                    log.warning("login page did not load cleanly: %s", e.message.splitlines()[0])
                while True:
                    if time.time() > conn.expires_at:
                        conn.status, conn.message = "expired", "the login link expired; start a new connection"
                        return
                    await asyncio.sleep(self.check_every_s)
                    card = self._card(conn.domain)
                    if card is None:
                        conn.status, conn.message = "failed", "the site's card was removed"
                        return
                    code = await self.executor.check_session(
                        card, conn.provider, conn.user, tier="browser", cookies={}
                    )
                    if code is None:
                        found = await context.cookies([card.base_url])
                        cookies = {c["name"]: c["value"] for c in found if "name" in c and "value" in c}
                        self.store.put(conn.user, conn.domain, conn.provider, cookies)
                        conn.status, conn.message = "connected", f"connected to {conn.domain}"
                        log.info("%s connected to %s (%d cookies stored)", conn.user, conn.domain, len(cookies))
                        return
        except Exception as e:
            log.exception("connection watcher failed")
            conn.status, conn.message = "failed", f"login browser failed: {type(e).__name__}"
        finally:
            if conn.page is not None:
                try:
                    await conn.page.close()
                except PlaywrightError:
                    pass
                conn.page = None

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

    async def close(self) -> None:
        for conn in list(self._by_token.values()):
            if conn.task is not None and not conn.task.done():
                conn.task.cancel()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
