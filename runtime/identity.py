"""Device identity: the browser a sign-in was made with, presented again on every later request.

A site that ties a login to a device sees the login browser (Chromium in Neko) and then our server's requests. So
the login browser's identity is saved with the sign-in, and every tier presents it: its user-agent, its client
hints (`sec-ch-ua*`, which Chrome sends next to the user-agent) and its language. Requests without a saved identity
(public sites, older sign-ins) use DEFAULT_IDENTITY, a Chrome matching the impersonate tier's newest fingerprint.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

# Chromium's "GREASE" brand: a fake brand added so sites can't parse the list naively. It is derived from the
# major version (components/embedder_support/user_agent_utils.cc, GenerateBrandVersionList).
_GREASEY_CHARS = (" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_")
_GREASED_VERSIONS = ("8", "99", "24")
_ORDERS = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))

# curl_cffi's Chrome fingerprints (TLS + HTTP/2), newest last. The impersonate tier uses the newest one that isn't
# newer than the identity's Chrome.
_IMPERSONATE_CHROME = (99, 100, 101, 104, 107, 110, 116, 119, 120, 123, 124, 131, 136, 142, 145, 146, 150, 151, 152)
_IMPERSONATE_MACOS = (151, 152)

_VERSION = re.compile(r"(?:Headless)?Chrome/(\d+)\.(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class BrowserIdentity:
    user_agent: str
    full_version: str  # e.g. "155.0.8059.79"
    platform: str  # client-hint platform: "Linux", "macOS", "Windows"
    brand: str | None  # "Google Chrome", or None for a plain Chromium build
    accept_language: str  # the Accept-Language header, e.g. "en-US,en;q=0.9"
    mobile: bool = False

    @property
    def major(self) -> int:
        return int(self.full_version.split(".", 1)[0])

    def brands(self, full: bool = False) -> list[tuple[str, str]]:
        """The client-hint brand list, in Chrome's order (`full` = full versions, for sec-ch-ua-full-version-list)."""
        seed = self.major
        version = self.full_version if full else str(seed)
        chars = _GREASEY_CHARS
        grease_brand = f"Not{chars[seed % len(chars)]}A{chars[(seed + 1) % len(chars)]}Brand"
        grease_version = _GREASED_VERSIONS[seed % len(_GREASED_VERSIONS)]
        grease = (grease_brand, f"{grease_version}.0.0.0" if full else grease_version)
        chromium = ("Chromium", version)
        if self.brand is None:
            out = [grease, chromium] if seed % 2 == 0 else [chromium, grease]
            return out
        order = _ORDERS[seed % len(_ORDERS)]
        slots: list[tuple[str, str]] = [("", "")] * 3
        slots[order[0]], slots[order[1]], slots[order[2]] = grease, chromium, (self.brand, version)
        return slots

    def sec_ch_ua(self, full: bool = False) -> str:
        return ", ".join(f'"{name}";v="{version}"' for name, version in self.brands(full))

    def headers(self) -> dict[str, str]:
        """What the browser sends on every request (lowercase names)."""
        return {
            "user-agent": self.user_agent,
            "sec-ch-ua": self.sec_ch_ua(),
            "sec-ch-ua-mobile": "?1" if self.mobile else "?0",
            "sec-ch-ua-platform": f'"{self.platform}"',
            "accept-language": self.accept_language,
        }

    def impersonate_target(self) -> str:
        """curl_cffi's closest Chrome fingerprint that isn't newer than this browser."""
        macos = self.platform == "macOS"
        versions = _IMPERSONATE_MACOS if macos else _IMPERSONATE_CHROME
        fitting = [v for v in versions if v <= self.major] or [versions[0]]
        return f"chrome{fitting[-1]}" + ("_macos" if macos else "")

    def cdp_override(self) -> dict[str, Any]:
        """Parameters for the browser tier's DevTools `Network.setUserAgentOverride` (headers and navigator)."""
        return {
            "userAgent": self.user_agent,
            "acceptLanguage": ",".join(part.split(";", 1)[0].strip() for part in self.accept_language.split(",")),
            "platform": _NAVIGATOR_PLATFORM.get(self.platform, self.platform),
            "userAgentMetadata": {
                "brands": [{"brand": b, "version": v} for b, v in self.brands()],
                "fullVersionList": [{"brand": b, "version": v} for b, v in self.brands(full=True)],
                "fullVersion": self.full_version,
                "platform": self.platform,
                "platformVersion": "",
                "architecture": "x86",
                "model": "",
                "mobile": self.mobile,
            },
        }

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> BrowserIdentity:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})

    @classmethod
    def from_devtools(cls, version_info: dict[str, Any], *, brand: str | None, accept_language: str) -> BrowserIdentity:
        """From a browser's DevTools `/json/version` (its "Browser" and "User-Agent")."""
        match = _VERSION.search(str(version_info.get("Browser", "")))
        if match is None:
            raise ValueError(f"not a Chrome/Chromium version: {version_info.get('Browser')!r}")
        user_agent = str(version_info["User-Agent"]).replace("HeadlessChrome/", "Chrome/")
        return cls(
            user_agent=user_agent,
            full_version=".".join(match.groups()),
            platform=platform_of(user_agent),
            brand=brand,
            accept_language=accept_language,
        )


_NAVIGATOR_PLATFORM = {"Linux": "Linux x86_64", "macOS": "MacIntel", "Windows": "Win32"}


def platform_of(user_agent: str) -> str:
    if "Windows" in user_agent:
        return "Windows"
    if "Macintosh" in user_agent or "Mac OS X" in user_agent:
        return "macOS"
    if "Android" in user_agent:
        return "Android"
    return "Linux"


def accept_language_header(languages: str) -> str:
    """Chrome's Accept-Language for a language list like "en-US,en": "en-US,en;q=0.9"."""
    parts = [p.strip() for p in languages.split(",") if p.strip()]
    out = [parts[0]] if parts else []
    for i, lang in enumerate(parts[1:], start=1):
        out.append(f"{lang};q={max(1.0 - i / 10, 0.1):.1f}")
    return ",".join(out)


# Requests with no saved identity: Google Chrome on Windows at curl_cffi's newest fingerprint, so the user-agent,
# client hints and the impersonate tier's TLS all agree.
DEFAULT_IDENTITY = BrowserIdentity(
    user_agent=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/152.0.0.0 Safari/537.36"
    ),
    full_version="152.0.0.0",
    platform="Windows",
    brand="Google Chrome",
    accept_language="en-US,en;q=0.9",
)
