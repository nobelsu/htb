"""Sign-in for apps that can't send our API key (regular Claude), and the account page.

Each test walks the flow regular Claude uses: find the sign-in server from the 401, register (Dynamic Client
Registration), send the person to /authorize -> our sign-in page, swap the one-time code for tokens (PKCE), call
tools as that person, refresh.
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import re
import secrets
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from runtime.confirm import Confirmer
from runtime.connect import ConnectionManager
from runtime.executor import Executor
from runtime.gateway import Gateway
from runtime.keys import KeyStore
from runtime.oauth import OAuth
from runtime.registry import Registry
from runtime.session_store import EncryptedSessionStore, new_session_key
from tests.conftest import LiveServer, free_port
from tests.fixture_site.app import CSRF, VALID_SID
from tests.test_gateway import API_KEY, act, envelope
from tests.test_login_confirm import FakeNeko
from tests.test_user_keys import connect_as

DOMAIN = "fixture.test"
CODE = "judges-2026"
PASSWORD = "alice-password1"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


@dataclass
class Env:
    base: str
    keys: KeyStore
    store: EncryptedSessionStore
    oauth: OAuth
    manager: ConnectionManager
    db: Path
    provider: str


@pytest.fixture
def env(tmp_path: Path, fixture_card: dict[str, Any]) -> Iterator[Env]:
    db = tmp_path / "gw.db"
    registry = Registry(db)
    registry.save(fixture_card)
    store = EncryptedSessionStore(db, new_session_key())
    executor = Executor(store, Confirmer("s"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    manager = ConnectionManager(registry, store, executor, base, neko=FakeNeko())  # type: ignore[arg-type]
    gateway = Gateway(registry, executor, poll_interval_s=0.05, connections=manager)
    keys = KeyStore(db)
    keys.create("alice", "alice@example.com", PASSWORD)
    oauth = OAuth(db, base)
    app = gateway.asgi_app(API_KEY, keys=keys, signup_code=CODE, public_base_url=base, oauth=oauth)
    with LiveServer(app, port=port):
        yield Env(base, keys, store, oauth, manager, db, str(fixture_card["connect_providers"][0]["id"]))
    registry.close()


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@dataclass
class App:
    client_id: str
    metadata: dict[str, Any]


async def register(http: httpx.AsyncClient, base: str) -> App:
    """What Claude does before the person sees anything: discovery from the 401, then registration."""
    unauthorized = await http.post(f"{base}/mcp", json={})
    assert unauthorized.status_code == 401
    challenge = unauthorized.headers["www-authenticate"]
    found = re.search(r'resource_metadata="([^"]+)"', challenge)
    assert found is not None and found.group(1) == f"{base}/.well-known/oauth-protected-resource/mcp"
    resource = (await http.get(found.group(1))).json()
    assert resource["resource"].rstrip("/") == f"{base}/mcp"
    issuer = resource["authorization_servers"][0].rstrip("/")
    metadata = (await http.get(f"{issuer}/.well-known/oauth-authorization-server")).json()
    reg = await http.post(metadata["registration_endpoint"], json={
        "redirect_uris": [CALLBACK], "client_name": "Claude", "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
    })
    assert reg.status_code == 201, reg.text
    return App(reg.json()["client_id"], metadata)


async def start(http: httpx.AsyncClient, app: App, challenge: str, base: str) -> str:
    """/authorize -> our sign-in page. Returns the sign-in request id."""
    resp = await http.get(app.metadata["authorization_endpoint"], params={
        "response_type": "code", "client_id": app.client_id, "redirect_uri": CALLBACK, "state": "st-1",
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": f"{base}/mcp",
    })
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    assert location.startswith(f"{base}/oauth/login?request=")
    return parse_qs(urlsplit(location).query)["request"][0]


async def sign_in(http: httpx.AsyncClient, base: str, request_id: str, **form: str) -> httpx.Response:
    return await http.post(f"{base}/oauth/login", data={"request": request_id, **form})


async def code_from(resp: httpx.Response) -> str:
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    assert location.startswith(CALLBACK)
    query = parse_qs(urlsplit(location).query)
    assert query["state"] == ["st-1"]
    return query["code"][0]


async def tokens(http: httpx.AsyncClient, app: App, code: str, verifier: str) -> httpx.Response:
    return await http.post(app.metadata["token_endpoint"], data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
        "client_id": app.client_id, "code_verifier": verifier,
    })


async def connect_app(env: Env, http: httpx.AsyncClient, **form: str) -> dict[str, Any]:
    app = await register(http, env.base)
    verifier, challenge = pkce()
    request_id = await start(http, app, challenge, env.base)
    page = await http.get(f"{env.base}/oauth/login", params={"request": request_id})
    assert page.status_code == 200 and "Claude" in page.text
    code = await code_from(await sign_in(http, env.base, request_id, **form))
    resp = await tokens(http, app, code, verifier)
    assert resp.status_code == 200, resp.text
    out: dict[str, Any] = resp.json() | {"client_id": app.client_id, "metadata": app.metadata}
    return out


# ---- the flow -----------------------------------------------------------------------------------------------


async def test_regular_claude_connects_and_acts_as_the_person(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    async with httpx.AsyncClient(follow_redirects=False) as http:
        with caplog.at_level(logging.DEBUG, logger="runtime"):
            got = await connect_app(env, http, action="password", login="alice", password=PASSWORD)
    assert got["token_type"] == "Bearer" and got["expires_in"] == 3600 and got["refresh_token"]
    env.store.put("alice", DOMAIN, env.provider, {"sid": VALID_SID, "csrftoken": CSRF})  # only alice is signed in
    async with connect_as(env.base, got["access_token"]) as client:
        assert envelope(await client.call_tool(*act("list_orders")))["status"] == "SUCCESS"
    async with connect_as(env.base, API_KEY) as client:  # user default: not alice
        assert envelope(await client.call_tool(*act("list_orders")))["status"] == "AUTH_REQUIRED"
    for secret in (PASSWORD, got["access_token"], got["refresh_token"]):
        assert secret not in caplog.text
    with sqlite3.connect(env.db) as conn:
        dump = "\n".join(conn.iterdump())
    assert got["access_token"] not in dump and got["refresh_token"] not in dump  # hashes only


async def test_sign_in_with_email_or_an_api_key(env: Env) -> None:
    async with httpx.AsyncClient(follow_redirects=False) as http:
        by_email = await connect_app(env, http, action="password", login="Alice@Example.com", password=PASSWORD)
        by_env_key = await connect_app(env, http, action="key", key=API_KEY)
    assert env.oauth.user_for_access_token(by_email["access_token"]) == "alice"
    assert env.oauth.user_for_access_token(by_env_key["access_token"]) == "default"


async def test_create_an_account_on_the_sign_in_page(env: Env) -> None:
    async with httpx.AsyncClient(follow_redirects=False) as http:
        got = await connect_app(
            env, http, action="signup", code=CODE, name="bob", email="bob@example.com", password="bob-password1"
        )
    assert env.oauth.user_for_access_token(got["access_token"]) == "bob"
    assert env.keys.check_login("bob", "bob-password1") == "bob"


async def test_wrong_details_are_refused_and_guessing_is_slowed_down(env: Env) -> None:
    async with httpx.AsyncClient(follow_redirects=False) as http:
        app = await register(http, env.base)
        request_id = await start(http, app, pkce()[1], env.base)
        wrong = await sign_in(http, env.base, request_id, action="password", login="alice", password="nope-nope")
        assert wrong.status_code == 401 and "don't match" in html.unescape(wrong.text)
        assert "location" not in wrong.headers
        bad_key = await sign_in(http, env.base, request_id, action="key", key="htb_made_up")
        assert bad_key.status_code == 401
        bad_code = await sign_in(http, env.base, request_id, action="signup", code="x", name="eve",
                                 email="e@example.com", password="eve-password1")
        assert bad_code.status_code == 403 and env.keys.find("eve") is None
        for _ in range(7):
            await sign_in(http, env.base, request_id, action="password", login="alice", password="still-wrong")
        blocked = await sign_in(http, env.base, request_id, action="password", login="alice", password=PASSWORD)
        assert blocked.status_code == 429  # even the right password, for a while


async def test_cancelling_tells_the_app(env: Env) -> None:
    async with httpx.AsyncClient(follow_redirects=False) as http:
        app = await register(http, env.base)
        request_id = await start(http, app, pkce()[1], env.base)
        resp = await sign_in(http, env.base, request_id, action="deny")
    assert resp.status_code == 302 and "error=access_denied" in resp.headers["location"]


async def test_codes_are_single_use_and_bound_to_the_verifier(env: Env) -> None:
    async with httpx.AsyncClient(follow_redirects=False) as http:
        app = await register(http, env.base)
        verifier, challenge = pkce()
        request_id = await start(http, app, challenge, env.base)
        code = await code_from(await sign_in(http, env.base, request_id, action="password", login="alice",
                                             password=PASSWORD))
        assert (await tokens(http, app, code, "a-different-verifier-" + "x" * 30)).status_code == 400
        assert (await tokens(http, app, code, verifier)).status_code == 200
        assert (await tokens(http, app, code, verifier)).status_code == 400  # used


async def test_refresh_rotates_and_a_sign_in_lasts_30_days(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import runtime.oauth as oauth_module

    async with httpx.AsyncClient(follow_redirects=False) as http:
        got = await connect_app(env, http, action="password", login="alice", password=PASSWORD)

        async def refresh(token: str) -> httpx.Response:
            return await http.post(got["metadata"]["token_endpoint"], data={
                "grant_type": "refresh_token", "refresh_token": token, "client_id": got["client_id"]})

        renewed = await refresh(got["refresh_token"])
        assert renewed.status_code == 200, renewed.text
        assert env.oauth.user_for_access_token(renewed.json()["access_token"]) == "alice"
        assert (await refresh(got["refresh_token"])).status_code == 400  # the old one is spent

        real = oauth_module.time.time
        monkeypatch.setattr(oauth_module.time, "time", lambda: real() + 31 * 86400)
        assert (await refresh(renewed.json()["refresh_token"])).status_code == 400  # signed in over 30 days ago
        assert env.oauth.user_for_access_token(renewed.json()["access_token"]) is None


# ---- the account page ---------------------------------------------------------------------------------------


async def account_session(env: Env, http: httpx.AsyncClient) -> None:
    resp = await http.post(f"{env.base}/account/login", data={"action": "password", "login": "alice",
                                                               "password": PASSWORD})
    assert resp.status_code == 302 and resp.headers["location"] == "/account"


async def test_account_page_needs_signing_in(env: Env) -> None:
    async with httpx.AsyncClient(follow_redirects=False) as http:
        assert (await http.get(f"{env.base}/account")).headers["location"] == "/account/login"
        assert (await http.get(f"{env.base}/account/status")).status_code == 401
        await account_session(env, http)
        page = await http.get(f"{env.base}/account")
    assert page.status_code == 200 and "<title>Account, alice</title>" in page.text
    for tile in ("Connector", "New key", "Password"):
        assert f"<span>{tile}</span>" in page.text
    assert "Alerts" not in page.text and "Notification" not in page.text
    assert "Nothing yet" in page.text  # no sites or apps yet
    assert f'data-copy="{env.base}/mcp"' in page.text  # the connector URL, one click to copy


async def test_waiting_sign_ins_show_up_live(env: Env) -> None:
    async with httpx.AsyncClient(follow_redirects=False) as http:
        await account_session(env, http)
        assert (await http.get(f"{env.base}/account/status")).json() == {"pending": []}
        link = env.manager.start("alice", DOMAIN)["url"]
        env.manager.start("bob", DOMAIN)  # someone else's: not shown
        pending = (await http.get(f"{env.base}/account/status")).json()["pending"]
    assert [(p["domain"], p["url"]) for p in pending] == [(DOMAIN, link)]


async def test_disconnect_a_site_and_an_app(env: Env) -> None:
    env.store.put("alice", DOMAIN, env.provider, {"sid": VALID_SID})
    async with httpx.AsyncClient(follow_redirects=False) as http:
        got = await connect_app(env, http, action="password", login="alice", password=PASSWORD)
        page = await http.get(f"{env.base}/account")  # the sign-in above also signed in to the account page
        assert DOMAIN in page.text and "Claude" in page.text
        await http.post(f"{env.base}/account/disconnect", data={"domain": DOMAIN})
        await http.post(f"{env.base}/account/apps/revoke", data={"client_id": got["client_id"]})
    assert env.store.get("alice", DOMAIN, env.provider) is None
    assert env.oauth.user_for_access_token(got["access_token"]) is None and env.oauth.apps("alice") == []


async def test_new_api_key_from_the_account_page(env: Env) -> None:
    old = env.keys.reissue("alice")
    async with httpx.AsyncClient(follow_redirects=False) as http:
        await account_session(env, http)
        page = await http.post(f"{env.base}/account/key")
    found = re.search(r"htb_[A-Za-z0-9_-]{30,}", page.text)
    assert found is not None and env.keys.user_for(found.group(0)) == "alice"
    assert env.keys.user_for(old) is None and "claude mcp add" in page.text


async def test_other_sites_cant_post_to_the_account_page(env: Env) -> None:
    env.store.put("alice", DOMAIN, env.provider, {"sid": VALID_SID})
    async with httpx.AsyncClient(follow_redirects=False) as http:
        await account_session(env, http)
        evil = await http.post(f"{env.base}/account/disconnect", data={"domain": DOMAIN},
                               headers={"origin": "https://evil.example"})
    assert evil.status_code == 403 and env.store.get("alice", DOMAIN, env.provider) is not None
