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
- **Low-level `Server`** with `on_list_tools` / `on_call_tool`. *Superseded:* the first version exposed one MCP tool
  per card action (input schemas from `schema.params.input_schema`) and sent tool-list-changed notifications in both
  protocol eras (via `subscriptions/listen` and, for handshake-era clients, the private `ServerSession._connection`).
  All of that was removed with the switch to generic tools (see "Generic tools").
- **Reload.** A background task polls `registry.refresh()` every second, catching cards saved by the mapper process;
  they show up through `find_actions` straight away.
- **Result shape.** The envelope is returned as JSON text content and as `structuredContent`. `isError` is `false`
  for every envelope (its `status` carries the outcome); `true` only for an unknown tool or an executor crash.
- **Auth.** A plain ASGI middleware checks `Authorization: Bearer $GATEWAY_API_KEY` (constant-time compare) on every
  HTTP request, 401 otherwise. The SDK's OAuth `AuthSettings` path was not used (it needs an issuer, scopes etc.).
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
- **H5 Heal adapter (asked).** `runtime/mapper_remapper.py` is the `Remapper` done by WeiWei's mapper; Nobel builds
  it (runtime side), calling the mapper's own `recapture` and `map_domain` without changing `mapper/`. Turned on
  with `REMAPPER=mapper`; recordings are read from `CAPTURES_DIR/<domain>/<action>/`.
  - **Only the broken actions are re-recorded (asked).** Each broken action's recordings are replayed, then the
    whole card is rebuilt from every recording. Faster than WeiWei's `mapper heal` (which replays everything); the
    runtime still validates every changed action before it goes live.
  - **Sign-in for the replay (asked).** The user's current sign-in from our encrypted store (the one made through
    the login link), else the sign-in saved inside the recording. The cookies go to a private (0600) Playwright
    storage-state file in a private temp folder that is deleted as soon as the replays finish; values are never
    logged. Cookies are written host-only for the card's `base_url` host.
  - Errors from the mapper (no recordings, goal not found, replay failed, invalid card) become `RemapError`, so
    the job fails cleanly, the action is marked degraded and the cooldown applies. Re-records run one at a time
    (the mapper stages bundles in a shared folder) in a worker thread (the mapper uses Playwright's sync API).
  - If one of several broken actions fails to replay, the ones already replayed keep their new recordings (the
    mapper keeps the old ones in `<domain>.prev/`); no card is produced.
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
- **L1 How the user logs in (asked; changed three times).** Only the **Neko login browser**: on the connect page the
  user clicks "Log in" and signs in to the real site in a real Chromium in a short-lived container, streamed over
  WebRTC. History: a screenshot-over-WebSocket stream (blurry, laggy) was replaced by Neko; a cookie-capturing
  browser extension was added as an alternative and then **removed** (asked: we won't ship an extension).
- **Agents continue by themselves.** `poll_connection` waits server-side (default 25 s, max 45 s, like AgentWeb's) and
  returns the moment the sign-in lands; agents are told to call it right after showing the link and again while
  it says `pending`. The page replaces the browser with a clear "You're signed in" panel when done.
  Because users can't see tool results, the instructions tell agents to write the sign-in link in their reply
  before polling. As a safety net, until someone opens the link `poll_connection` waits at most 6 s and returns a
  hint to send the link (an agent once started waiting without ever showing it).
  **The real fix: MCP URL elicitation.** Claude Code (2.1.317+, protocol 2026-07-28) declares URL elicitation, and
  the model kept assuming the link was visible because Claude Code does display tool results (collapsed). So for
  clients that declare it, `start_connection` returns an input-required result with a URL elicitation: the *client*
  shows the user the sign-in link with an open button; on accept it retries the call, which then waits (up to 40 s)
  for the sign-in and returns `connected`. Declining returns a plain "cancelled" message. Other clients keep the
  link-in-the-result flow above. Caveat found in testing: Claude Code declares URL elicitation but its desktop app
  and `claude -p` answer "cancel" instantly without showing anything, so "cancel" is treated as "the app couldn't
  show it": the result carries the link and tells the agent to write it and call poll_connection in the same turn. The gateway logs each client's name and elicitation support once.
  Still, agents sometimes summarised the JSON's user_message as "use the link" and dropped the URL (the unopened-link
  safety net then recovered ~30 s later). So while a link is waiting, sign-in results lead with a plain text line,
  "Show this to the user now..." followed by the link itself, before the JSON.
- **L6 Instant "decline" means the app answered (judgement call).** Since Claude Code 2.1.318 the desktop app
  answers URL prompts with "decline" instead of "cancel", still within ~10 ms and without showing anything, so the
  agent was told "cancelled" and stopped waiting. No person answers in under 2 s (`HUMAN_REPLY_MIN_S`), so a decline
  faster than that gets the same fallback as "cancel" (agent writes the link, then calls poll_connection). A slower
  decline is still a real "no".
- **Neko details.** One container per login link (`ghcr.io/m1k1o/neko/chromium`, Neko 3.2), started when the user
  clicks "Log in", removed once connected or expired; leftovers are removed at startup (label `htb.neko`).
  Chromium runs in kiosk mode on the provider's `login_url`, at 1920x1200 with device scale 1.5 (sites lay out at
  1280x800; the stream stays sharp on retina screens), ~25 fps. The client runs in Neko's `embed` mode, logged in
  with a random per-container password, and is reverse-proxied by the gateway under `/neko/{token}/` (HTTP and the
  `/api/ws` signalling WebSocket), so it is same-origin and works behind HTTPS. WebRTC media goes directly to the
  container over one published port per login (`NEKO_UDP_PORTS`), on UDP and, as a fallback, TCP (Neko's TCP mux:
  some browsers, VPNs and office networks won't do WebRTC over UDP). The advertised address (`NEKO_PUBLIC_IP`)
  defaults to `auto`, the machine's network IP detected per login: advertising `127.0.0.1` broke Firefox, which
  refuses WebRTC to loopback (`media.peerconnection.ice.loopback` is off by default). A TURN
  server can be set with `NEKO_ICE_SERVERS` for users behind strict networks.
- **Reading the login.** Every 2 s the watcher reads the browser's cookies with one browser-level DevTools command
  (`Storage.getCookies` over Chrome's WebSocket, exposed through a small in-container forwarder because Chrome binds
  DevTools to localhost) and keeps those the site would receive for `base_url`. Page-level DevTools stays disabled
  by policy, so the user can't open DevTools in the streamed browser.
- **Local development.** If the site is on the Docker host's loopback (the fixture shop), the forwarder maps the
  container's `127.0.0.1:<port>` to `host.docker.internal:<port>`.
- **Typing.** Click into a field before typing: Neko's client keeps the Tab key for its own page.
- **Connected-too-early guard.** When "Log in" starts the login browser, the provider's `session_check` is run once
  with no cookies. If it passes, it can't tell signed-in from signed-out (e.g. the mapper's placeholder picked a
  public endpoint), so that login never auto-connects: the page asks the user to click **I've signed in**, and we
  accept only if the browser's cookies changed since the login page first loaded. Otherwise auto-detection is
  unchanged, and the button just runs the real `session_check` immediately.
- **Disconnect.** `disconnect(domain, provider?)` (all of the site's providers by default) deletes the encrypted
  session, closes and deletes the user's server-side browser profile for that site, and cancels a pending login
  link (stopping its Neko browser). It is idempotent and marked destructive in MCP (`destructiveHint`); its
  description tells agents to call it only when the user asks. It does not log the user out on the site itself.
- **Generic tools only (asked; spec changed).** The gateway exposes a fixed set of tools instead of one per
  action: `find_actions(query?, site?)` and `run_action(site, action, params, confirmation_id?)`, plus the sign-in
  tools and `heal_status`. Reasons: Claude Code defers loading large tool sets ("Loaded tools" round trips cost
  seconds per call), and a fixed set scales to any number of sites; new cards appear through `find_actions` without
  a tool-list change. Search is a simple keyword score over each action's name (weighted), site, description and
  parameter names, top 10. `run_action`'s `confirmation_id` maps to the card's `_confirmation_id`. The old
  one-tool-per-action mode was removed entirely (asked), along with tool-list-changed notifications: the tool list
  never changes.
- **Plain language for users (asked).** What the agent tells the user should describe outcomes, not internals.
  The MCP server instructions ask agents not to mention tool names, status codes, sessions, cookies, browser
  profiles or cards unless the user asks how it works. The sign-in tools (`start_connection`, `poll_connection`,
  `connection_status`, `disconnect`) return a ready-made `user_message`; `disconnect` keeps its technical fields
  under `details`. Tool descriptions avoid internal vocabulary. The result envelope is unchanged (it is for agents).
- **No URL-level request logs.** The gateway runs uvicorn without its access log, filters uvicorn's WebSocket
  handshake lines and keeps httpx at WARNING: those lines would record login tokens, the login browser's per-container password and users' query parameters.
- **Verification before storing.** Cookies from the login browser are stored only once the provider's
  `session_check` passes with them (or, for a weak check, the user confirms and the cookies changed). Links are
  single-use and expire (`CONNECT_TIMEOUT_S`, default 10 min); one pending link per (user, site), so calling
  `start_connection` again returns the same link.
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
- **L7 Cookie rotation (asked; spec changed).** Nobel and WeiWei agreed the spec shouldn't forbid saving refreshed
  cookies (`CARD_SPEC.md` "Cookies" is updated). After a run that ends `SUCCESS` or `EMPTY`, and after a passing
  `session_check` (so the 15-minute keep-alive refreshes idle sign-ins too), the cookies are saved back to the
  user's encrypted sign-in, mirroring the site: changed values updated, new cookies added, deleted ones removed
  (both asked). Details (judgement calls):
  - Only cookies the site's own host gets are saved (its own and parent domains), not ones set for other sites
    during a cross-site redirect. The browser tier already reads only `base_url`'s cookies.
  - Written only if the stored cookies are still what the run started from, so a fresh sign-in or another run's
    save made meanwhile wins, and a sign-in disconnected meanwhile isn't brought back.
  - Rotation keeps the status and `connected_at` (it isn't a new sign-in); it updates `checked_at`.
  - An empty jar is never saved (it means we saw no cookies, not that the site signed us out). Nothing is written
    when nothing changed.
  - The login flow's own checks (with the login browser's cookies) save nothing here; it saves on connect.
  - Sign-ins pasted into `DEV_SESSION_COOKIES` are not rotated; they stay as written in `.env`.
  - The log says how many cookies changed, with no cookie names or values.
- **L8 Device identity (asked).** Later requests present the browser the user signed in with, so sites that tie a
  login to a device don't see a new one. Saved with each sign-in (`sessions.identity`, plain JSON: not secret):
  the login browser's user-agent and full version (DevTools `/json/version`), its platform, brand (none for Neko's
  Chromium) and Accept-Language (pinned with `--accept-lang`, `NEKO_ACCEPT_LANGUAGES`). Every tier presents it:
  `http` and `impersonate` send its user-agent, client hints (`sec-ch-ua`, `-mobile`, `-platform`) and language;
  `impersonate` uses curl_cffi's newest Chrome fingerprint not newer than it (Chromium 155 -> `chrome152`); `browser`
  overrides the tab via DevTools `Network.setUserAgentOverride` (headers and `navigator.userAgentData`), plus a
  per-tab Accept-Language (Playwright's own wins over DevTools'). Checked against the real Neko login: the saved
  identity equals what its Chromium sent the site, header for header.
  - **Requests with no saved identity (asked: real Chrome).** Public actions, `.env` sign-ins and sign-ins made
    before this present `DEFAULT_IDENTITY`: Google Chrome 152 on Windows, identical to curl_cffi's `chrome152`
    profile, instead of `python-httpx`.
  - The client-hint brand list is computed from the version with Chromium's own GREASE rule; checked against real
    Chrome 120/122/124/131 headers, curl_cffi's recorded Chrome 152 and the live Neko Chromium 155.
  - A card's own header still wins over the identity's (the mapper strips `user-agent`/`accept-language` anyway).
  - The fixture site's bot guard now keys on `sec-fetch-mode` (only a browser's network stack sends it) as its
    stand-in for a TLS fingerprint, since the `http` tier now sends a Chrome user-agent and client hints.
  - Not covered: the IP address (residential proxies, still to do), time zone, screen size and other JS-visible
    traits (only the `browser` tier runs JS); the `browser` tier's real Chrome engine version may differ from the
    version it claims.
