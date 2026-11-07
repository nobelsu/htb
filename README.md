# htb

Hack the Burgh 2026: turn websites into agent-callable actions ("cards").
Spec: `CARD_SPEC.md` (v0.4, frozen). Runtime judgement calls: `DECISIONS.md`.
Mapper design: `docs/mapper-design.md`.

| Path | What | Owner |
|---|---|---|
| `CARD_SPEC.md`, `action_map.example.json` | Frozen card spec and its example card | shared |
| `schema/` | Card and envelope models, `validate_card()` | Nobel |
| `runtime/` | Executor, registry, MCP gateway | Nobel |
| `tools/check_card.py` | Standalone card checker | Nobel |
| `tests/` (except `tests/mapper/`) | Runtime tests and the fixture shop | Nobel |
| `mapper/`, `tests/mapper/`, `docs/mapper-design.md` | Capture and capture → card pipeline, its tests and fixture site | WeiWei |

## Setup

```bash
uv sync
uv run playwright install chromium   # browser for mapper captures
uv run pytest
cp .env.example .env   # then set GATEWAY_API_KEY and CONFIRMATION_SECRET
```

## Runtime

Run the fixture shop and the gateway (two terminals):

```bash
uv run uvicorn tests.fixture_site.app:app --port 8900
uv run python -m runtime.gateway --load tests/fixture_site.json
```

The MCP endpoint is `http://127.0.0.1:8765/mcp` (Streamable HTTP, `Authorization: Bearer $GATEWAY_API_KEY`).
`POST http://127.0.0.1:8900/_admin/break` moves one fixture endpoint, `/_admin/reset` restores it.

The mapper imports `schema` (`validate_card`, `Card`, `Envelope`) and saves through `runtime.registry.Registry`.

## Mapper

```bash
uv run python -m mapper explore <start_url> --state login.state.json --save   # LLM picks tasks, scripts, records, maps
uv run python -m mapper capture <start_url> --task NAME --run m1|m2|real|page2 [--marker query=zqxv8841]
uv run python -m mapper map captures/<domain>/
uv run python -m mapper validate path/to/card.json
uv run python -m mapper heal captures/<domain>/ --save   # site changed: replay all recordings, re-map, save
```

Captures, HAR files, `state.json` and `.env` hold live login sessions: never commit them.

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

Sites with `connect_providers` need the user's login once. The agent calls `start_connection(domain)` and shows the
user a link. On that page the user clicks **Log in** and signs in to the real site inside a private Chromium in a
short-lived Neko container, streamed over WebRTC. We watch the browser's cookies, check them with the provider's
`session_check` and store them encrypted (`SESSION_KEY`); the password never reaches us or the agent. The agent
calls `poll_connection` straight after showing the link: it waits (up to 45 s per call) and returns as soon as the
user is signed in, so the agent carries on by itself. `connection_status` reports the state; `disconnect` deletes
the stored sign-in and the server-side browser profile; a keep-alive marks sessions that have expired.

Setup:
- Neko: Docker running, and `docker pull ghcr.io/m1k1o/neko/chromium:latest` (about 2.2 GB). Hosted: set
  `NEKO_PUBLIC_IP` to the server's public IP, open the `NEKO_UDP_PORTS` range (UDP and TCP), and for users behind
  strict networks add a TURN server via `NEKO_ICE_SERVERS`. `NEKO_ENABLED=0` turns it off.
- Browser tier: install Chrome on the server (`uv run playwright install --with-deps chrome`); keep `PROFILES_DIR` and
  the SQLite file on persistent storage; serve over HTTPS and set `PUBLIC_BASE_URL`.

Fixture shop login for testing: `demo` / `demo-password`. Tests needing Docker/Neko or Playwright's Chromium skip
themselves when those aren't installed.
The mapper's `heal` re-records and re-maps the whole site (see `docs/mapper-design.md`, "Healing"); wiring it in as the runtime's `Remapper` is the next step.
