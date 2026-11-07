# Decisions

Judgement calls made where `CARD_SPEC.md` (v0.4) is silent or ambiguous.
"Asked" = answered by Nobel. "Default" = proposed and not objected to.

## Answered questions
- **D1 `action_maps` columns (asked).** `id INTEGER PK, domain, version, map_json, active, created_at, note`.
  `UNIQUE(domain, version)`; partial unique index on `domain WHERE active = 1`.
- **D2 Confirmation (asked).** The HMAC confirmation flow is built now (not stubbed). Secret in `.env`, 2-minute window.
- **D3 Write without `requires_confirmation` (asked).** A warning, not an error. `validate_card()` returns it in `report.warnings`; the card saves.
- **D4 `retry_after_s` (asked, my suggestion).** Lives in the error object: `error.retry_after_s`, from the
  `Retry-After` header (seconds), `null` when absent. The only addition to the spec's `error` shape.
- **D5 Status not in `expect.status` (asked).** A 2xx/3xx not in the list → `PAGE_CHANGED` (row 6).
  A status that *is* listed in `expect.status` (even a 4xx/5xx) is expected: the status-only rules (429, 401, 5xx,
  4xx probe) don't fire for it. Signal rules (`logged_out_signals`, `blocked_signals`) still apply.
- **D6 Probe outcome (asked).** Probe fails with the same 4xx → `PAGE_CHANGED`. Anything else (success, EMPTY,
  5xx, a different 4xx, BLOCKED...) → `ERROR`, with the probe's result in the message. No heal on unclear evidence.
- **D7 Tiers (asked; updated with the impersonate tier).** `http` and `impersonate` exist. A tier that isn't built
  yet falls back to the nearest lower one, so `browser` cards start on `impersonate` until the browser worker
  exists. `meta.tier` reports the tier that produced the result.
- **D8 Sessions (asked).** A `SessionStore` interface keyed by (user, domain, provider id). Tests use an in-memory
  store; for local dev, cookies can be loaded from `.env` (`DEV_SESSION_COOKIES`,
  `{"<domain>": {"<provider_id>": {"<cookie>": "<value>"}}}`). The remote-login flow will fill the store later.

## Schema / validation
- Card models use `extra="forbid"`: unknown fields are rejected at save time, so typos surface early.
- Step ids must be unique within an action.
- Inherited headers (`defaults.headers` + `action.headers`) are checked under each action's *effective*
  `auth_provider`, so a `session.cookies` default header with an action overriding `auth_provider: null` is rejected.
  (`check_card.py` checks `defaults.headers` only against `defaults.auth_provider`.) This is the literal spec rule.
- Examples and param defaults are validated against the param types (examples feed the 4xx probe, so they must be valid).
- `parse`/`then` are rejected on `jsonpath` extracts (they only make sense on `regex`).
- A step may not reference its own extract keys (only *earlier* steps).
- Param type checks are strict JSON types: no `"2"` → `2` coercion; `true` is not an integer.
  A `null` value for an optional param is treated as "not given".
- `BAD_PARAMS` field errors go in `error.message` as `name: problem; ...` (no extra envelope field).
- `envelope.data` is a list of dicts when `output.fields` is set, otherwise the raw extracted items.

## Registry
- `rollback(domain, version)` re-activates the existing row; it does not copy it into a new version.
- `save(..., activate=False)` stores an inactive version (for later use by heal candidates / review).
- The in-memory active copy is refreshed after every local write, and by `refresh()`, which notices writes from
  other processes (the mapper) via `PRAGMA data_version`. The gateway polls `refresh()`.
- Cards are stored as compact JSON of only the fields the mapper set, in the mapper's key order (so
  `output.fields` order is the envelope's field order), and so inheritance
  (explicit `auth_provider: null` vs absent) survives the round trip.

## Template resolver
- `urlencode` = `quote(v, safe="")` (encodes `/` too). Ordinary query values are encoded the same way, so a space
  is `%20`, never `+`. Query keys are encoded too.
- `escape_restli` values in the query keep `( ) , : ' %` raw; everything else (`&`, `=`, `#`, `/`...) is still
  percent-encoded by the query builder. `escape_restli` itself escapes `%` first, then `( ) , : '` and space;
  other characters (including non-ASCII) are left for the query builder.
- `strip_quotes` removes one surrounding pair of `"` only if both ends have one.
- `int` accepts ints, integral floats and numeric strings (whitespace trimmed); booleans and others fail.
  `offset:N` uses the same conversion.
- Text rendering of non-strings: booleans → `true`/`false`, `null` → empty, lists/dicts → compact JSON.
- Optional param with no value: a query key, header or body key (or body list item) whose template uses it is
  dropped. In the URL path it becomes an empty string.
