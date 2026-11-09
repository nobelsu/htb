# spider

spider turns websites into actions an AI agent can call directly, without driving a browser.

A site's own pages already call the site's own internal endpoints. spider watches a task being done once in a
real browser, works out which of those requests is the action, and writes it down as a **card**: the request to
send, where the inputs go, and where the results are in the answer. Agents then call
`run_action("linkedin.com", "search_companies", {"query": "robotics"})` over MCP and get clean JSON back in
about a second, signed in as their user.

Built for Hack the Burgh 2026 by Nobel Suhendra and Wei Wei.

## How it works

```
agent (Claude) --MCP--> gateway --> executor --> the site's own HTTP endpoints
                          |            |
                          |            +-- the user's sign-in (encrypted), their browser identity,
                          |                residential proxy, tiers: plain HTTP, browser fingerprint, real Chrome
                          |
                          +-- registry: cards, versioned per site (SQLite)
                          +-- map jobs: flowmap records with Claude, replays, infers, checks, saves
                          +-- heal jobs: broken action, replay its recording, re-map, new version
```

1. **Map.** Claude (or a person) does the task once in a browser. flowmap replays it with planted marker values,
   sees which request carries the input and which response carries the results, and confirms it by replaying
   with that request blocked. The model only does the task and names things; which request is the action is
   decided from the evidence.
2. **Save.** One card per site (`CARD_SPEC.md`, explained in `docs/cards.md`), shared by every user. Example values and fixed request values
   are checked for personal data before a card is saved.
3. **Run.** Agents find actions with `find_actions` and call them with `run_action`. Each call uses the user's
   own sign-in, and climbs from plain HTTP to a real Chrome tab only when the site needs it.
4. **Heal.** When a site changes and an action breaks, spider replays that action's recording, re-maps it and
   saves a new card version while the call waits or retries.
5. **Writes are held back while mapping.** When a write (follow, send, add to cart) is mapped, the committing
   request is held in the browser so the site never gets it. Saved writes always need the user's confirmation.

## Repository

| Path | What | Owner |
|---|---|---|
| `CARD_SPEC.md`, `action_map.example.json` | The card format (v0.4) and an example card | shared |
| `cards/` | The live, checked cards: linkedin.com (12 actions) and amazon.co.uk (2). Explained in `docs/cards.md` | Nobel |
| `schema/` | Card and result models, `validate_card()`, placeholders | Nobel |
| `runtime/gateway.py` | The MCP gateway: tools, pages and jobs wired together (`python -m runtime.gateway`) | Nobel |
| `runtime/run/` | Running an action: executor, templates, extraction, signals, tiers, `browser/` (real Chrome tabs), confirmation ids, redaction | Nobel |
| `runtime/signin/` | Sign-in: the connect flow, `neko/` (remote sign-in browsers), encrypted sessions, device identity, proxy | Nobel |
| `runtime/jobs/` | Heal jobs and map jobs, and the adapter to Wei Wei's mapper | Nobel |
| `runtime/store/` | SQLite: card registry and search, users and API keys, job state | Nobel |
| `runtime/web/` | The gateway's pages and the OAuth sign-in for regular Claude | Nobel |
| `flowmap/` | The mapper the product runs: record, replay, infer, assemble, heal, explore | Nobel |
| `mapper/` | Wei Wei's mapper: capture pipeline and explorer | Wei Wei |
| `tools/check_card.py` | Checks a card file against the spec without running it | Nobel |
| `tests/runtime/`, `tests/flowmap/`, `tests/mapper/` | Tests for each package | both |
| `tests/sites/` | Test sites: the demo shop (`fixture_site/`, card `fixture_site.json`) and the bookshop | Nobel |
| `flows/bookshop.test/` | Example flowmap recordings | Nobel |
| `docs/` | Cards explained (`cards.md`), the mappers (`flowmap.md`, `mapper-design.md`), the pitch context | both |

`runtime/registry.py`, `executor.py`, `confirm.py` and `sessions.py` are old import paths kept for `mapper/`.
`DECISIONS.md` records every judgement call made while building (L-series runtime and sign-in, M-series mapper).

