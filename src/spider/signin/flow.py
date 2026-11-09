"""The login flow behind start_connection / poll_connection / connection_status.
start_connection returns a link to our connect page. There the user clicks "Log in" and signs in to the site
inside a real Chromium in a short-lived Neko container, streamed over WebRTC (signin/neko). We watch
its cookies and verify them with the provider's session_check before storing them encrypted. The password
never reaches us or the agent.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from spider.card import Card, ResultCode
from spider.run.executor import Executor
from spider.run.tiers.browser_pool import BrowserPool
from spider.run.tiers.http import ProfileKey
from spider.signin.identity import BrowserIdentity
from spider.signin.neko.manager import NekoManager, NekoUnavailable
from spider.store.cards import Registry
from spider.store.sessions import EncryptedSessionStore

log = logging.getLogger(__name__)
MAX_POLL_WAIT_S = 45.0
UNOPENED_POLL_WAIT_S = 6.0  # until the link is opened, don't hold the agent: it may not have sent the link
R = ResultCode

ConnStatus = Literal["pending", "connected", "expired"]


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
    browser_task: asyncio.Task[None] | None = None  # the "log in here" watcher, if started
    # True when the provider's session_check also passes with no cookies at all: it can't tell signed-in from
    # signed-out, so we wait for the user to confirm instead of auto-connecting (None = not checked yet).
    check_is_weak: bool | None = None
    first_cookies: dict[str, str] | None = None  # the login browser's cookies when first read
    identity: BrowserIdentity | None = None  # the login browser's device identity, saved with the sign-in
    page_opened: bool = False  # someone loaded the connect page (so the user has the link)

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


class ConnectError(ValueError):
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
        neko: NekoManager | None = None,
        check_every_s: float = 2.0,
    ) -> None:
        self.registry = registry
        self.store = store
        self.executor = executor
        self.pool = pool
        self.public_base_url = public_base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.neko = neko
        self.check_every_s = check_every_s
        self._by_token: dict[str, Connection] = {}
        self._latest: dict[tuple[str, str], Connection] = {}  # (user, domain) -> most recent connection
        # Called with (user, domain, provider) whenever a sign-in is stored (map jobs resume on it).
        self.on_connected: list[Callable[[str, str, str], None]] = []

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

    def start_for_new_site(
        self, user: str, domain: str, provider: str, login_url: str, base_url: str
    ) -> dict[str, Any]:
        """A sign-in for a site that has no card yet, because a map job needs one (DECISIONS M8).

        There is no session_check to verify it with yet, so it is treated like a weak check: the user clicks
        "I've signed in", and that only counts once signing in has changed the site's cookies. The latest
        pending link is reused."""
        current = self._latest.get((user, domain))
        if current is not None:
            current.refresh()
            if current.status == "pending" and current.provider == provider:
                return self._with_instructions(current)
        conn = Connection(
            token=secrets.token_urlsafe(24),
            user=user,
            domain=domain,
            provider=provider,
            login_url=login_url,
            base_url=base_url,
            expires_at=time.time() + self.timeout_s,
            check_is_weak=True,
        )
        self._by_token[conn.token] = conn
        self._latest[(user, domain)] = conn
        log.info("connection started for %s on %s (a site being mapped)", user, domain)
        return self._with_instructions(conn)

    def latest(self, user: str, domain: str) -> Connection | None:
        conn = self._latest.get((user, domain))
        if conn is not None:
            conn.refresh()
        return conn

    def _with_instructions(self, conn: Connection) -> dict[str, Any]:
        out = conn.public(self.public_base_url)
        out["user_message"] = _poll_message(conn, out.get("url"))
        out["instructions"] = (
            "The user can't see this result. In this same reply: first write the user_message exactly as "
            "given (it is a markdown link), then immediately call poll_connection (don't end your turn): it "
            "waits until they've signed in. Their password is never shared with you. If it returns "
            "'pending', call it again. Once 'connected', retry the original tool."
        )
        return out

    async def poll(self, user: str, domain: str, wait_s: float = 0.0, opened: bool = False) -> dict[str, Any]:
        """Report the latest sign-in. With wait_s, hold until it finishes (or wait_s passes) before answering.

        `opened`: the client confirmed the user was sent to the link (URL elicitation), so wait the full time.
        """
        conn = self._latest.get((user, domain))
        if conn is None:
            return self.status(user, domain)
        if opened:
            conn.page_opened = True
        started = time.monotonic()
        wait_s = max(0.0, min(wait_s, MAX_POLL_WAIT_S))
        while True:
            conn.refresh()
            limit = wait_s if conn.page_opened else min(wait_s, UNOPENED_POLL_WAIT_S)
            if conn.status != "pending" or time.monotonic() - started >= limit:
                break
            await asyncio.sleep(0.25)
        out = conn.public(self.public_base_url)
        out["user_message"] = _poll_message(conn, out.get("url"))
        out["hint"] = {
            "connected": "connected: call the original tool again",
            "pending": "still waiting for the user to sign in; call poll_connection again",
            "expired": "call start_connection again for a new link",
        }[conn.status]
        out["link_opened"] = conn.page_opened
        if conn.status == "pending" and not conn.page_opened:
            out["hint"] = (
                "The user hasn't opened the sign-in link yet, probably because they haven't seen it: they "
                "can't see tool results. Write the user_message (with the link) in your reply to them now, "
                "then call poll_connection again."
            )
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
        states = {p["status"] for p in providers}
        if not providers:
            message = f"{domain} doesn't need you to sign in."
        elif "connected" in states:
            message = f"You're signed in to {domain}."
        elif "pending" in states:
            message = f"Waiting for you to finish signing in to {domain}."
        elif "expired" in states:
            message = f"Your sign-in to {domain} has expired, so you'll need to sign in again."
        else:
            message = f"You're not signed in to {domain}."
        return {
            "domain": domain,
            "login_needed": bool(card.connect_providers),
            "user_message": message,
            "providers": providers,
        }

    def mark_opened(self, token: str) -> None:
        conn = self._by_token.get(token)
        if conn is not None:
            conn.page_opened = True

    def by_token(self, token: str) -> Connection | None:
        conn = self._by_token.get(token)
        if conn is not None:
            conn.refresh()
        return conn

    def pending_for(self, user: str) -> list[dict[str, Any]]:
        """Sign-ins waiting for this user (the latest link per site), for their account page."""
        out = []
        for (owner, domain), conn in sorted(self._latest.items()):
            conn.refresh()
            if owner != user or conn.status != "pending":
                continue
            out.append(
                {
                    "domain": domain,
                    "url": f"{self.public_base_url}/connect/{conn.token}",
                    "minutes_left": max(1, int((conn.expires_at - time.time()) // 60)),
                }
            )
        return out

    async def _verify(
        self, conn: Connection, jar: dict[str, str], identity: BrowserIdentity | None = None
    ) -> ResultCode | None:
        card = self._card(conn.domain)
        if card is None:
            return R.ERROR
        return await self.executor.check_session(
            card, conn.provider, conn.user, cookies=jar, identity=identity
        )

    async def _login_identity(self, conn: Connection) -> BrowserIdentity | None:
        """The login browser's device identity, so later requests present the same browser (DECISIONS.md
        L8)."""
        if self.neko is None:
            return None
        try:
            return await self.neko.identity(conn.token)
        except (
            Exception
        ) as e:  # DevTools briefly unavailable: the sign-in still works, with the default identity
            log.warning("could not read the login browser's identity: %s", e)
            return None

    def _connected(
        self, conn: Connection, jar: dict[str, str], via: str, identity: BrowserIdentity | None = None
    ) -> None:
        self.store.put(conn.user, conn.domain, conn.provider, jar, identity)
        conn.status, conn.message = "connected", f"connected to {conn.domain}"
        shown = (
            f"Chrome {identity.major} on {identity.platform}" if identity is not None else "default identity"
        )
        log.info("%s connected to %s via %s (%d cookies, %s)", conn.user, conn.domain, via, len(jar), shown)
        for hook in self.on_connected:
            try:
                hook(conn.user, conn.domain, conn.provider)
            except Exception:
                log.exception("a sign-in hook failed")

    # ---- "log in here": a Neko login browser ------------------------------

    async def start_browser_login(self, token: str) -> dict[str, Any]:
        """Start (or reuse) the login browser for this link. Raises ConnectError.

        Returns {"viewer": path, "confirm": bool}: confirm=True means the user must click "I've signed in"
        because the site's session_check can't tell signed-in from signed-out.
        """
        conn = self.by_token(token)
        if conn is None:
            raise ConnectError("unknown login link")
        if conn.status != "pending":
            raise ConnectError(f"this link is {conn.status}")
        if self.neko is None:
            raise ConnectError("signing in is not available on this server right now")
        try:
            proxy = self.executor.proxy_for(conn.user, conn.domain, conn.provider, conn.base_url)
            session = await self.neko.launch(conn.token, conn.login_url, [conn.base_url], proxy)
        except NekoUnavailable as e:
            log.warning("login browser unavailable: %s", e)
            raise ConnectError(f"the login browser could not start ({e})") from None
        if conn.check_is_weak is None:
            conn.check_is_weak = await self._verify(conn, {}) is None
            if conn.check_is_weak:
                log.info(
                    "session_check for %s passes without cookies; asking the user to confirm", conn.domain
                )
        if conn.browser_task is None:
            conn.browser_task = asyncio.get_running_loop().create_task(self._watch_browser(conn))
        return {"viewer": session.viewer_path(), "confirm": conn.check_is_weak}

    async def confirm_browser_login(self, token: str) -> Connection:
        """The user clicked "I've signed in". Raises ConnectError with a user-facing reason."""
        conn = self.by_token(token)
        if conn is None:
            raise ConnectError("unknown login link")
        if conn.status != "pending":
            raise ConnectError(f"this link is {conn.status}")
        if self.neko is None or self.neko.get(conn.token) is None:
            raise ConnectError("start the sign-in first")
        try:
            jar = await self.neko.cookies(conn.token, conn.base_url)
        except Exception:
            raise ConnectError("the sign-in browser isn't responding; try again in a moment") from None
        not_yet = (
            f"It doesn't look like you've signed in to {conn.domain} yet. Finish signing in, then try again."
        )
        if not jar:
            raise ConnectError(not_yet)
        # A weak check can't verify the login, so at least require that signing in changed the cookies.
        if conn.check_is_weak and conn.first_cookies is not None and jar == conn.first_cookies:
            raise ConnectError(not_yet)
        identity = await self._login_identity(conn)
        if not conn.check_is_weak and await self._verify(conn, jar, identity) is not None:
            raise ConnectError(not_yet)
        self._connected(conn, jar, "the login browser (confirmed)", identity)
        await self._stop_browser(conn)
        return conn

    async def _watch_browser(self, conn: Connection) -> None:
        assert self.neko is not None
        try:
            while True:
                await asyncio.sleep(self.check_every_s)
                conn.refresh()
                if conn.status != "pending":
                    return
                try:
                    jar = await self.neko.cookies(conn.token, conn.base_url)
                except Exception as e:  # container restarting, DevTools briefly unavailable...
                    log.debug("reading login browser cookies failed: %s", e)
                    continue
                if conn.first_cookies is None:
                    conn.first_cookies = dict(jar)
                if conn.check_is_weak:
                    continue  # can't tell signed-in from signed-out: wait for "I've signed in"
                if not jar:
                    continue
                if conn.identity is None:
                    conn.identity = await self._login_identity(conn)
                if await self._verify(conn, jar, conn.identity) is None:
                    self._connected(conn, jar, "the login browser", conn.identity)
                    return
        finally:
            await self.neko.stop(conn.token)

    async def _stop_browser(self, conn: Connection) -> None:
        if conn.browser_task is not None and not conn.browser_task.done():
            conn.browser_task.cancel()
        if self.neko is not None:
            await self.neko.stop(conn.token)

    async def close(self) -> None:
        for conn in list(self._by_token.values()):
            await self._stop_browser(conn)
        if self.neko is not None:
            await self.neko.close()

    # ---- disconnect --------------------------------------------------------

    async def disconnect(self, user: str, domain: str, provider: str | None = None) -> dict[str, Any]:
        """Forget the user's login for a site: the encrypted session, the server-side browser profile and any
        pending login link. `provider` None = every provider of the site."""
        card = self._card(domain)
        if card is None:
            return {"error": f"unknown domain {domain!r}"}
        if provider is not None and card.provider(provider) is None:
            return {"error": f"{domain} has no provider {provider!r}"}
        ids = [provider] if provider is not None else [p.id for p in card.connect_providers]
        results = []
        for pid in ids:
            record = self.store.record(user, domain, pid)
            self.store.delete(user, domain, pid)
            profile_deleted = False
            if self.pool is not None:
                profile_deleted = await self.pool.delete_profile(ProfileKey(user, domain, pid))
            results.append(
                {
                    "provider": pid,
                    "was_connected": record is not None and record.status == "connected",
                    "session_deleted": record is not None,
                    "browser_profile_deleted": profile_deleted,
                }
            )
        conn = self._latest.get((user, domain))
        if conn is not None and conn.provider in ids and conn.status != "expired":
            conn.status, conn.message = "expired", "disconnected by the user"
            await self._stop_browser(conn)
        log.info("%s disconnected from %s (%s)", user, domain, ", ".join(ids) or "no providers")
        was_connected = any(r["was_connected"] for r in results)
        return {
            "domain": domain,
            "disconnected": True,
            "user_message": (
                f"You're signed out of {domain}. To use it again, you'll need to sign in."
                if was_connected
                else f"You weren't signed in to {domain}, so there was nothing to sign out of."
            ),
            "next_step": "tools for this site will ask the user to sign in again (AUTH_REQUIRED)",
            "details": {"providers": results},  # for the agent's logic, not for the user
        }

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


def _poll_message(conn: Connection, url: str | None) -> str:
    """The plain-language line the agent shows the user about this sign-in."""
    if conn.status == "connected":
        return f"You're signed in to {conn.domain}."
    if conn.status == "expired":
        return f"That sign-in link for {conn.domain} has expired. I can get you a new one."
    minutes = max(1, int((conn.expires_at - time.time()) // 60))
    return f"**[Sign in to {conn.domain} →]({url})** (the link works for {minutes} more minutes)"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