- **L9 Several users (asked).** Each API key belongs to one user, and every tool call acts as that user (the
  executor, sign-ins, browser profiles, login links and device identities were already per user; the gateway's
  hard-coded `default` is gone). `BearerAuth` resolves the key and puts the user on the request; tools read it
  from `ctx.request`. Cards, heal jobs and health checks stay shared (they're about sites, not users).
  - **The .env key stays user `default` (asked),** so existing sign-ins keep working.
  - **Keys come from a sign-up page (asked) behind an invite code (asked)**, `SIGNUP_CODE` in `.env`; empty = no
    page (404). The form asks for a name and an email (asked; the email isn't verified, we don't send mail). The
    key is shown once with the `claude mcp add` line. Also a command line: `python -m runtime.keys add | list |
    reissue | revoke`.
  - Judgement calls: names are 2-32 of `a-z 0-9 - _` (they end up in profile folder names); `default`, `_shared`,
    `admin`, `root` are reserved. Keys are `htb_` + 32 random bytes; only their SHA-256 is stored (high-entropy
    keys don't need a slow hash). Wrong invite codes: 10 per 10 minutes per client address, then every attempt
    from it is refused until they age out (behind a proxy that's the proxy's address). The page is no-store,
    no-referrer and can't be framed. Logs name new users but never the key, the code or the email.
  - A URL sign-in prompt's retry is only accepted from the user who started it.
  - Not done: users can't see or rotate their own key on the page (lost key = `reissue` by us); no per-user
    rate limits or quotas; `heal_status` shows any job (jobs are about sites).
- **L10 Residential proxies (asked).** One setting, `PROXY_URL`, a URL template in the provider's own sticky-session
  format with `{session}` (asked: any provider). Each sign-in (user + site + provider) gets a stable id, 16 hex
  characters of a SHA-256 (providers restrict the characters), so it keeps one IP as long as the provider allows
  (asked). What goes through it (asked): everything that belongs to a sign-in, on every tier (http, impersonate,
  browser, escalations, sign-in checks) and the Neko login browser; public actions go direct (providers bill per GB).
  - Sites on this machine (localhost, 127.0.0.0/8, ::1) always go direct: a provider can't reach them
    (`ProxyConfig(local=True)` overrides it in tests).
  - Chromium can't send proxy credentials, so the login browser uses an in-container helper (`proxy_auth.py`,
    stdlib, run as root by supervisord) that adds them; the credentials are only in a 0600 file mounted for it,
    never on a command line. The login browser's temp folder (config and that file) is now deleted when it stops
    (it used to be left behind). The helper takes http:// upstream proxies only; with https/socks the login
    browser goes direct (logged). For plain-http requests it forces one request per connection so each carries
    the credentials.
  - The browser tier's warm profile browser gets the proxy when it starts (Playwright's proxy option).
  - Credentials are never logged: `ProxySpec`'s repr and every log line show `scheme://***@host:port (session ...)`.
  - Not covered: the mapper's own capture/replay browser (heal adapter) doesn't use the proxy.