## Quick start

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra llm
uv run playwright install chromium
cp .env.example .env
```

Set `GATEWAY_API_KEY` and `CONFIRMATION_SECRET` in `.env`, and `SESSION_KEY`
(`uv run python -m runtime.signin.session_store` prints one). Then run the demo shop and the gateway, in two terminals:

```bash
uv run uvicorn tests.sites.fixture_site.app:app --port 8900
```

```bash
uv run python -m runtime.gateway --load tests/sites/fixture_site.json
```

Add it to Claude Code:

```bash
claude mcp add --transport http spider http://127.0.0.1:8765/mcp --header "Authorization: Bearer <GATEWAY_API_KEY>"
```

Then ask Claude: "use spider to search the fixture.test shop for lamps".

To also load the real sites' cards (LinkedIn needs each user to sign in with their own account):

```bash
uv run python -m runtime.gateway --load tests/sites/fixture_site.json --load cards/linkedin.com.json --load cards/amazon.co.uk.json
```

The demo shop (`tests/sites/fixture_site/`) has 21 actions covering every runtime case: reads, writes that need
confirmation, rate limits, bot challenges, browser-only endpoints, and a sign-in (`demo` / `demo-password`).
`POST http://127.0.0.1:8900/_admin/break` moves one endpoint so you can watch an action heal;
`/_admin/reset` puts it back.

## MCP tools

The endpoint is `<PUBLIC_BASE_URL>/mcp` (Streamable HTTP, `Authorization: Bearer <key>`, or OAuth for regular
Claude).

| Tool | What it does |
|---|---|
| `find_actions` | Search the registry for actions that fit a task ("search companies on linkedin.com") |
| `run_action` | Run one action with inputs; writes return a confirmation id first |
| `request_map` | Ask for a site, or one named action on it, to be set up when nothing fits |
| `map_status` | Progress of a map job |
| `heal_status` | Progress of a heal job after a `PAGE_CHANGED` result |
| `start_connection` | Get a sign-in link for a site that needs the user's login |
| `poll_connection` | Wait until the user has signed in |
| `connection_status` | Which sites the user is signed in to |
| `disconnect` | Delete a stored sign-in and its browser profile |

Every result is one envelope (`schema/envelope.py`): a status (`SUCCESS`, `EMPTY`, `NEEDS_CONFIRMATION`,
`AUTH_EXPIRED`, `PAGE_CHANGED`, `BLOCKED`, ...), the data, and what to do next.

## Using spider from regular Claude (web, desktop, phone)

In Claude: Customize, Connectors, Add custom connector, URL `<PUBLIC_BASE_URL>/mcp`, then Connect. Claude registers
itself and opens spider's sign-in page (password, an API key, or a new account with the invite code).
`PUBLIC_BASE_URL` must be the https address people reach, for example an ngrok domain.

The gateway serves its own pages (`runtime/web/ui.py`): home `/`, sign-up `/signup`, sign-in `/account/login`, the
account page `/account` (waiting sign-ins, sites, connected apps, API key), Claude's sign-in `/oauth/login`, and
the website sign-in `/connect/<token>`. `PRODUCT_NAME` sets the name shown on them.

## Users and keys

Every key belongs to one user, and every call acts as that user: their own sign-ins, browser profiles and device
identity. `GATEWAY_API_KEY` is user `default`. Everyone else gets their own key:

- **Sign-up page:** set `SIGNUP_CODE` and share it; people open `/signup`, enter the code, a name and an email,
  and get their key once with the `claude mcp add` line. Wrong codes are rate limited.
- **Command line:** `uv run python -m runtime.store.keys add alice --email alice@example.com` (also `list`, `reissue`,
  `revoke`, `set-password`). Keys are shown once; only their SHA-256 is stored.

## Setting up a new site

### From an agent (map jobs)

