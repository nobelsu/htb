"""User sessions (cookies per connected provider). The login flow that fills them is TODO."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from runtime.identity import BrowserIdentity

Cookies = dict[str, str]


class SessionStore(Protocol):
    def get(self, user: str, domain: str, provider_id: str) -> Cookies | None:
        """The user's stored cookies for this provider, or None if not connected."""
        ...


class InMemorySessionStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str, str], Cookies] = {}
        self._identities: dict[tuple[str, str, str], BrowserIdentity] = {}

    def put(
        self, user: str, domain: str, provider_id: str, cookies: Cookies, identity: BrowserIdentity | None = None
    ) -> None:
        self._data[(user, domain, provider_id)] = dict(cookies)
        if identity is not None:
            self._identities[(user, domain, provider_id)] = identity
        else:
            self._identities.pop((user, domain, provider_id), None)

    def identity(self, user: str, domain: str, provider_id: str) -> BrowserIdentity | None:
        return self._identities.get((user, domain, provider_id))

    def get(self, user: str, domain: str, provider_id: str) -> Cookies | None:
        found = self._data.get((user, domain, provider_id))
        return dict(found) if found is not None else None

    def refresh(self, user: str, domain: str, provider_id: str, before: Cookies, after: Cookies) -> bool:
        """Cookie rotation: replace the cookies only if they are still `before`. Returns whether it did."""
        key = (user, domain, provider_id)
        if self._data.get(key) != before:
            return False
        self._data[key] = dict(after)
        return True


class DevEnvSessionStore(InMemorySessionStore):
    """Local dev only: cookies pasted into DEV_SESSION_COOKIES, for every user.

    Format: {"<domain>": {"<provider_id>": {"<cookie>": "<value>"}}}
    """

    def __init__(self, raw_json: str | None) -> None:
        super().__init__()
        self._shared: dict[tuple[str, str], Cookies] = {}
        if raw_json and raw_json.strip():
            data = json.loads(raw_json)
            for domain, providers in data.items():
                for provider_id, cookies in providers.items():
                    self._shared[(domain, provider_id)] = {str(k): str(v) for k, v in cookies.items()}

    def get(self, user: str, domain: str, provider_id: str) -> Cookies | None:
        own = super().get(user, domain, provider_id)
        if own is not None:
            return own
        shared = self._shared.get((domain, provider_id))
        return dict(shared) if shared is not None else None

    def refresh(self, user: str, domain: str, provider_id: str, before: Cookies, after: Cookies) -> bool:
        return False  # sign-ins pasted into .env stay as written (DECISIONS.md, cookie rotation)


# TODO(login): a persistent store filled by start_connection / poll_connection (remote browser login),
# plus session_check before use.