- **Sharing through a tunnel (ngrok).** The MCP SDK's DNS-rebinding protection only accepts localhost `Host` and
  `Origin` headers when the gateway listens on 127.0.0.1, so through a tunnel every MCP request got 421. The gateway
  now also allows exactly `PUBLIC_BASE_URL`'s host and origin; any other host is still refused. ngrok was chosen
  over Cloudflare quick tunnels (those don't support server-sent events, which MCP streaming uses) and gives a
  fixed free domain. The video relay for the login browser is metered's free TURN (`NEKO_ICE_SERVERS`).
- **L11 Sign-in for regular Claude (OAuth) and the account page (asked).** Regular Claude (web, desktop, phone)
  adds a server as a custom connector and signs in with OAuth; its API-key header option is a limited beta. So the
  gateway is also an OAuth authorization server: the MCP SDK serves discovery, Dynamic Client Registration,
  /authorize, /token (PKCE) and /revoke; `runtime/oauth.py` is the provider and `runtime/account_page.py` the
  pages. `BearerAuth` accepts OAuth access tokens next to API keys, and its 401 names the resource metadata
  (RFC 9728), which is how Claude finds the sign-in server. The SDK has no Client ID Metadata Document support, so
  Claude uses "Register automatically" (DCR).
  - **Sign in with a password, or a key (asked).** Accounts get a password (scrypt; sign-up asks for one; name or
    email to sign in); "Sign in with an API key instead" covers the .env key (user `default`) and key-only accounts.
    The sign-in page can also create an account (invite code). Wrong passwords, keys and codes share the per-address
    limit (10 per 10 minutes).
  - **30 days (asked).** Access tokens last 1 hour; refresh tokens rotate on every use (the old one is spent) and a
    sign-in ends 30 days after it started. Revoking either token ends the whole sign-in. Codes are single use and
    last 5 minutes; the sign-in page request lasts 15 minutes (in memory: a restart means clicking Connect again).
    Tokens, codes and page sessions are stored as SHA-256 hashes only.
  - **Account page (asked: all four parts),** `/account`: sign-ins your agent is waiting for (live, every 2 s, with
    an optional browser notification), the sites you're signed in to (Disconnect), connected apps (Disconnect =
    revoke their tokens), and a new API key (the old one stops working; not for `default`, whose key is in .env).
    It's a cookie session (HttpOnly, SameSite=Lax, Secure on https), 30 days; POSTs from another origin are refused.
    Signing in for an app or signing up also signs you in to the account page.
