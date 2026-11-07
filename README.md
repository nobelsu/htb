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

The MCP endpoint is `http://127.0.0.1:8765/mcp` (Streamable HTTP, `Authorization: Bearer <key>`).

### Regular Claude (web, desktop, phone)

In Claude: Customize → Connectors → Add custom connector, URL `<PUBLIC_BASE_URL>/mcp`, then Connect. Claude registers
itself and opens our sign-in page (password, an API key, or a new account with the invite code). Your account page
is `<PUBLIC_BASE_URL>/account`: sign-ins your agent is waiting for, your sites, connected apps, and your API key.
`PUBLIC_BASE_URL` must be the https address people reach (e.g. the ngrok domain).

### Users and API keys

Every key belongs to one user, and every tool call acts as that user: their own sign-ins, browser profiles, login
links and device identities. `GATEWAY_API_KEY` from `.env` is user `default`. Everyone else gets their own key:

- **Sign-up page:** set `SIGNUP_CODE` in `.env` and share it; people open `<PUBLIC_BASE_URL>/signup`, enter the code,
  a name and an email, and get their key once with the `claude mcp add ...` line to paste. Wrong codes are rate
  limited (10 per 10 minutes per address). Empty `SIGNUP_CODE` = no sign-up page.
- **Command line:** `uv run python -m runtime.keys add alice --email alice@example.com` (also `list`, `reissue`,
  `revoke`). Keys are shown once; only their SHA-256 is stored (`api_users` table in `runtime.db`).
`POST http://127.0.0.1:8900/_admin/break` moves one fixture endpoint, `/_admin/reset` restores it.

The mapper imports `schema` (`validate_card`, `Card`, `Envelope`) and saves through `runtime.registry.Registry`.

## Mapper

```bash
uv run python -m mapper capture <start_url> --task NAME --run m1|m2|real|page2 [--marker query=zqxv8841]
uv run python -m mapper map captures/<domain>/
uv run python -m mapper validate path/to/card.json
uv run python -m mapper heal captures/<domain>/ --save   # site changed: replay all recordings, re-map, save
```

Captures, HAR files, `state.json` and `.env` hold live login sessions: never commit them.

## Healing

When an action returns `PAGE_CHANGED`, the runtime queues a heal job, pre-checks it (a block or an expired login
cancels it), then asks the mapper to re-record just the broken action and re-map the site:

```python
class Remapper(Protocol):  # runtime/healing.py
    async def remap(self, card: Card, actions: list[str], reason: str) -> Card | dict: ...
```

Return the whole proposed card. The runtime validates the changed actions against their `examples` and saves a
new version (healed writes go live too, listed as unvalidated).

- `REMAPPER=mapper` (the default in `.env.example`) uses WeiWei's mapper (`runtime/mapper_remapper.py`): it replays
  the broken action's recordings from `CAPTURES_DIR/<domain>/<action>/` in a headless browser, signed in as the
  user, then re-maps the site. Sites need recordings made with `python -m mapper capture`.
- `REMAPPER=tests.fixture_site.remapper:FixtureRemapper` is a stand-in for the fixture shop (which has no
  recordings): break the site, call `get_item`, watch `heal_status`, call again.

## Login (connect a site) and the browser tier

Sites with `connect_providers` need the user's login once. The agent calls `start_connection(domain)` and shows the
user a link. On that page the user clicks **Log in** and signs in to the real site inside a private Chromium in a
short-lived Neko container, streamed over WebRTC. We watch the browser's cookies, check them with the provider's
`session_check` and store them encrypted (`SESSION_KEY`); the password never reaches us or the agent. The agent
calls `poll_connection` straight after showing the link: it waits (up to 45 s per call) and returns as soon as the
user is signed in, so the agent carries on by itself. `connection_status` reports the state; `disconnect` deletes
the stored sign-in and the server-side browser profile; a keep-alive marks sessions that have expired.
Cookies the site refreshes (after successful actions and passing keep-alive checks) are saved back to the encrypted
sign-in, so sites that rotate their login cookie stay signed in. The login browser's identity (user-agent, client
hints, language) is saved too, and every tier presents it on that sign-in's later requests; requests with no saved
identity look like Chrome 152 on Windows.

**Residential proxy (optional).** Set `PROXY_URL` to your provider's sticky-session URL with `{session}` where its
session id goes. Each sign-in (user + site) gets its own session id, so the login browser and every later request
for that sign-in leave through the same home IP; public actions go direct. The login browser can't send proxy
credentials itself, so a small helper inside its container adds them (`runtime/neko/proxy_auth.py`).

Setup:
- Neko: Docker running, and `docker pull ghcr.io/m1k1o/neko/chromium:latest` (about 2.2 GB). Hosted: set
  `NEKO_PUBLIC_IP` to the server's public IP, open the `NEKO_UDP_PORTS` range (UDP and TCP), and for users behind
  strict networks add a TURN server via `NEKO_ICE_SERVERS`. `NEKO_ENABLED=0` turns it off.
- Browser tier: install Chrome on the server (`uv run playwright install --with-deps chrome`); keep `PROFILES_DIR` and
  the SQLite file on persistent storage; serve over HTTPS and set `PUBLIC_BASE_URL`.

Fixture shop login for testing: `demo` / `demo-password`. Tests needing Docker/Neko or Playwright's Chromium skip
themselves when those aren't installed.
Heal jobs use the mapper automatically with `REMAPPER=mapper`; `python -m mapper heal` still works by hand.
