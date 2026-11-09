"""OAuth for apps that can't send our API key (regular Claude: web, desktop, phone). The MCP SDK serves the
OAuth endpoints (/.well-known/..., /register, /authorize, /token, /revoke); this is the provider behind them.
 Flow: Claude registers itself (Dynamic Client Registration), sends the person to /authorize, which we
redirect to our sign-in page (/oauth/login?request=...). Once they sign in (password, API key, or a new
account with the invite code), we redirect back to Claude with a one-time code, which Claude exchanges for an
access token (1 hour) and a refresh token. Refresh tokens rotate on every use and a sign-in lasts 30 days at
most (DECISIONS.md L11).

Tokens, codes and account-page sessions are stored as SHA-256 hashes only.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    IdentityAssertionParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

ACCESS_TTL_S = 3600
SIGN_IN_MAX_AGE_S = 30 * 86400  # how long Claude stays connected before signing in again (asked)
CODE_TTL_S = 300
REQUEST_TTL_S = 900  # time to finish the sign-in page
ACCOUNT_SESSION_TTL_S = 30 * 86400

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id   TEXT PRIMARY KEY,
    info        TEXT NOT NULL,          -- OAuthClientInformationFull JSON (client name, redirect URIs, ...)
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash   TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL,
    user        TEXT NOT NULL,
    params      TEXT NOT NULL,          -- AuthorizationParams JSON
    expires_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash  TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,          -- access | refresh
    client_id   TEXT NOT NULL,
    user        TEXT NOT NULL,
    scopes      TEXT NOT NULL,
    resource    TEXT,
    expires_at  REAL NOT NULL,
    family      TEXT NOT NULL,          -- one per sign-in; rotation keeps it, revoking ends all of it
    signed_in   REAL NOT NULL,          -- when the person signed in (the family's start)
    revoked     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS oauth_tokens_user ON oauth_tokens (user, client_id);
CREATE TABLE IF NOT EXISTS account_sessions (
    token_hash  TEXT PRIMARY KEY,
    user        TEXT NOT NULL,
    expires_at  REAL NOT NULL
);
"""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class PendingSignIn:
    """An /authorize request waiting for the person to sign in on our page."""

    id: str
    client: OAuthClientInformationFull
    params: AuthorizationParams
    expires_at: float

    @property
    def app_name(self) -> str:
        return self.client.client_name or "An app"