- **L12 Page design: Spine (asked, after wireframes).** Every page the gateway serves (home, sign-up, sign-in for
  the account and for apps, account, change password, website sign-in, errors) shares `runtime/ui.py`: a full-height
  black spine on the left with the page name rotated and huge, giant stacked type on the right (fields use
  placeholders, not labels), and the main action as a full-width black bar at the bottom. Black and white, Inter
  Tight (IBM Plex Mono only for URLs and commands), stroke icons instead of symbols, no subtitles or helper lines;
  errors are one black line. The finished website sign-in turns the page black (white spine, "Done").
  Wireframes and rejected directions: https://claude.ai/artifact/99VgbA8VVt2CN56cp1cGYE.
  - The spine word scales to its length and to the window height; on phones the spine becomes a top band.
  - New with it: a home page at `/` (public: the auth check exempts exactly "/"), change password
    (`/account/password`), sign-in modes as links (`?with=key`, `?with=signup`), one-click copy for the connector URL
    and the Claude Code command, and an alerts tile for browser notifications.
  - `PRODUCT_NAME` (env, default "htb") names the product on every page until the real name is chosen.
  - **Motion (asked: all of the options).** Hover: ink fill (black sweeps in from the left and the text flips to
    white; on black bars and buttons the ink is white). Load: the spine word is unmasked and every line rises from
    behind an edge, 70 ms apart. Moments: the finished website sign-in wipes to black; a new waiting sign-in slides in
    and its Sign in button pulses; disconnecting strikes the name through and folds the row away before submitting;
    copy turns its icon into a tick. Extras: a big inverting cursor (mouse only), field underlines thicken on focus
    and the placeholder slides out as you type, bar arrows nudge, the spine word drifts on scroll. All of it is off
    under prefers-reduced-motion.
