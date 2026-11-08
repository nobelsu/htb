"""HMAC confirmation ids for actions with `requires_confirmation` (CARD_SPEC.md "Confirmation")."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any

from schema.envelope import Confirmation

WINDOW_S = 120


def canonical_params(params: dict[str, Any]) -> str:
    """Params after defaults, without `_`-prefixed keys, as sorted compact JSON."""
    clean = {k: v for k, v in params.items() if not k.startswith("_")}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Confirmer:
    def __init__(self, secret: str, window_s: int = WINDOW_S) -> None:
        if not secret:
            raise ValueError("CONFIRMATION_SECRET must be set")
        self._key = secret.encode()
        self.window_s = window_s

    def _mac(self, user: str, domain: str, action: str, canonical: str, expires_at: int) -> str:
        msg = "|".join([user, domain, action, canonical, str(expires_at)]).encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def issue(
        self,
        user: str,
        domain: str,
        action: str,
        params: dict[str, Any],
        summary: str,
        now: float | None = None,
    ) -> Confirmation:
        expires_at = int(now if now is not None else time.time()) + self.window_s
        mac = self._mac(user, domain, action, canonical_params(params), expires_at)
        token = base64.urlsafe_b64encode(f"{expires_at}.{mac}".encode()).decode().rstrip("=")
        return Confirmation(id=f"c_{token}", expires_at=_iso(expires_at), summary=summary)

    def check(
        self,
        conf_id: str,
        user: str,
        domain: str,
        action: str,
        params: dict[str, Any],
        now: float | None = None,
    ) -> str | None:
        """Return None if valid, otherwise the reason it is not."""
        try:
            if not conf_id.startswith("c_"):
                raise ValueError
            raw = conf_id[2:]
            decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
            exp_text, mac = decoded.split(".", 1)
            expires_at = int(exp_text)
        except ValueError:
            return "malformed confirmation id"
        expected = self._mac(user, domain, action, canonical_params(params), expires_at)
        if not hmac.compare_digest(mac, expected):
            return "confirmation id does not match this action and these params"
        if (now if now is not None else time.time()) > expires_at:
            return "confirmation id has expired; call again without it to get a new one"
        return None
