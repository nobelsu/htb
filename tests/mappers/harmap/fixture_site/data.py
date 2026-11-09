"""Deterministic synthetic dataset for the fixture site."""

from __future__ import annotations

PAGE_SIZE = 10

_ADJECTIVES = [
    "Northwind",
    "Bluebird",
    "Granite",
    "Lumen",
    "Copperleaf",
    "Harbor",
    "Quill",
    "Saffron",
    "Tidewater",
    "Ember",
    "Juniper",
    "Orbit",
    "Pinecrest",
    "Vantage",
    "Willow",
    "Zephyr",
]
_NOUNS = ["Labs", "Systems", "Works", "Group", "Partners", "Collective"]
_INDUSTRIES = ["Software", "Biotech", "Software", "Logistics", "Software", "Finance"]


def _slug(name: str) -> str:
    return name.lower().replace(" ", "-")


def build_companies(count: int = 46) -> list[dict]:
    companies = []
    for i in range(count):
        name = f"{_ADJECTIVES[i % len(_ADJECTIVES)]} {_NOUNS[(i // len(_ADJECTIVES)) % len(_NOUNS)]}"
        if i >= len(_ADJECTIVES) * len(_NOUNS):
            name = f"{name} {i}"
        industry = _INDUSTRIES[i % len(_INDUSTRIES)]
        companies.append(
            {
                "id": f"c_{1000 + i}",
                "name": name,
                "industry": industry,
                "url": f"/company/{_slug(name)}",
                "followers": 1200 + (i * 7919) % 50000,
                "logo": {"url": f"/static/logos/{_slug(name)}.png", "width": 64},
            }
        )
    return companies


COMPANIES = build_companies()


def search(query: str, start: int, count: int = PAGE_SIZE) -> tuple[int, list[dict]]:
    """Case-insensitive substring match on name or industry. Returns (total, page)."""
    q = query.strip().lower()
    hits = [c for c in COMPANIES if q and (q in c["name"].lower() or q in c["industry"].lower())]
    return len(hits), hits[start : start + count]
