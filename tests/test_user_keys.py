"""Several users: each API key belongs to a user, and every tool call acts as the caller's user. People get a key
from the sign-up page (with the invite code) or from `python -m runtime.keys`; the .env key stays user "default"."""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from runtime.confirm import Confirmer
from runtime.connect import ConnectionManager
from runtime.executor import Executor
from runtime.gateway import Gateway
from runtime.keys import KeyStoreError, KeyStore
from runtime.registry import Registry
from runtime.session_store import EncryptedSessionStore, new_session_key
from schema import ResultCode
from tests.conftest import LiveServer
from tests.fixture_site.app import CSRF, VALID_SID
from tests.test_gateway import API_KEY, act, envelope
from tests.test_login_confirm import FakeNeko

DOMAIN = "fixture.test"
SIGNED_IN = {"sid": VALID_SID, "csrftoken": CSRF}
CODE = "judges-2026"


# ---- the key store ----------------------------------------------------------------------------------------


def test_keys_identify_their_user_and_are_stored_hashed(tmp_path: Path) -> None:
    keys = KeyStore(tmp_path / "k.db")
    key = keys.create("alice", "alice@example.com")
    assert key.startswith("htb_") and len(key) > 40
    assert keys.user_for(key) == "alice"
    assert keys.user_for(key + "x") is None and keys.user_for("") is None
    with sqlite3.connect(tmp_path / "k.db") as conn:
        dump = "\n".join(conn.iterdump())
    assert key not in dump  # only a hash is kept
    assert [(u.name, u.email, u.revoked) for u in keys.users()] == [("alice", "alice@example.com", False)]


@pytest.mark.parametrize("name", ["", "a", "Alice", "has space", "x" * 40, "default", "_shared", "-dash"])
def test_names_must_be_simple_and_free(tmp_path: Path, name: str) -> None:
    with pytest.raises(KeyStoreError):
        KeyStore(tmp_path / "k.db").create(name, "a@b.co")


def test_names_are_unique_and_emails_look_like_emails(tmp_path: Path) -> None:
    keys = KeyStore(tmp_path / "k.db")
    keys.create("alice", "alice@example.com")
    with pytest.raises(KeyStoreError, match="taken"):
        keys.create("alice", "other@example.com")
    with pytest.raises(KeyStoreError, match="email"):
        keys.create("bob", "not-an-email")


def test_revoke_and_reissue(tmp_path: Path) -> None:
    keys = KeyStore(tmp_path / "k.db")
    old = keys.create("alice", "alice@example.com")
    new = keys.reissue("alice")
    assert keys.user_for(old) is None and keys.user_for(new) == "alice"
    keys.revoke("alice")
    assert keys.user_for(new) is None and keys.users()[0].revoked
    with pytest.raises(KeyStoreError):
        keys.reissue("nobody")


# ---- every tool call acts as the caller's user --------------------------------------------------------------


@pytest.fixture
def multi_gateway(tmp_path: Path, fixture_card: dict[str, Any]) -> Iterator[tuple[str, KeyStore, EncryptedSessionStore]]:
    db = tmp_path / "gw.db"
    registry = Registry(db)
    registry.save(fixture_card)
    store = EncryptedSessionStore(db, new_session_key())
    executor = Executor(store, Confirmer("s"))
    manager = ConnectionManager(registry, store, executor, "http://placeholder", neko=FakeNeko())  # type: ignore[arg-type]
    gateway = Gateway(registry, executor, poll_interval_s=0.05, connections=manager)
    keys = KeyStore(db)
    with LiveServer(gateway.asgi_app(API_KEY, keys=keys, signup_code=CODE)) as server:
        manager.public_base_url = server.url
        yield server.url, keys, store
    registry.close()


@asynccontextmanager
async def connect_as(base: str, key: str) -> AsyncIterator[Client]:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=30)
    async with http:
        async with Client(streamable_http_client(f"{base}/mcp", http_client=http), cache=None) as client:  # type: ignore[arg-type]
            yield client


def provider(fixture_card: dict[str, Any]) -> str:
    return str(fixture_card["connect_providers"][0]["id"])


async def test_each_key_acts_as_its_own_user(
    multi_gateway: tuple[str, KeyStore, EncryptedSessionStore], fixture_card: dict[str, Any]
) -> None:
    base, keys, store = multi_gateway
    alice, bob = keys.create("alice", "a@example.com"), keys.create("bob", "b@example.com")
    store.put("alice", DOMAIN, provider(fixture_card), SIGNED_IN)  # only alice is signed in to the shop

    async with connect_as(base, alice) as client:
        assert envelope(await client.call_tool(*act("list_orders")))["status"] == ResultCode.SUCCESS.value
        status = envelope(await client.call_tool("connection_status", {"domain": DOMAIN}))
        assert status["providers"][0]["status"] == "connected"
    async with connect_as(base, bob) as client:
        assert envelope(await client.call_tool(*act("list_orders")))["status"] == ResultCode.AUTH_REQUIRED.value
        status = envelope(await client.call_tool("connection_status", {"domain": DOMAIN}))
        assert status["providers"][0]["status"] == "not_connected"
        await client.call_tool("disconnect", {"domain": DOMAIN})  # bob disconnecting doesn't touch alice
        started = envelope(await client.call_tool("start_connection", {"domain": DOMAIN}))
    alices = store.get("alice", DOMAIN, provider(fixture_card))
    assert alices is not None and alices["sid"] == VALID_SID  # still signed in (plus the cookie her run rotated in)
    async with connect_as(base, API_KEY) as client:  # the .env key is user "default"
        assert envelope(await client.call_tool(*act("list_orders")))["status"] == ResultCode.AUTH_REQUIRED.value
    # bob's sign-in link signs bob in, not anyone else
    token = started["url"].rsplit("/", 1)[1]
    async with httpx.AsyncClient() as http:
        assert (await http.get(f"{base}/connect/{token}/status")).json()["status"] == "pending"