- A missing `session.cookies.X` raises `MissingCookie` (executor maps it to `AUTH_EXPIRED`, see Executor).
- Header names are merged case-insensitively and sent lowercased.
- A string body (not an object/array) is sent as text with its placeholders substituted.
- Filter failures (e.g. `int` on `"abc"`) are `TemplateError` → `ERROR` with the message.

## Fixture site (`tests/fixture_site/`)
- `fixture_site.json` hard-codes `base_url: http://127.0.0.1:8900` for manual runs
  (`uv run uvicorn tests.fixture_site.app:app --port 8900`); tests start the site on a free port and patch `base_url`.
- The break switch (`POST /_admin/break`) moves `/api/items/{id}` to `/api/v2/items/{id}`, so `shop_get_item`
  goes from `ERROR` (unknown id, probe passes) to `PAGE_CHANGED` (probe gets the same 404).
- `add_to_cart` SKU prefixes drive write outcomes: `SLOW-` stores then stalls past the timeout, `FAIL-` 500s without
  storing, `GHOST-` answers 201 without storing.

## Executor
- **Evaluation per response**, first match wins: 429 → `RATE_LIMITED` (no retry) · 401 or `logged_out_signals` →
  `AUTH_EXPIRED` (`ERROR` if no auth) · `blocked_signals` → `BLOCKED` · 5xx/timeout → retry rules · 4xx →
  `changed_signals` then probe · status not in `expect.status` → `PAGE_CHANGED` · content type · `expect.paths`.
  Then extracts; a body that cannot be read as the extract needs (not JSON, regex match not JSON) → `PAGE_CHANGED`.
- **Write steps.** In a non-idempotent action, every non-GET/HEAD step at or after the first one is a write step
  (never retried; timeout/5xx → `UNKNOWN_OUTCOME`). GET/HEAD steps anywhere, and every step of an idempotent action,
  are retried once on timeout/5xx.
- **Transport errors.** A connect failure (request certainly not sent) is treated like a 5xx for reads, and as
  `ERROR` "write was not sent" for write steps. Other transport errors (sent, no response) are like a timeout.
- **Probe.** Uses `examples[0]`, with a fresh cookie jar, inside the same 30 s budget, and with probing (and heal
  queueing) disabled. "Same status" = the example run ended in `ERROR` because of the same 4xx, at any step.
- **`_confirmation_id`** on an action without `requires_confirmation` → `BAD_PARAMS`. The id is checked only once
  the params themselves are valid. The id format is `c_` + base64url(`expires_at.hmac_hex`); `expires_at` is a
  Unix timestamp; the HMAC (SHA-256) covers `user|domain|action|canonical_params|expires_at`. `summary` is
  `"<description> Params: <canonical_params>"`.
- **User.** A single API key means a single user, `"default"`. Per-user keys are a TODO.
- **Single-value extract errors** report `error.step` = the step that *extracted* the value (where the bad data
  came from). `EMPTY` envelopes have `error: null` and `data: []` (or `null` if `output.from` is null).
- **`until`** follows the single-value rule literally: zero matches → `EMPTY`, several → `PAGE_CHANGED`, a value
  that is not an integer → `PAGE_CHANGED`. Without `until`, a next page exists when the raw item count (before
  `limit`) is at least `page_size`. `next_params` is only set on `SUCCESS`.
- **`output.fields`**: the first match is used. Raw items that are not objects/arrays project to all-null fields.
- **Verify** runs after `SUCCESS`/`EMPTY` and after `UNKNOWN_OUTCOME`. It passes when the status is in
  `verify.expect.status`, the content type and paths check out, and the assert has at least one match with every match
  equal to `equals`. Any failure, including a timeout, → `VERIFY_FAILED` (`step: "verify"`), never retried.
  `VERIFY_FAILED` keeps the run's `data`. Verify after `UNKNOWN_OUTCOME` passing → `SUCCESS`.
- **Timeouts.** Step → action → defaults → 10 s, each attempt limited by what is left of the 30 s action budget.
  The budget being used up counts as a timeout.
- **Redaction.** Headers whose names contain cookie/csrf/xsrf/auth/token/session/api-key are logged as
  `[redacted]`; requests are only logged at DEBUG. Session cookie values are scrubbed from every error message.
  Envelopes never include headers or cookies.
- **Out of scope stubs.** `NoHealQueue` (PAGE_CHANGED → `heal: null`), and TODOs for cache, rate limits,
  EMPTY tracking, ladder escalation and the impersonate/browser tiers.

