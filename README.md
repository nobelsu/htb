# htb

Runtime for website action cards: registry, executor and MCP gateway. Spec: `CARD_SPEC.md` (v0.4).
Judgement calls: `DECISIONS.md`.

```bash
uv sync
uv run pytest
cp .env.example .env   # then set GATEWAY_API_KEY and CONFIRMATION_SECRET
```

Run the fixture shop and the gateway (two terminals):

```bash
uv run uvicorn tests.fixture_site.app:app --port 8900
uv run python -m runtime.gateway --load tests/fixture_site.json
```

The MCP endpoint is `http://127.0.0.1:8765/mcp` (Streamable HTTP, `Authorization: Bearer $GATEWAY_API_KEY`).
`POST http://127.0.0.1:8900/_admin/break` moves one fixture endpoint, `/_admin/reset` restores it.

The mapper imports `schema` (`validate_card`, `Card`, `Envelope`) and saves through `runtime.registry.Registry`.

## Healing

When an action returns `PAGE_CHANGED`, the runtime queues a heal job, pre-checks it (a block or an expired login
cancels it), then asks the mapper to re-map just that action:

```python
class Remapper(Protocol):  # runtime/healing.py
    async def remap(self, card: Card, actions: list[str], reason: str) -> Card | dict: ...
```

Return the whole proposed card. The runtime validates the changed actions against their `examples` and saves a
new version; writes are never auto-activated. Set `REMAPPER=module:Class` in `.env`. For the fixture shop,
`REMAPPER=tests.fixture_site.remapper:FixtureRemapper` demos the full loop: break the site, call `shop_get_item`,
watch `heal_status`, call again.

## Login (connect a site) and the browser tier

Sites with `connect_providers` need the user to log in once. The agent calls `start_connection(domain)` and
gets a link; the user opens it and sees the site's real login page, streamed from a browser on the server, and
logs in themselves. The password never reaches us or the agent. When the provider's `session_check` passes, the
profile's cookies are stored encrypted (`SESSION_KEY`) for the http/impersonate tiers, and the browser profile
(under `PROFILES_DIR`) stays warm for the browser tier. `poll_connection` / `connection_status` report progress.

Hosting notes: install Chrome on the server (`uv run playwright install --with-deps chrome`), keep
`PROFILES_DIR` and the SQLite file on persistent storage, serve over HTTPS and set `PUBLIC_BASE_URL` to that
https URL. Fixture shop login for testing: `demo` / `demo-password`.
