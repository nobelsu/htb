"""Residential proxies: every request that belongs to a sign-in, and the login browser itself, leave through the same
home IP, so a site sees one person in one place.

Configured with one URL template (PROXY_URL) in the provider's own "sticky session" format; `{session}` is replaced
by a stable id per sign-in (user + site + provider), so the provider keeps handing out the same IP for as long as it
allows. The shape is roughly as below, but every provider has its own sticky-session syntax and it changes, so copy
it from your provider's dashboard or docs ("sticky session") and put {session} where their session id goes:

    http://USER-session-{session}:PASS@gateway.provider.example:7000

The login browser (Neko) supports http:// proxies only; https:// and socks5:// work for the server's own requests.

Public actions (no sign-in) go direct. Sites on this machine (localhost) never use the proxy, since a provider can't
reach them, unless `local` is set (tests).
"""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

PLACEHOLDER = "{session}"
SCHEMES = ("http", "https", "socks5", "socks5h")


class ProxyConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProxySpec:
    """One sign-in's proxy. `url` holds the credentials: never log it; use `redacted`."""

    scheme: str
    host: str
    port: int
    username: str
    password: str
    session: str
    local: bool = False  # sites on this machine go through it too (tests)

    @property
    def url(self) -> str:
        auth = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@" if self.username else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    @property
    def server(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def redacted(self) -> str:
        return f"{self.scheme}://***@{self.host}:{self.port} (session {self.session})"

    def __repr__(self) -> str:  # dataclass repr would print the password
        return f"ProxySpec({self.redacted})"

    __str__ = __repr__


def session_id(user: str, domain: str, provider: str | None) -> str:
    """Stable per sign-in, and only letters and digits (providers restrict the session id's characters)."""
    return hashlib.sha256(f"{user}\x00{domain}\x00{provider or ''}".encode()).hexdigest()[:16]


def is_local(host: str) -> bool:
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ProxyConfig:
    template: str
    local: bool = False  # also proxy sites on this machine (tests only)

    def __post_init__(self) -> None:
        parts = urlsplit(self.template.replace(PLACEHOLDER, "x"))
        if parts.scheme not in SCHEMES or not parts.hostname or parts.port is None:
            raise ProxyConfigError(
                "PROXY_URL must look like http://USER:PASS@HOST:PORT (http, https or socks5), with {session} "
                "where your provider puts the sticky-session id"
            )

    @property
    def sticky(self) -> bool:
        return PLACEHOLDER in self.template

    def for_sign_in(self, user: str, domain: str, provider: str | None, base_url: str) -> ProxySpec | None:
        """The proxy for this sign-in's requests to `base_url` (None: go direct)."""
        if not self.local and is_local(urlsplit(base_url).hostname or ""):
            return None
        session = session_id(user, domain, provider)
        parts = urlsplit(self.template.replace(PLACEHOLDER, session))
        return ProxySpec(
            scheme=parts.scheme,
            host=parts.hostname or "",
            port=parts.port or 0,
            username=unquote(parts.username or ""),
            password=unquote(parts.password or ""),
            session=session,
            local=self.local,
        )

    @property
    def redacted(self) -> str:
        parts = urlsplit(self.template.replace(PLACEHOLDER, "x"))
        return f"{parts.scheme}://***@{parts.hostname}:{parts.port}" + ("" if self.sticky else " (not sticky)")
