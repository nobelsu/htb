"""A stand-in for the mapper, for the fixture shop only.

It "re-captures" by probing which item endpoint answers (`/api/items/p1` or `/api/v2/items/p1`) and points the
broken actions' step URLs at the one that works. The real mapper replaces this via the `Remapper` interface.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from spider.card import Card
from spider.jobs.heal import RemapError

OLD, NEW = "/api/items/", "/api/v2/items/"


class FixtureRemapper:
    async def remap(
        self, card: Card, actions: list[str], reason: str, user: str | None = None
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=card.base_url, timeout=5) as client:
            working = [p for p in (OLD, NEW) if (await client.get(f"{p}p1")).status_code == 200]
        if not working:
            raise RemapError("no item endpoint answers")
        target = working[0]
        other = NEW if target == OLD else OLD
        data = card.to_json_dict()
        for action in data["actions"]:
            if action["name"] not in actions:
                continue
            for step in action["steps"]:
                step["request"]["url"] = step["request"]["url"].replace(other, target)
        data["provenance"] = {
            "mapped_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "fixture_remapper",
            "mapper_version": "fixture",
        }
        return data