async def test_unknown_and_revoked_keys_are_refused(multi_gateway: tuple[str, KeyStore, EncryptedSessionStore]) -> None:
    base, keys, _ = multi_gateway
    key = keys.create("carol", "c@example.com")
    async with httpx.AsyncClient() as http:
        async def status(k: str) -> int:
            return (await http.post(f"{base}/mcp", headers={"Authorization": f"Bearer {k}"}, json={})).status_code

        assert await status("htb_made_up") == 401
        assert await status(key) != 401
        keys.revoke("carol")
        assert await status(key) == 401


# ---- the sign-up page ---------------------------------------------------------------------------------------


def _form(code: str = CODE, name: str = "dave", email: str = "dave@example.com") -> dict[str, str]:
    return {"code": code, "name": name, "email": email}


async def test_sign_up_page_gives_a_working_key(
    multi_gateway: tuple[str, KeyStore, EncryptedSessionStore], caplog: pytest.LogCaptureFixture
) -> None:
    base, keys, _ = multi_gateway
    async with httpx.AsyncClient() as http:
        page = await http.get(f"{base}/signup")
        assert page.status_code == 200 and "invite code" in page.text.lower()
        assert page.headers["cache-control"] == "no-store"
        with caplog.at_level(logging.DEBUG):
            done = await http.post(f"{base}/signup", data=_form())
    assert done.status_code == 200 and done.headers["cache-control"] == "no-store"
    match = re.search(r"htb_[A-Za-z0-9_-]{30,}", done.text)
    assert match is not None
    key = match.group(0)
    assert f"claude mcp add --transport http htb {base}/mcp --header" in done.text.replace("&quot;", '"')
    assert keys.user_for(key) == "dave"
    async with connect_as(base, key) as client:
        assert envelope(await client.call_tool("find_actions", {}))["sites"][0]["site"] == DOMAIN
    for secret in (key, CODE, "dave@example.com"):
        assert secret not in caplog.text


@pytest.mark.parametrize(
    ("form", "status", "message"),
    [
        (_form(code="wrong"), 403, "invite code"),
        (_form(name="Not OK"), 400, "name"),
        (_form(email="nope"), 400, "email"),
    ],
)
async def test_sign_up_rejects_bad_input(
    multi_gateway: tuple[str, KeyStore, EncryptedSessionStore], form: dict[str, str], status: int, message: str
) -> None:
    base, keys, _ = multi_gateway
    async with httpx.AsyncClient() as http:
        resp = await http.post(f"{base}/signup", data=form)
    assert resp.status_code == status and message in resp.text.lower()
    assert keys.users() == [] and "htb_" not in resp.text


async def test_sign_up_name_taken(multi_gateway: tuple[str, KeyStore, EncryptedSessionStore]) -> None:
    base, keys, _ = multi_gateway
    keys.create("dave", "d@example.com")
    async with httpx.AsyncClient() as http:
        resp = await http.post(f"{base}/signup", data=_form())
    assert resp.status_code == 409 and "taken" in resp.text


async def test_guessing_the_code_is_slowed_down(multi_gateway: tuple[str, KeyStore, EncryptedSessionStore]) -> None:
    base, keys, _ = multi_gateway
    async with httpx.AsyncClient() as http:
        codes = [(await http.post(f"{base}/signup", data=_form(code=f"guess{i}"))).status_code for i in range(12)]
        assert codes[:10] == [403] * 10 and codes[10:] == [429, 429]
        assert (await http.post(f"{base}/signup", data=_form())).status_code == 429  # even the right code, for a while
    assert keys.users() == []


async def test_sign_up_is_off_without_a_code(tmp_path: Path, fixture_card: dict[str, Any]) -> None:
    registry = Registry(tmp_path / "gw.db")
    registry.save(fixture_card)
    gateway = Gateway(registry, Executor(EncryptedSessionStore(tmp_path / "gw.db", new_session_key()), Confirmer("s")))
    with LiveServer(gateway.asgi_app(API_KEY, keys=KeyStore(tmp_path / "gw.db"), signup_code="")) as server:
        async with httpx.AsyncClient() as http:
            assert (await http.get(f"{server.url}/signup")).status_code == 404
            assert (await http.post(f"{server.url}/signup", data=_form(code=""))).status_code == 404
    registry.close()


# ---- the command line --------------------------------------------------------------------------------------


def test_command_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from runtime.keys import main

    db = str(tmp_path / "k.db")
    assert main(["--db", db, "add", "erin", "--email", "erin@example.com"]) == 0
    key = re.search(r"htb_\S+", capsys.readouterr().out)
    assert key is not None and KeyStore(db).user_for(key.group(0)) == "erin"
    assert main(["--db", db, "list"]) == 0
    listed = capsys.readouterr().out
    assert "erin" in listed and "htb_" not in listed
    assert main(["--db", db, "revoke", "erin"]) == 0
    assert KeyStore(db).user_for(key.group(0)) is None
    assert main(["--db", db, "add", "erin", "--email", "erin@example.com"]) == 1  # name taken
