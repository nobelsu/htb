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
- **D7 Tiers (asked).** Only `http` exists. Cards whose effective tier is `impersonate`/`browser` run on `http`;
  `meta.tier` reports `"http"`. Ladder escalation is a TODO, so `BLOCKED` is returned directly.
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
- Cards are stored as canonical JSON (sorted keys) of only the fields the mapper set, so inheritance
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
