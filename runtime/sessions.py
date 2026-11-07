"""User sessions (cookies per connected provider). The login flow that fills them is TODO."""

from __future__ import annotations

import json
from typing import Protocol

Cookies = dict[str, str]


class SessionStore(Protocol):
    def get(self, user: str, domain: str, provider_id: str) -> Cookies | None:
        """The user's stored cookies for this provider, or None if not connected."""
        ...


class InMemorySessionStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str, str], Cookies] = {}

    def put(self, user: str, domain: str, provider_id: str, cookies: Cookies) -> None:
        self._data[(user, domain, provider_id)] = dict(cookies)

    def get(self, user: str, domain: str, provider_id: str) -> Cookies | None:
        found = self._data.get((user, domain, provider_id))
        return dict(found) if found is not None else None


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


# TODO(login): a persistent store filled by start_connection / poll_connection (remote browser login),
# plus session_check before use.
