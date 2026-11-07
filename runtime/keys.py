"""API keys: one per user. A key identifies the user every tool call acts as (their sign-ins, browser profiles,
login links and device identities). Keys are shown once and only their SHA-256 is stored.

People get a key from the gateway's sign-up page (with the invite code, SIGNUP_CODE) or from the command line:

    uv run python -m runtime.keys add alice --email alice@example.com
    uv run python -m runtime.keys list
    uv run python -m runtime.keys reissue alice      # new key, the old one stops working
    uv run python -m runtime.keys revoke alice

The `.env` key (GATEWAY_API_KEY) is not stored here; it is always user "default".
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ENV_KEY_USER = "default"  # the .env key's user
RESERVED = frozenset({ENV_KEY_USER, "_shared", "admin", "root"})
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,31}")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
KEY_PREFIX = "htb_"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS api_users (
    name        TEXT PRIMARY KEY,
    email       TEXT NOT NULL,
    key_hash    TEXT NOT NULL UNIQUE,   -- sha256 of the key; the key itself is never stored
    created_at  TEXT NOT NULL,
    revoked_at  TEXT,
    password    TEXT                    -- scrypt hash (see _hash_password); NULL = key-only account
);
"""


class KeyStoreError(Exception):
    """A user-facing reason (bad name, taken, bad email, unknown user)."""


@dataclass(frozen=True)
class ApiUser:
    name: str
    email: str
    created_at: str
    revoked: bool


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_name(name: str) -> None:
    if not NAME_RE.fullmatch(name) or name in RESERVED:
        raise KeyStoreError(
            "the name must be 2-32 lowercase letters, digits, '-' or '_', starting with a letter or digit"
        )


def check_email(email: str) -> None:
    if not EMAIL_RE.fullmatch(email) or len(email) > 254:
        raise KeyStoreError("that doesn't look like an email address")


class KeyStore:
    def __init__(self, path: str | Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA_SQL)
        if "password" not in {r[1] for r in self._conn.execute("PRAGMA table_info(api_users)")}:
            self._conn.execute("ALTER TABLE api_users ADD COLUMN password TEXT")  # tables made before passwords

    def close(self) -> None:
        self._conn.close()

    def create(self, name: str, email: str, password: str | None = None) -> str:
        """A new user and their key (returned once). With a password they can also sign in on the web pages."""
        check_name(name)
        check_email(email)
        stored = None
        if password is not None:
            check_password(password)
            stored = _hash_password(password)
        if self.find(email) is not None:
            raise KeyStoreError("that email already has an account")
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO api_users (name, email, key_hash, created_at, password) VALUES (?, ?, ?, ?, ?)",
                    (name, email, _hash(key), _now(), stored),
                )
            except sqlite3.IntegrityError:
                raise KeyStoreError(f"the name {name!r} is taken") from None
        return key

    def user_for(self, key: str) -> str | None:
        """The user this key belongs to, or None (unknown or revoked)."""
        if not key.startswith(KEY_PREFIX):
            return None
        row = self._conn.execute(
            "SELECT name FROM api_users WHERE key_hash = ? AND revoked_at IS NULL", (_hash(key),)
        ).fetchone()
        return str(row[0]) if row else None

    def find(self, login: str) -> str | None:
        """The user a name or email belongs to (active accounts only)."""
        row = self._conn.execute(
            "SELECT name FROM api_users WHERE (name = ? OR lower(email) = lower(?)) AND revoked_at IS NULL",
            (login.strip(), login.strip()),
        ).fetchone()
        return str(row[0]) if row else None

    def check_login(self, login: str, password: str) -> str | None:
        """The user if `login` (name or email) and `password` match an active account with a password."""
        name = self.find(login)
        row = self._conn.execute("SELECT password FROM api_users WHERE name = ?", (name,)).fetchone() if name else None
        if row is None or not row[0]:
            _hash_password(password)  # same work either way, so timing doesn't reveal which accounts exist
            return None
        return name if _verify_password(password, str(row[0])) else None

    def set_password(self, name: str, password: str) -> None:
        check_password(password)
        with self._lock:
            cur = self._conn.execute("UPDATE api_users SET password = ? WHERE name = ?", (_hash_password(password), name))
        if cur.rowcount == 0:
            raise KeyStoreError(f"no user {name!r}")

    def reissue(self, name: str) -> str:
        """A new key for an existing user (also un-revokes them); the old key stops working."""
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE api_users SET key_hash = ?, revoked_at = NULL WHERE name = ?", (_hash(key), name)
            )
        if cur.rowcount == 0:
            raise KeyStoreError(f"no user {name!r}")
        return key

    def revoke(self, name: str) -> None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE api_users SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL", (_now(), name)
            )
        if cur.rowcount == 0:
            raise KeyStoreError(f"no active user {name!r}")

    def users(self) -> list[ApiUser]:
        rows = self._conn.execute(
            "SELECT name, email, created_at, revoked_at FROM api_users ORDER BY created_at, name"
        ).fetchall()
        return [ApiUser(r[0], r[1], r[2], r[3] is not None) for r in rows]


MIN_PASSWORD = 8
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD or len(password) > 200:
        raise KeyStoreError(f"the password must be at least {MIN_PASSWORD} characters")


def _hash_password(password: str, salt: bytes | None = None) -> str:
    """scrypt (slow on purpose, unlike API keys: people pick guessable passwords)."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT["n"], r=_SCRYPT["r"], p=_SCRYPT["p"], dklen=_SCRYPT["dklen"])
    return f"scrypt${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    return hmac.compare_digest(_hash_password(password, bytes.fromhex(salt)).split("$")[2], digest)


def mcp_add_command(base_url: str, key: str) -> str:
    """The line a user pastes to connect Claude Code to the gateway."""
    return f'claude mcp add --transport http htb {base_url.rstrip("/")}/mcp --header "Authorization: Bearer {key}"'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m runtime.keys", description="Manage the gateway's API keys")
    parser.add_argument("--db", default=os.environ.get("REGISTRY_DB", "./runtime.db"))
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="create a user and print their key (shown once)")
    add.add_argument("name")
    add.add_argument("--email", required=True)
    sub.add_parser("set-password", help="set a user's web sign-in password (asks for it)").add_argument("name")
    sub.add_parser("list", help="list users (never keys)")
    for cmd, text in (("reissue", "print a new key for a user; the old one stops working"), ("revoke", "turn a user's key off")):
        sub.add_parser(cmd, help=text).add_argument("name")
    args = parser.parse_args(argv)

    store = KeyStore(args.db)
    base = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8765")
    try:
        if args.command == "add":
            key = store.create(args.name, args.email)
            print(f"{args.name}'s key (shown once):\n  {key}\nTo connect Claude Code:\n  {mcp_add_command(base, key)}")
        elif args.command == "reissue":
            key = store.reissue(args.name)
            print(f"{args.name}'s new key (shown once):\n  {key}\nTo connect Claude Code:\n  {mcp_add_command(base, key)}")
        elif args.command == "set-password":
            import getpass

            store.set_password(args.name, getpass.getpass(f"New password for {args.name}: "))
            print(f"{args.name} can now sign in on the web pages with that password")
        elif args.command == "revoke":
            store.revoke(args.name)
            print(f"{args.name}'s key no longer works")
        else:
            for u in store.users():
                print(f"{u.name:24} {u.email:32} {u.created_at}{'  (revoked)' if u.revoked else ''}")
    except KeyStoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(".env")
    sys.exit(main())