## Gateway (mcp SDK 2.3.0)
- **Low-level `Server`** with `on_list_tools` / `on_call_tool`; input schemas come from `schema.params.input_schema`
  (`additionalProperties: false`; actions with `requires_confirmation` also get an optional `_confirmation_id`).
- **Both protocol eras are served.** 2026-07-28 clients get list-changed through `subscriptions/listen`
  (`ListenHandler` + `InMemorySubscriptionBus`). Handshake-era clients get `notifications/tools/list_changed` on their
  standalone stream; `tools.listChanged` is advertised by overriding `create_initialization_options`.
- **Private SDK attribute.** To reach handshake-era clients outside a request, the gateway keeps each client's
  `Connection`, which 2.3.0 only exposes as `ServerSession._connection` (the session object is a per-request proxy).
  Isolated in `Gateway._track`; revisit when upgrading the SDK.
- **Reload.** A background task polls `registry.refresh()` every second (catching writes from the mapper process),
  rebuilds the tool table on any active-card change and notifies clients. Tool-name clashes across cards: first
  domain (alphabetical) wins; the other is skipped with a warning.
- **Result shape.** The envelope is returned as JSON text content and as `structuredContent`. `isError` is `false`
  for every envelope (its `status` carries the outcome); `true` only for an unknown tool or an executor crash.
- **Auth.** A plain ASGI middleware checks `Authorization: Bearer $GATEWAY_API_KEY` (constant-time compare) on every
  HTTP request, 401 otherwise. The SDK's OAuth `AuthSettings` path was not used (it needs an issuer, scopes etc.).
- **Runtime tools** (`connection_status`, `start_connection`, `poll_connection`, `heal_status`) are not exposed yet:
  sessions/login and healing are out of scope. `AUTH_REQUIRED`'s hint still names `start_connection` per the spec.
- `--load card.json` saves the card on startup only if it differs from the active version.

## Confirmation guidance (added after the first Claude Code test)
- In testing, the agent got `NEEDS_CONFIRMATION` and confirmed by itself, treating the user's request as approval.
  Layer 1 fix, with no envelope change: for actions with `requires_confirmation`, the MCP tool description and the
  `_confirmation_id` param description tell the agent to show the summary and wait for the user's explicit approval
  ("their original request is not approval"). `spends: true` actions also say "This action spends money." in the
  description, and their `summary` starts with "This will spend money. ".
