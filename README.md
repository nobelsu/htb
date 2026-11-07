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

Sites with `connect_providers` need the user's login once. The agent calls `start_connection(domain)` and gets a
link. The user logs in to the site in their own browser as usual, opens the link and clicks **Connect**. The
**HTB Connect** extension (`extension/`) asks Chrome for access to that one site and sends its login cookies to the
gateway, which checks them with the provider's `session_check` and stores them encrypted (`SESSION_KEY`). The
password never reaches us or the agent. `poll_connection` / `connection_status` report progress; a keep-alive
marks sessions that have expired.

Install the extension (until it's on the Chrome Web Store): `chrome://extensions` → Developer mode → Load unpacked →
the `extension` folder. For a hosted gateway, first run `uv run python -m runtime.extension --origin https://your.gateway`.

Hosting notes: install Chrome on the server for the browser tier (`uv run playwright install --with-deps chrome`),
keep `PROFILES_DIR` and the SQLite file on persistent storage, serve over HTTPS and set `PUBLIC_BASE_URL`.
Fixture shop login for testing: `demo` / `demo-password`. The end-to-end login tests need Playwright's Chromium
(`uv run playwright install chromium`), since branded Chrome no longer loads unpacked extensions under automation.
