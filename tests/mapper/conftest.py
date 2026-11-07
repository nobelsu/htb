from __future__ import annotations

import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import pytest
from fixture_site import FixtureSite


@pytest.fixture
def site():
    with FixtureSite() as running:
        yield running


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: str

    def json(self):
        return json.loads(self.body)


class Client:
    """Minimal cookie-keeping HTTP client for poking the fixture site from tests."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def cookie(self, name: str) -> str | None:
        return next((c.value for c in self.jar if c.name == name), None)

    def request(self, method: str, path: str, data: bytes | None = None, headers=None) -> Response:
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers or {})
        try:
            with self.opener.open(req, timeout=5) as r:
                return Response(r.status, dict(r.headers), r.read().decode())
        except urllib.error.HTTPError as e:
            return Response(e.code, dict(e.headers), e.read().decode())

    def get(self, path: str, headers=None) -> Response:
        return self.request("GET", path, headers=headers)

    def post_form(self, path: str, form: dict[str, str]) -> Response:
        body = urllib.parse.urlencode(form).encode()
        return self.request(
            "POST", path, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

    def login(self) -> None:
        r = self.post_form("/login", {"username": "demo", "password": "demo", "next": "/"})
        assert r.status == 200, r


@pytest.fixture
def client(site):
    return Client(site.url)
