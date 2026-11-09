"""Host and registrable-domain helpers (no public-suffix-list dependency)."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

# Common multi-label public suffixes. Good enough for the sites we map; extend as needed.
MULTI_LABEL_SUFFIXES = frozenset(
    {
        "ac.uk",
        "co.uk",
        "gov.uk",
        "org.uk",
        "ltd.uk",
        "me.uk",
        "com.au",
        "net.au",
        "org.au",
        "co.jp",
        "co.nz",
        "co.in",
        "co.za",
        "com.br",
        "com.cn",
        "com.hk",
        "com.sg",
        "com.tr",
        "com.mx",
    }
)


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def public_suffix(host: str) -> str:
    host = host.lower().rstrip(".")
    if is_ip(host) or "." not in host:
        return ""
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-2:])
    return labels[-1]


def registrable_domain(host: str) -> str:
    """``www.linkedin.com`` → ``linkedin.com``; ``a.b.co.uk`` → ``b.co.uk``; IPs/localhost as-is."""
    host = host.lower().rstrip(".")
    suffix = public_suffix(host)
    if not suffix:
        return host
    rest = host[: -len(suffix) - 1]
    if not rest:
        return host
    return f"{rest.rsplit('.', 1)[-1]}.{suffix}"


def same_site(host: str, domain: str) -> bool:
    """True when ``host`` belongs to the registrable ``domain``."""
    return registrable_domain(host) == registrable_domain(domain)


def tool_prefix(domain: str) -> str:
    """Registrable domain minus its TLD, lowercased, ``[a-z0-9_]`` only, max 20 chars."""
    reg = registrable_domain(domain)
    suffix = public_suffix(reg)
    stem = reg[: -len(suffix) - 1] if suffix else reg
    cleaned = re.sub(r"[^a-z0-9_]+", "_", stem.lower()).strip("_")
    return (cleaned or "site")[:20]