- **L13 Page design: Margin in Linen, named spider (asked, after wireframes: https://claude.ai/artifact/L1o9JnahEtjbWSh57nLBEm).**
  Replaces Spine (L12), which Nobel found too loud. Every page (`runtime/ui.py`): a left art panel where a spider
  spins orb webs (radials in a scrambled order, frame, spiral; it rests, wanders off and spins the next; old webs
  stay and fade back, the oldest go; never quite regular), a hairline, and a calm column of small IBM Plex Sans on
  the right, centred vertically. Linen palette: paper #F5EDE2, ink #2A2421, umber #6B5A35 for actions, brick
  #9C3F33 for errors; the finished website sign-in turns to the night version. Fields have labels above them;
  errors show under the field they are about (the browser's bubbles are off); server errors show as a brick line
  at the top. The product is named "spider" (PRODUCT_NAME). With reduced motion: one finished web, no movement.
  - **Home page (asked: "animated show what it does").** The three steps under "Your agent, signed in." are gone
    (Nobel: pointless). Now one headline and an agent's call typed out (`linkedin_search_companies(query=...)`),
    then its results arriving, cycling through a few sites. The examples are illustrative, not live data.
  - **No alerts (asked).** The account page's alerts tile and its browser notifications are removed; the waiting
    sign-ins already show live on the page.
- **Link security.** `/connect/` and `/neko/` routes skip the Bearer check; the unguessable, expiring, single-use token is
  their credential (the proxy only serves a token's container while that login is pending). Every other HTTP and websocket path needs the Bearer key.
- **Not handled yet:** per-user quotas (L9); the mapper's own browser doesn't use the residential proxy (L10).

## Mapper v2 (`flowmap/`)
A second mapper with no explorer: a person records each action once as a flow, the mapper replays it with planted
markers and infers the card deterministically, and the LLM only names things. Wei Wei's `mapper/` is untouched.
- **M1 Where (default).** A new package `flowmap/` (tests in `tests/flowmap/`, CLI `python -m flowmap
  record|map|heal|show`) on branch `mapper-v2`. The brief's `mapper/flows.py`, `mapper/capture.py` and
  `mapper/cli.py` would clash with Wei Wei's package (`mapper/capture/` is a package, his CLI already has `map` and
  `heal`, and `runtime/mapper_remapper.py` imports his code).
- **M2 Healed writes (asked: follow the spec).** The brief said healed writes go to `heal_candidates`; CARD_SPEC
  Healing (H4) says they go live as a new version, with `requires_confirmation` still true so every run asks.
  The spec wins: no `heal_candidates`.
- **M3 Wei Wei's helpers (asked: copy them in).** The few pure helpers worth reusing (marker encodings, request
  filters, dependency tracing, pagination, secret checks) are copied into `flowmap/` with a comment naming the
  original, so flowmap doesn't break when his code changes.
- **M4 Heal events (asked: a new table).** `heal_events` in the runtime database: `remap_start` / `remap_done` (and
  failures) per heal job, with timings, for the benchmark.
- **M5 Test sites (asked).** Mapping is aimed at real websites (as Wei Wei does), asking before any run of a write
  flow on a real site. Healing is tested on a website we build ourselves, with switchable breaks (endpoint, field,
  structure and param renamed).
- **M6 Heal plugs into the runtime (default).** `flowmap/heal.py` implements the existing `Remapper` protocol
  (`REMAPPER=flowmap`); HealManager keeps the pre-check, validation, activation and `degraded` marking.
- **M7 An agent can record (asked: "we should do it using like an agent"; overlap with Wei Wei's explorer is
  fine).** `record --agent "task"` (`flowmap/agent.py`): Claude does the action once from the accessibility
  tree, one tool call per turn; each step is saved with the role and name it used, input values become
  placeholders, and the results it names become `results_selector` (one CSS selector found in the page).
  Everything after recording is unchanged. Guards are code, not prompt: same site only, new tabs closed, no
  sign-out/delete clicks, no data-changing clicks unless `--write` (which asks first on real sites), no
  sign-in (the person signs in in the window; the recording restarts). Older page snapshots are dropped
  from the conversation to keep it small.
- **M8 Mapping runs on the server (asked, 2026-11-08; not built yet).** Map jobs run inside the gateway next to
  heal jobs, using its stored sessions, `BrowserPool`, proxy and identity (so sign-in goes through Connect,
  where Google works). Build order: flowmap on the runtime's browser and sessions; map jobs
  (`request_map`, status); explore as a map job (Claude proposes tasks, each is recorded, mapped and kept
  only if it passes; follow-ups come from returned ids). Decisions:
  - Any signed-up user may request a map, but only when the site has no card or a heal is needed (then the
    request starts the heal).
  - A site behind a login is mapped with the requester's session; examples on the shared card hold no
    personal data, and raw captures are deleted after mapping.
  - Explore tries writes too, but the action that would really commit the write never reaches the site: the
    agent does the final click and the browser holds the request back, recording it in full. The commit is
    spotted by any of: the agent saying so, the control's text (Send, Post, Place order, Pay, Book, Follow,
    Connect, Add to cart...), a GraphQL mutation or a PUT/PATCH/DELETE, or a POST form submit. Before that
    step only clear changes are blocked (many sites read with POST); after it, anything that could change data.
  - Private, undoable writes (add to cart, save an item, a preference) may really happen on the requester's
    account to reach a later step, and are undone afterwards (asked: "yes, then undo them"). Writes other
    people see (follow, connect, like, message, post, apply) and costly or final ones (order, book, pay, an
    official form) are always held back. Payment details are never entered: a page that wants a card ends the
    task there.
  - A write mapped from a held-back request has no response: the card marks it never run for real; the first
    confirmed real call clears the mark and records what it returns. Values the page makes up fresh for every
    request (idempotency ids, nonces) are marked "generate per call", not turned into params.
  - Heal re-captures writes the same way (held back), so healing never sends a write. M2 is unchanged: a
    healed write still goes live as a new version with `requires_confirmation`.
  - Whoever hits a broken action, the heal starts automatically, and the work it interrupted (a map job, an
    explore) carries on for the user once the heal is done.
  - Map jobs run in the gateway process, one at a time, separate from heal jobs; a worker of their own later.
  - Flowmap first; Wei Wei's mapper can plug in through the same `Remapper`-style seam later.
  - Step 1 built: `flowmap/browser.py` (`BrowserSetup`) opens installed Chrome without the automation flag,
    with a user's stored sign-in, its device identity and proxy; capture, `record --agent`, `map` and `heal`
    take `--user`. `Remapper.remap` gained `user=`: HealManager passes whoever's call found the break (it used
    to heal with the `default` user's sign-in).
  - Step 2 built: map jobs (`runtime/mapping.py`, `map_jobs` table, `flowmap/server.py`, `MAPPER=flowmap`).
    Agents call `request_map(site, task, action, inputs)` and `map_status(job_id, wait_seconds)`; the account
    page lists the user's last five. The requesting agent names the action and gives the input values (no
    planning call). The runtime validates the mapped card and runs every read example through the executor
    before saving version 1, as heal does; the mapper never saves. A second request for a site being mapped
    joins the same job and sees only its status (not the first user's task, values or log). Reads only.
    Addresses on this machine or a private network are refused (names are resolved first) unless
    `MAP_ALLOW_LOCAL=1`; redirects during the browser's first visit aren't checked.
  - Sign-in for a site with no card: the job stops at `needs_sign_in` with a Connect link for the page that
    asked (`ConnectionManager.start_for_new_site`). With no `session_check` yet it works like a weak check:
    the user clicks "I've signed in", which counts only once the cookies changed. The stored sign-in
    (`on_connected`) queues the job again, and it maps signed in under the provider id `provider_for(domain)`
    (the site's name). flowmap then works out `session_check` and `logged_out_signals` by sending the mapped
    action's request (then the start page) signed in and signed out; a check that can't tell them apart is
    kept as weak and reported. The runtime runs the check with the requester's sign-in before saving.
    The card is shared: other users sign in through the normal Connect flow once it exists.
  - Step 3, reads built: explore is a map job with no action (`request_map(site)`, `task` optional as what
    the user wants there). Claude proposes up to four read tasks from the start page, then follow-ups that
    take values copied from what the kept actions returned; each is recorded and mapped into one card and kept
    only if it passes (at most six). Proposals are checked in code (names, same site, inputs, change wording);
    an action with exactly the requests of one already kept is dropped as a duplicate; a task that wants a
    sign-in pauses the whole job. The runtime now keeps the actions whose examples pass and drops the rest
    (`result.dropped`), for single-action jobs too (a lone failing action still fails the job). Writes
    (hold-back, undo, never run for real) are the next piece.
  - Spec addition (asked, 2026-11-08: "Add to the spec"): placeholders `{{gen.uuid}}`, `{{gen.hex}}` and
    `{{gen.now_ms}}`, a value made fresh for each call, for what a page makes up anew for every request
    (idempotency keys, nonces). One value per name per call (all steps and retries share it). Backward
    compatible, so `schema_version` stays "0.4"; Wei Wei's mapper can emit it too.
  - Step 3, writes built (simplified, asked to go fast): every write is held back while mapping, private or
    not, so nothing is undone (doing private writes for real and undoing them is not built). `HoldBack`
    (`flowmap/holdback.py`) answers held requests itself (200, `{}`, `x-flowmap-held`): before the commit
    click only PUT/PATCH/DELETE, GraphQL mutations and form posts are held; from the commit click (a control
    whose text reads as a change in the recorder; the flow's last click in capture) every non-GET. The
    recorder and capture both use it, so heal re-captures writes held back too. A held write's steps expect
    any 2xx of any type. Explore proposes up to two writes per round (never delete, settings, payment or
    sign-out wording); the recorder gives up rather than type payment details. The runtime marks a mapped
    write `never_run` in `action_state`; `run_action` says so on its confirmation and the first real
    SUCCESS clears it. flowmap doesn't emit `gen` values yet.
  - Levels of mapping (asked, 2026-11-08: "let's just do 2"): level 1 is the site's data request (as
    before); level 2, built, reads a page rendered on the server with a `css` extract (spec addition: each
    match is `{text, href}`; `from: body` only; selectolax parses). Inference picks the page when its HTML
    holds the visible results, and prefers the request in a group that carries the input (the blank page of
    a GET form loads first). Level 3 (replaying the flow in a browser) is not built.
  - LinkedIn company search mapped at level 2 (2026-11-08): GET /search/results/companies/?keywords={query}
    read with CSS, http tier, strong sign-in check (signed out: 403 or redirect to /uas/login); a call takes
    about a second. It took four fixes, all general: (1) a recorded read that ends on an address holding
    every input becomes just that address (`url_shortcut`), since LinkedIn fetches results in place after
    typing but renders them in the HTML on a fresh load; (2) capture no longer treats a cookie whose value is
    a host name as a secret (LinkedIn's hashed www.linkedin.com out of every URL, so nothing looked
    same-site: every earlier LinkedIn mapping failed on this); (3) inference uses a redirect's target when
    a request redirects to the same place (LinkedIn's 307); (4) the sign-in check doesn't reject checks
    that end on the start page when no separate login page is known. The selector LinkedIn gets uses its
    generated class names, which change with its deploys: heal re-maps it when that happens.
  - Decision 1 widened (asked, 2026-11-08: "it should be able to remap it if its not complete mapping"): a
    site with a card can gain actions. request_map with an action it lacks maps that action; with no action
    it explores again for what the card doesn't have. What's there is never re-mapped (an action it has
    answers "exists", a broken one starts the heal). The mapper gets the card (`MapRequest.existing`) and
    adds to it, keeping its sign-in setup; the runtime checks only the new actions' examples and saves a new
    version, refusing if the card changed while the job ran. LinkedIn gained search_people this way.
  - Explore is a crawl (asked, 2026-11-08: "deeper and deeper"): from the start page, every kept action's
    results that link to a new kind of page (by path shape: /in/alice/ and /in/bob/ are one kind) have one
    such page opened, and Claude proposes what can be read there (details, tabs: posts, comments,
    reactions...). Up to 3 links deep and 12 new actions per explore. Results that carry ids instead of links
    (JSON APIs) get the earlier follow-up round from their values. Open: a deep crawl of a site behind a
    sign-in is a lot of traffic on the requester's account, and actions on people's pages put a real
    person's id in the shared card's examples (decision 2 says examples hold no personal data).
  - Decision 2 enforced (asked, 2026-11-08: "Yes, check examples"): before a card is saved, each new
    action's example values are checked (flowmap/privacy.py): emails and phone numbers by rule; Claude (low
    effort) judges the rest in context and suggests a public stand-in (a well-known company, a public
    figure's page, a common term). A stand-in is used only if the action works with it; otherwise the action
    isn't saved. The saved recording (heal replays it) gets the same values, or is deleted. Ran it on
    LinkedIn's card: get_profile_experience's example went from a private person's profile to williamhgates
    (version 4).
  - The same check covers fixed values in requests (asked, 2026-11-08): a task recorded as "open my
    profile" bakes the requester's own username or member id into the request, and the shared card would
    serve their data to everyone. Claude is shown every fixed value (path segments, query and body values);
    one that belongs to the signed-in person becomes a required input with a public example (kept only if it
    works), and personal values quoted in descriptions are replaced too. Not built: a per-site "who am I"
    action, so "my profile" needs the agent to know the user's id.
  - Inference by evidence, not just resemblance (asked, 2026-11-08: "yeah let's do these"): (1) the recorder
    points at the results (result_elements: each result's link by role and name, as the tree shows it) and
    the selector comes from those elements, so badges and formatting never have to be retyped; copying text
    stays as a fallback. (2) The blocking test: when inference has two or more candidates, the task is
    replayed with each of the top three blocked in turn; the one whose absence empties the results is the
    source (flowmap/server.py `_confirm_source`, capture `visible_without`). The page itself is tested last,
    since blocking it empties any page. Writes still use the held-back request.
  - The live page isn't the HTML (2026-11-08, from LinkedIn's profiles): a site's scripts can restructure
    the page after it loads, so the recorder's selector (from the live page) can find nothing in the HTML a
    plain request gets, though the data is there. When it finds too little, a selector is built from the
    HTML itself with the same rules (`runtime/extract.py html_selector`), for scoring candidates and for the
    card. Data kept only inside scripts (no elements) is still not read.
