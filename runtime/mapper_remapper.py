"""The heal adapter: the runtime's `Remapper`, done by WeiWei's mapper.

When a heal job runs, this re-records the broken actions by replaying their saved recordings
(`captures/<domain>/<action>/`) in a headless browser, then re-maps the whole site from every recording and
returns the new card. The runtime's HealManager then decides, action by action, what goes live.

Decisions (DECISIONS.md, "Heal adapter"): only the broken actions are re-recorded (faster); the replay uses the
user's current sign-in from our encrypted store, else the sign-in saved inside the recording.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from runtime.healing import RemapError
from runtime.sessions import Cookies, SessionStore
from schema import Card

log = logging.getLogger("runtime.mapper_remapper")
DEFAULT_USER = "default"

# mapper.capture.replay.recapture(domain_dir, *, task, state, headless) -> (plans, warnings)
Recapture = Callable[..., tuple[list[Any], list[str]]]
# mapper.pipeline.map_domain(captures_dir) -> DomainResult (.card, .validation.ok, .validation.errors)
MapDomain = Callable[[Path], Any]


class MapperRemapper:
    def __init__(
        self,
        captures_dir: str | Path = "captures",
        *,
        sessions: SessionStore | None = None,
        user: str = DEFAULT_USER,
        headless: bool = True,
        recapture: Recapture | None = None,
        map_domain: MapDomain | None = None,
    ) -> None:
        self.captures_dir = Path(captures_dir)
        self.sessions = sessions
        self.user = user
        self.headless = headless
        self._recapture = recapture
        self._map_domain = map_domain
        self._lock = asyncio.Lock()  # one re-record at a time: the mapper stages bundles in a shared folder

    async def remap(
        self, card: Card, actions: list[str], reason: str, user: str | None = None
    ) -> dict[str, Any]:
        domain_dir = self.captures_dir / card.domain
        if not domain_dir.is_dir():
            raise RemapError(f"no recordings for {card.domain} under {self.captures_dir}")
        missing = [a for a in actions if not (domain_dir / a).is_dir()]
        if missing:
            raise RemapError(f"no recording for {', '.join(missing)} under {domain_dir}")
        sign_ins = {a: self._sign_in(card, a, user or self.user) for a in actions}
        log.info("re-recording %s %s with the mapper (%s)", card.domain, ", ".join(actions), reason)
        async with self._lock:
            try:
                return await asyncio.to_thread(self._heal, card, domain_dir, actions, sign_ins)
            except RemapError:
                raise
            except Exception as e:  # the mapper's own errors (goal not found, replay failed...)
                raise RemapError(f"{type(e).__name__}: {e}") from e

    def _sign_in(self, card: Card, action_name: str, user: str) -> Cookies | None:
        action = card.action(action_name)
        provider = action.effective_auth_provider(card.defaults) if action is not None else None
        if provider is None or self.sessions is None:
            return None
        return self.sessions.get(user, card.domain, provider)

    def _heal(
        self, card: Card, domain_dir: Path, actions: list[str], sign_ins: dict[str, Cookies | None]
    ) -> dict[str, Any]:
        recapture, map_domain = self._mapper()
        with tempfile.TemporaryDirectory(prefix="spider-heal-") as tmp:  # private (0700), removed afterwards
            for name in actions:
                cookies = sign_ins.get(name)
                state = _write_storage_state(Path(tmp) / f"{name}.state.json", card.base_url, cookies) if cookies else None
                _, warnings = recapture(domain_dir, task=name, state=state, headless=self.headless)
                for warning in warnings:
                    log.info("replay %s: %s", card.domain, warning)
        result = map_domain(domain_dir)
        if not result.validation.ok:
            errors = "; ".join(str(e) for e in result.validation.errors[:3]) or "live validation failed"
            raise RemapError(f"re-mapped card is invalid: {errors}")
        new_card: dict[str, Any] = result.card
        return new_card

    def _mapper(self) -> tuple[Recapture, MapDomain]:
        if self._recapture is not None and self._map_domain is not None:
            return self._recapture, self._map_domain
        from mapper.capture.replay import recapture
        from mapper.pipeline import map_domain

        return self._recapture or recapture, self._map_domain or map_domain


def _write_storage_state(path: Path, base_url: str, cookies: Cookies) -> Path:
    """The user's sign-in as a Playwright storage state, readable only by us. Values are never logged."""
    url = urlsplit(base_url)
    common = {"domain": url.hostname or "", "path": "/", "expires": -1, "httpOnly": False,
              "secure": url.scheme == "https", "sameSite": "Lax"}
    state = {"cookies": [{"name": k, "value": v, **common} for k, v in cookies.items()], "origins": []}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    return path