class OAuth:
    def __init__(self, path: str | Path, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA_SQL)
        self._pending: dict[str, PendingSignIn] = {}

    def close(self) -> None:
        self._conn.close()

    # ---- clients (Dynamic Client Registration) -------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self._conn.execute(
            "SELECT info FROM oauth_clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO oauth_clients (client_id, info, created_at) VALUES (?, ?, ?)",
                (client_info.client_id, client_info.model_dump_json(), time.time()),
            )

    # ---- authorization: our sign-in page in the middle ------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self._forget_old_requests()
        request = PendingSignIn(secrets.token_urlsafe(24), client, params, time.time() + REQUEST_TTL_S)
        self._pending[request.id] = request
        return f"{self.base_url}/oauth/login?request={request.id}"

    def pending(self, request_id: str) -> PendingSignIn | None:
        self._forget_old_requests()
        return self._pending.get(request_id)

    def approve(self, request_id: str, user: str) -> str:
        """The person signed in: a one-time code for the app, and where to send their browser."""
        request = self._pending.pop(request_id, None)
        if request is None or request.expires_at < time.time():
            raise AuthorizeError("access_denied", "this sign-in request expired; start again from the app")
        code = secrets.token_urlsafe(32)
        with self._lock:
            self._conn.execute(
                "INSERT INTO oauth_codes (code_hash, client_id, user, params, expires_at) VALUES (?, ?, ?, "
                "?, ?)",
                (
                    _hash(code),
                    request.client.client_id,
                    user,
                    request.params.model_dump_json(),
                    time.time() + CODE_TTL_S,
                ),
            )
        return construct_redirect_uri(str(request.params.redirect_uri), code=code, state=request.params.state)

    def deny(self, request_id: str) -> str | None:
        request = self._pending.pop(request_id, None)
        if request is None:
            return None
        return construct_redirect_uri(
            str(request.params.redirect_uri), error="access_denied", state=request.params.state
        )

    def _forget_old_requests(self) -> None:
        now = time.time()
        for rid in [r for r, p in self._pending.items() if p.expires_at < now]:
            self._pending.pop(rid, None)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = self._conn.execute(
            "SELECT client_id, user, params, expires_at FROM oauth_codes WHERE code_hash = ?",
            (_hash(authorization_code),),
        ).fetchone()
        if row is None or row[0] != client.client_id:
            return None
        params = AuthorizationParams.model_validate_json(row[2])
        return AuthorizationCode(
            code=authorization_code,
            scopes=params.scopes or [],
            expires_at=float(row[3]),
            client_id=row[0],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=row[1],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM oauth_codes WHERE code_hash = ?", (_hash(authorization_code.code),)
            )
        if cur.rowcount == 0:  # used already (one-time)
            raise TokenError("invalid_grant", "authorization code already used")
        assert authorization_code.subject is not None
        return self._issue(
            client.client_id,
            authorization_code.subject,
            authorization_code.scopes,
            authorization_code.resource,
            family=secrets.token_hex(12),
            signed_in=time.time(),
        )

    # ---- tokens ----------------------------------------------------------------------------------------

    def _issue(
        self,
        client_id: str,
        user: str,
        scopes: list[str],
        resource: str | None,
        *,
        family: str,
        signed_in: float,
    ) -> OAuthToken:
        now = time.time()
        access, refresh = "spat_" + secrets.token_urlsafe(32), "sprt_" + secrets.token_urlsafe(32)
        rows = [
            (_hash(access), "access", now + ACCESS_TTL_S),
            (_hash(refresh), "refresh", signed_in + SIGN_IN_MAX_AGE_S),
        ]
        with self._lock:
            for token_hash, kind, expires in rows:
                self._conn.execute(
                    "INSERT INTO oauth_tokens (token_hash, kind, client_id, user, scopes, resource, "
                    "expires_at, family, "
                    "signed_in) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        token_hash,
                        kind,
                        client_id,
                        user,
                        json.dumps(scopes),
                        resource,
                        expires,
                        family,
                        signed_in,
                    ),
                )
        return OAuthToken(
            access_token=access,
            expires_in=ACCESS_TTL_S,
            refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )

    def _token(self, token: str, kind: str) -> sqlite3.Row | None:
        cur = self._conn.cursor()
        cur.row_factory = sqlite3.Row
        row: sqlite3.Row | None = cur.execute(
            "SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = ? AND revoked = 0 AND expires_at > ?",
            (_hash(token), kind, time.time()),
        ).fetchone()
        return row

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = self._token(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            expires_at=int(row["expires_at"]),
            resource=row["resource"],
            subject=row["user"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        row = self._token(refresh_token.token, "refresh")
        if row is None:
            raise TokenError("invalid_grant", "refresh token is no longer valid")
        with self._lock:  # rotate: the old refresh token is spent
            self._conn.execute(
                "UPDATE oauth_tokens SET revoked = 1 WHERE token_hash = ?", (_hash(refresh_token.token),)
            )
        return self._issue(
            client.client_id,
            row["user"],
            scopes or json.loads(row["scopes"]),
            row["resource"],
            family=row["family"],
            signed_in=float(row["signed_in"]),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self._token(token, "access")
        if row is None:
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            expires_at=int(row["expires_at"]),
            resource=row["resource"],
            subject=row["user"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoking either token ends the whole sign-in (its access and refresh tokens)."""
        row = self._conn.execute(
            "SELECT family FROM oauth_tokens WHERE token_hash = ?", (_hash(token.token),)
        ).fetchone()
        if row is not None:
            with self._lock:
                self._conn.execute("UPDATE oauth_tokens SET revoked = 1 WHERE family = ?", (row[0],))

    async def exchange_identity_assertion(
        self, client: OAuthClientInformationFull, params: IdentityAssertionParams
    ) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "enterprise identity assertions aren't supported")

    def user_for_access_token(self, token: str) -> str | None:
        row = self._token(token, "access")
        return str(row["user"]) if row is not None else None

    # ---- the account page: connected apps --------------------------------------------------------------

    def apps(self, user: str) -> list[dict[str, Any]]:
        """Apps signed in as this user (a live refresh token), one entry per app."""
        rows = self._conn.execute(
            "SELECT t.client_id, min(t.signed_in), max(t.expires_at), c.info FROM oauth_tokens t LEFT JOIN "
            "oauth_clients c ON c.client_id = t.client_id WHERE t.user = ? AND t.kind = 'refresh' AND "
            "t.revoked = 0 AND t.expires_at > ? GROUP BY t.client_id ORDER BY min(t.signed_in) DESC",
            (user, time.time()),
        ).fetchall()
        out = []
        for client_id, signed_in, expires, info in rows:
            client = OAuthClientInformationFull.model_validate_json(info) if info else None
            name = client.client_name if client else None
            out.append(
                {
                    "client_id": client_id,
                    "name": name or "An app",
                    "where": _where(client),
                    "signed_in": signed_in,
                    "expires_at": expires,
                }
            )
        return out

    def revoke_app(self, user: str, client_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE oauth_tokens SET revoked = 1 WHERE user = ? AND client_id = ? AND revoked = 0",
                (user, client_id),
            )
        return cur.rowcount

    # ---- account-page sessions (a cookie) ---------------------------------------------------------------

    def new_session(self, user: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._conn.execute("DELETE FROM account_sessions WHERE expires_at < ?", (time.time(),))
            self._conn.execute(
                "INSERT INTO account_sessions (token_hash, user, expires_at) VALUES (?, ?, ?)",
                (_hash(token), user, time.time() + ACCOUNT_SESSION_TTL_S),
            )
        return token

    def session_user(self, token: str | None) -> str | None:
        if not token:
            return None
        row = self._conn.execute(
            "SELECT user FROM account_sessions WHERE token_hash = ? AND expires_at > ?",
            (_hash(token), time.time()),
        ).fetchone()
        return str(row[0]) if row else None

    def end_session(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._conn.execute("DELETE FROM account_sessions WHERE token_hash = ?", (_hash(token),))


def _where(client: OAuthClientInformationFull | None) -> str:
    """Where an app runs, from the address it returns to after signing in: the website's name (claude.ai), or
    "this computer" for an app that listens on localhost (Claude Code, the desktop app)."""
    uris = [str(u) for u in (client.redirect_uris or [])] if client else []
    host = urlsplit(uris[0]).hostname if uris else None
    if not host:
        return ""
    return "this computer" if host in ("localhost", "127.0.0.1", "::1") else host
