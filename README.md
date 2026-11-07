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
link. The page offers:
- **Log in to the site** (default, nothing to install): a real Chromium in a short-lived Neko container, streamed
  over WebRTC into the page. The user signs in there; we watch the browser's cookies and stop the container once
  the login works.
- **Use my existing login** (if the **HTB Connect** extension in `extension/` is installed): sends the site's
  cookies from the user's own browser.

Either way the cookies are checked with the provider's `session_check` and stored encrypted (`SESSION_KEY`); the
password never reaches us or the agent. `poll_connection` / `connection_status` report progress; a keep-alive marks
sessions that have expired.

Setup:
- Neko: Docker running, and `docker pull ghcr.io/m1k1o/neko/chromium:latest` (about 2.2 GB). Hosted: set
  `NEKO_PUBLIC_IP` to the server's public IP, open the `NEKO_UDP_PORTS` range (UDP), and for users behind strict
  networks add a TURN server via `NEKO_ICE_SERVERS`. `NEKO_ENABLED=0` turns it off.
- Extension (optional): `chrome://extensions` → Developer mode → Load unpacked → `extension/`. For a hosted gateway,
  first run `uv run python -m runtime.extension --origin https://your.gateway`.
- Browser tier: install Chrome on the server (`uv run playwright install --with-deps chrome`); keep `PROFILES_DIR` and
  the SQLite file on persistent storage; serve over HTTPS and set `PUBLIC_BASE_URL`.

Fixture shop login for testing: `demo` / `demo-password`. Tests needing Docker/Neko or Playwright's Chromium skip
themselves when those aren't installed.