- This is advisory: the agent still holds the confirmation id. Layer 2 (not built, needs agreement as it changes
  the spec's flow): MCP elicitation for `spends: true`, so the gateway asks the user directly and the agent
  never holds the approval; the HMAC id flow stays as the fallback for clients without elicitation.

## Healing
- **H1 Mapper hand-off (asked).** A Python interface, `runtime.healing.Remapper`:
  `async remap(card, actions, reason) -> Card | dict`. It returns the whole proposed card; the runtime picks what
  to activate. Configured with `REMAPPER=module:Class`. `tests/fixture_site/remapper.py:FixtureRemapper` is a
  stand-in for the fixture shop (asked) that probes which item endpoint answers.
- **H2 Repeat PAGE_CHANGED (asked).** While a job for the site covers the action, every PAGE_CHANGED returns that
  job's id. A different action gets its own job. One worker runs jobs one at a time, so at most one job per
  domain runs.
- **H3 EMPTY spikes (asked).** Built now. Only fresh agent-facing `EMPTY` results count; `SUCCESS` resets the
  count; other codes leave it alone. At 5 the count resets and one example with `min_items >= 1` runs in the
  background; if it is `EMPTY` too, a heal is queued (`trigger: empty_spike`).
- **Cooldown (not in the spec; approved by Nobel).** After a heal for an action ends `failed`, `cancelled` or
  `needs_review`, PAGE_CHANGED for that action returns the finished job's id with the remaining cooldown as
  `retry_after_s` (default 300 s), instead of queueing a heal on every call.
- **Pre-check.** Runs `session_check` for each provider the broken actions use, then one example of the first
  broken read. `BLOCKED`, `AUTH_EXPIRED`, `RATE_LIMITED` and `AUTH_REQUIRED` cancel the job (nothing degraded).
  If the example now *passes*, the job is also cancelled (the break was input-specific, e.g. an ambiguous SKU);
  this case is not in the spec (approved by Nobel).
- **"Pass".** For validation and health checks: every example returns `SUCCESS`/`EMPTY` with at least `min_items`.
- **Activation.** Changed actions = those whose definition differs from the active card. Actions the remapper
  adds are ignored; actions it drops are kept. Changed reads that pass go in; changed reads that fail keep the old
  definition and are marked degraded; changed writes go live (H4). A broken
  action the remapper didn't change stays degraded. No change → no new version.
- **H4 Healed writes go live (asked; spec changed).** A healed write action goes into the new version like a read
  (WeiWei's mapper decision, approved by Nobel). Writes are never executed to validate them (that would really
  post/buy/follow, and writes usually have no examples), so the job result lists them under `unvalidated`. The
  `heal_candidates` table is no longer written; it stays in the schema for history. `CARD_SPEC.md` "Healing" is
  updated to match.
- **Site-level fields** = everything except `actions` and `provenance`. A site-level change is all or nothing:
  if any read action fails its examples under it, nothing is activated.
- **Degraded** is set when an action is kept on its old definition after a failed heal, and cleared when a later
  heal activates a new definition for it. `meta.degraded` reads it on every result.
- **Health checks.** Off by default (`HEALTH_CHECK_INTERVAL_S=0`; per-domain opt-out with
  `HEALTH_CHECK_DISABLED_DOMAINS`). A no-login site is one where no action has an effective `auth_provider`.
  Actions with an open heal job are skipped. A failure queues a heal (`trigger: health_check`), which still goes
  through the pre-check.
- **Job statuses.** `queued`, `running`, `succeeded`, `failed`,
  `cancelled`. Jobs left `running` by a crash are marked `failed` on startup.
- **`heal_status(job_id)`** is an unprefixed runtime tool. It returns the job record, plus `retry_after_s` while
  the job is open.
- `Executor.run_internal` runs examples for the runtime (no probe, no heal, no EMPTY counting, confirmation
  bypassed), so it must only be used for read actions.

## Impersonate tier and ladder
- **Impersonate** uses `curl_cffi` with its current `chrome` target (Chrome TLS/HTTP2 fingerprint and default
  headers). URLs are sent exactly as our encoder built them (`quote=False`). Plain `http://` URLs use HTTP/1.1,
  because Chrome never sends an h2c upgrade. Card headers are sent on top of Chrome's defaults.
- **Escalation** happens per step, on `BLOCKED` only: the blocked step is retried on the next available tier, with the
  same cookie jar (copied by name/value onto the new tier's session for the base host), and later steps stay on
  the higher tier. Extracted values from earlier steps are kept.
- **Writes.** Reads and idempotent actions always escalate. Non-idempotent actions escalate when blocked on a step
  before the write step, or when the blocked response was not a 2xx. A 2xx challenge on or after the write step
  returns `BLOCKED` with "not re-sent" in the message.
- At the top available tier, `BLOCKED` is returned with the tiers tried. Each tier counts as a request (for
  rate limiting, once built). Heal pre-checks, health checks and probes use the same ladder.
- `session_check` (heal pre-check) starts on the card's default tier but does not escalate.
- Not recorded yet: which tier worked, in `action_state` (the spec puts that learning there). The executor always
  starts from the card's tier.

## Login flow and browser tier
- **L1 How the user logs in (asked; changed twice).** The connect page offers two ways in:
  1. **"Log in here" (default, no install):** a real Chromium in a short-lived [Neko](https://github.com/m1k1o/neko)
     container, streamed to the user over **WebRTC** (asked: option A). This replaced the first version's
     screenshot-over-WebSocket stream (blurry, laggy).
  2. **"Use my existing login" (HTB Connect extension):** shown only when the extension is installed. The user's
     own browser session is captured (`chrome.cookies.getAll`, httpOnly included) and posted back.
- **Neko details.** One container per login link (`ghcr.io/m1k1o/neko/chromium`, Neko 3.2), started when the user
  clicks "Log in", removed once connected or expired; leftovers are removed at startup (label `htb.neko`).
  Chromium runs in kiosk mode on the provider's `login_url`, at 1920x1200 with device scale 1.5 (sites lay out at
  1280x800; the stream stays sharp on retina screens), ~25 fps. The client runs in Neko's `embed` mode, logged in
  with a random per-container password, and is reverse-proxied by the gateway under `/neko/{token}/` (HTTP and the
  `/api/ws` signalling WebSocket), so it is same-origin and works behind HTTPS. WebRTC media goes directly to the
  container over one published port per login (`NEKO_UDP_PORTS`), on UDP and, as a fallback, TCP (Neko's TCP mux:
  some browsers, VPNs and office networks won't do WebRTC over UDP). The advertised address (`NEKO_PUBLIC_IP`)
  defaults to `auto`, the machine's network IP detected per login: advertising `127.0.0.1` broke Firefox, which
  refuses WebRTC to loopback (`media.peerconnection.ice.loopback` is off by default), advertised as `NEKO_PUBLIC_IP`; a TURN
  server can be set with `NEKO_ICE_SERVERS` for users behind strict networks.
- **Reading the login.** Every 2 s the watcher reads the browser's cookies with one browser-level DevTools command
  (`Storage.getCookies` over Chrome's WebSocket, exposed through a small in-container forwarder because Chrome binds
  DevTools to localhost) and keeps those the site would receive for `base_url`. Page-level DevTools stays disabled
  by policy, so the user can't open DevTools in the streamed browser.
- **Local development.** If the site is on the Docker host's loopback (the fixture shop), the forwarder maps the
  container's `127.0.0.1:<port>` to `host.docker.internal:<port>`.
- **Typing.** Click into a field before typing: Neko's client keeps the Tab key for its own page.
- **Disconnect.** `disconnect(domain, provider?)` (all of the site's providers by default) deletes the encrypted
  session, closes and deletes the user's server-side browser profile for that site, and cancels a pending login
  link (stopping its Neko browser). It is idempotent and marked destructive in MCP (`destructiveHint`); its
  description tells agents to call it only when the user asks. It does not log the user out on the site itself.
- **Plain language for users (asked).** What the agent tells the user should describe outcomes, not internals.
  The MCP server instructions ask agents not to mention tool names, status codes, sessions, cookies, browser
  profiles or cards unless the user asks how it works. The sign-in tools (`start_connection`, `poll_connection`,
  `connection_status`, `disconnect`) return a ready-made `user_message`; `disconnect` keeps its technical fields
  under `details`. Tool descriptions avoid internal vocabulary. The result envelope is unchanged (it is for agents).
- **No URL-level request logs.** The gateway runs uvicorn without its access log, filters uvicorn's WebSocket
  handshake lines and keeps httpx at WARNING: those lines would record login tokens, the login browser's per-container password and users' query parameters.
- **Verification before storing.** The gateway runs the provider's `session_check` with the posted cookies (card's
  default tier). Only if it passes are they stored; otherwise the link stays pending and the user can log in and
  retry. Links are single-use and expire (`CONNECT_TIMEOUT_S`, default 10 min). One pending link per (user, site).
- **Extension security.** Only gateway origins listed in the manifest (`externally_connectable`) can message the
  extension; it trusts the browser-reported sender origin, never the message body, for where cookies go. It
  shows the destination host on its approval page. Site access is an *optional* permission requested at connect
  time for just that site (`scheme://host/*`, plus `scheme://*.domain/*` when the site's host is a subdomain of the
  card's domain), via a user click, so the install prompt asks for no site access.
- **Pinned extension ID.** The manifest has a public `key`, so the ID is fixed and the connect page knows whom to
  message (`runtime/extension.py`). No private key is in the repo (not needed to load unpacked). Hosted gateway
  origins are added with `uv run python -m runtime.extension --origin https://...`.
- **Only name/value are stored.** Cookie domain/path are dropped (as before); the browser tier seeds the user's
  server-side profile with them for the site's `base_url`.
- **L2 Browser (asked).** The server-side browser tier uses real Google Chrome (`BROWSER_CHANNEL=chrome`),
  headless, with the plain "Chrome" User-Agent (headless Chrome otherwise says "HeadlessChrome").
- **L3 Warm (asked).** One persistent Chrome profile per (user, site, provider) under `PROFILES_DIR`, kept running
  while used and closed after `BROWSER_IDLE_S` idle. Keep-alive runs every connected session's `session_check`
  every `KEEPALIVE_INTERVAL_S` and marks logged-out ones `expired`; blocks, rate limits and errors are not a verdict.
- **L4 Cookie storage (asked).** Fernet-encrypted with `SESSION_KEY` in the `sessions` table; a wrong key reads as
  "not connected". The encrypted store is consulted first, then `DEV_SESSION_COOKIES`.
- **L5 Browser tier (asked).** Each step runs as `fetch()` in a tab on the request's origin, so it goes out as real
  Chrome. Browser-owned headers (cookie, user-agent, origin, referer, host...) are dropped. Only a redirect's final
  URL is visible, so `redirect:` signals see the final path only.
- **Link security.** `/connect/` and `/neko/` routes skip the Bearer check; the unguessable, expiring, single-use token is
  their credential (the proxy only serves a token's container while that login is pending). Every other HTTP and websocket path needs the Bearer key.
- **Not handled yet:** sites that bind sessions to the device/IP may reject the imported cookies (they fail the
  session check, so the user sees a clear error); Chrome Web Store publishing (load unpacked for now); browsers
  other than Chromium-based ones; per-user API keys (everything is user `default`).
