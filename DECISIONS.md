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
  A 4xx that *is* listed in `expect.status` counts as expected and skips rows 1–5.
- **D6 Probe outcome (asked).** Probe fails with the same 4xx → `PAGE_CHANGED`. Anything else (success, EMPTY,
  5xx, a different 4xx, BLOCKED...) → `ERROR`, with the probe's result in the message. No heal on unclear evidence.
- **D7 Tiers (asked).** Only `http` exists. Cards whose effective tier is `impersonate`/`browser` run on `http`;
  `meta.tier` reports `"http"`. Ladder escalation is a TODO, so `BLOCKED` is returned directly.
- **D8 Sessions (asked).** A `SessionStore` interface. Tests use an in-memory store; for local dev, cookies can be
  loaded from `.env` (`DEV_SESSION_COOKIES`). The remote-login flow will fill the store later.

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
