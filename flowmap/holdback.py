"""Holding writes back while mapping (DECISIONS M8): the request that would really commit a write never
reaches the site. The browser answers it itself and keeps it, so it can still be mapped.

Two phases, so reads made with POST (search forms, GraphQL queries) still work:

- before the committing step, only clear changes are held: PUT, PATCH, DELETE, a GraphQL mutation, and a
  POST that navigates (a form submit);
- once ``armed`` (the click that commits: "Add to cart", "Send", "Post"...), every request that isn't a
  GET or HEAD is held.

A held request gets a 200 with an empty JSON object and the ``x-flowmap-held`` header, so the page carries on
and capture records the exchange; inference must not trust that response (flowmap/server.py loosens the
step's ``expect`` and the runtime marks the action as never run for real).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("flowmap.holdback")

HELD_HEADER = "x-flowmap-held"
MUTATION = re.compile(r"""(^|["'{\s])mutation\b""")
# A click with this wording commits a write: from it on only reads reach the site (recorder and replay).
WRITES = re.compile(
    r"\b(follow|connect|send|post|publish|buy|order|pay|checkout|add to (?:cart|basket|bag)|like|subscribe"
    r"|message|invite|endorse|purchase|apply|reply|comment|share|repost)\b",
    re.I,
)


def is_clear_change(method: str, post_data: str | None, resource_type: str) -> bool:
    if method in ("PUT", "PATCH", "DELETE"):
        return True
    if method == "POST":
        return resource_type == "document" or bool(MUTATION.search(post_data or ""))
    return False


@dataclass
class HoldBack:
    armed: bool = False
    held: list[dict[str, Any]] = field(default_factory=list)

    async def install(self, context: Any) -> None:
        await context.route("**/*", self._route)

    def holds(self, method: str, post_data: str | None, resource_type: str) -> bool:
        if method in ("GET", "HEAD", "OPTIONS"):
            return False
        return self.armed or is_clear_change(method, post_data, resource_type)

    async def _route(self, route: Any, request: Any) -> None:
        try:
            post_data = request.post_data
        except Exception:  # binary body
            post_data = None
        if not self.holds(request.method, post_data, request.resource_type):
            await route.continue_()
            return
        self.held.append({"method": request.method, "url": request.url})
        log.info("held back %s %s", request.method, request.url)
        await route.fulfill(
            status=200, headers={"content-type": "application/json", HELD_HEADER: "1"}, body="{}"
        )