With `MAPPER=flowmap` and `ANTHROPIC_API_KEY` set, an agent that finds nothing for a site calls `request_map`.
The gateway maps it on the server: one named action ("search products on amazon.co.uk") in a few minutes, or the
whole site's main reads. The account page shows the job's progress; `map_status` reports it to the agent. A site
that needs a sign-in pauses the job and returns a sign-in link, then carries on.

A specific change ("follow this company") goes through `request_map` with `write: true`. The click that would
commit it is held back, and before the write is saved, its steps up to the send are tried on each tier to find
one that works.

`MAP_ALLOW_LOCAL=1` also allows sites on this machine (the bookshop test site:
`uv run uvicorn tests.sites.bookshop:app --port 8911`).

### From the command line (flowmap)

```bash
# have Claude record an action once (or drop --agent and do it yourself in the browser that opens)
uv run python -m flowmap record https://example.com/ --action search_things \
  --agent "search for the query" --input query=lamp --example query=chair

uv run python -m flowmap map example.com            # replay, infer, check, save to the registry
uv run python -m flowmap show example.com           # the card and recent heal timings
uv run python -m flowmap heal example.com search_things
```

Full reference: `docs/flowmap.md`. Wei Wei's mapper (`python -m mapper capture | map | heal`) is described in
`docs/mapper-design.md`.

## Healing

When an action returns `PAGE_CHANGED`, the runtime queues a heal job, checks first that the cause isn't a block
or an expired sign-in, then asks the remapper to re-map just the broken action. The new card is validated against
the action's `examples` and saved as a new version. `REMAPPER` picks the remapper:

- `flowmap`: replays the action's recording from `FLOWS_DIR/<domain>/<action>.json`.
- `mapper`: Wei Wei's mapper, replaying recordings from `CAPTURES_DIR/<domain>/`.
- `tests.sites.fixture_site.remapper:FixtureRemapper`: a stand-in for the demo shop.

## Sign-in and the browser tier

Sites with `connect_providers` need the user's sign-in once. The agent calls `start_connection` and shows the user
a link. There the user signs in to the real site in a private browser in a short-lived Neko container, streamed
to their screen. spider checks the cookies with the provider's `session_check` and stores them encrypted with
`SESSION_KEY`; the agent never sees a password or cookie. Cookies the site refreshes are saved back, and the
sign-in browser's identity (user agent, client hints, language) is presented on every later request.

- **Neko:** needs Docker and `docker pull ghcr.io/m1k1o/neko/chromium:latest`. Hosted: set `NEKO_PUBLIC_IP`, open
  `NEKO_UDP_PORTS` (UDP and TCP), and add a TURN server in `NEKO_ICE_SERVERS` for strict networks.
- **Residential proxy (optional):** set `PROXY_URL` to a sticky-session URL with `{session}` where the session id
  goes. Each sign-in keeps one home IP; public actions go direct.
- **Browser tier:** installed Chrome (`uv run playwright install --with-deps chrome` on a server); keep
  `PROFILES_DIR` and the database on persistent storage.

Every setting is described in `.env.example`.

## Tests

```bash
uv run pytest
uv run ruff check runtime schema flowmap tools tests --exclude tests/mapper
uv run ruff format --check runtime schema flowmap tools tests --exclude tests/mapper
```

Tests run in parallel. Those that need Docker, Neko or a Playwright browser skip themselves when it isn't
installed; two of Wei Wei's mapper tests need `uv sync --extra llm`.

## Security

Captures, HAR files, `state.json`, cookies, profiles, `runtime.db` and `.env` hold live sign-ins: they are
git-ignored and must never be committed. Logs show cookie, csrf and auth header values as `[redacted]`.

## Limits

- Sites that build everything in scripts with no readable request (browser replay) aren't supported.
- An action is set up from one real example, so a page layout the example didn't show can come back empty.
- Pages about a single item (a product page) yield one field so far.
- Writes on real sites are early: the safety path is tested on the demo shop and on LinkedIn.
- No per-user quotas, response cache or outgoing rate limit yet (`TODO`s in `runtime/run/executor.py`).
