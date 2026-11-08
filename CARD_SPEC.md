# Action map ("card") spec, v0.4 (frozen)

Card `schema_version` for this spec: `"0.4"`.

One JSON file per site. Example: `action_map.example.json`. Checker: `tools/check_card.py`.
Stored in SQLite `action_maps.map_json`, one row per version, one active version per domain.

**All LinkedIn URLs, `queryId`s and JSON paths in the example are illustrative**, not just the ones marked `PLACEHOLDER`. Real values come from a capture.

## Ownership
- The **mapper** writes cards. The **executor** reads them and never edits them.
- Runtime learnings (which tier worked, failure and EMPTY counts) go in `action_state`, not in the card.
- Cards are validated against the shared Pydantic models in the importable `schema/` package before saving. The mapper and executor import the same package.
- **Owners:** MCP server, executor, browser worker and login flow are Nobel's. Mapper, capture scripts and card generation are the mapper owner's.

## Site-level fields
| Field | Meaning |
|---|---|
| `schema_version` | `"0.4"` (matches this spec) |
| `domain` | Registry key, e.g. `linkedin.com` |
| `tool_prefix` | `[a-z0-9_]{1,20}`. A short name for the site (`{tool_prefix}_{action.name}` at most 64 characters). Not used for MCP tool names since the generic tools |
| `base_url` | Fills `{{base_url}}` |
| `blocked_signals` | Signals meaning "bot-blocked" (see Signals) |
| `changed_signals` | Optional. Signals meaning "this endpoint no longer exists", e.g. `body_contains:PersistedQueryNotFound` |
| `provenance` | `mapped_at`, `source`, `mapper_version` |
| `connect_providers[]` | `id`, `login_url`, `session_check` (a request plus `expect`), `logged_out_signals` |
| `defaults` | Inherited by every action (see Inheritance) |

## Save-time validation (reject the card if any fail)
- Every placeholder namespace and filter is valid.
- `session.cookies.X` is used only where the effective `auth_provider` is not `null`.
- Every `auth_provider` matches a `connect_providers[].id`.
- Every `steps.ID.X` refers to an earlier step's `extract` key.
- Every JSONPath compiles with `jsonpath_ng.ext.parse`. Every regex compiles.
- Every read action has at least one `examples` entry, and each example includes every required param.
- If `output.next_params` is set: `param` is a real param, and `output.limit` equals `page_size`.
- Action names are unique. Param names don't start with `_` (reserved).

## Params
Each param: `type` (`string` | `integer` | `number` | `boolean` | `enum`), `required`, optional `default`, `description`, `example`, and `values` for `enum`.
- A param is never both `required` and given a `default`.
- The action's input schema is generated from `params`, and its description is `action.description`. Agents see
  them through `find_actions` and call the action with `run_action`.
- Validation failure → `BAD_PARAMS`, before any request.

## Inheritance
- **Headers:** `defaults.headers`, then `action.headers` (optional), then `step.request.headers`. Merged; the more specific value wins.
- **`timeout_ms`:** step, then action, then `defaults`. The whole action is capped at 30 s.
- **`tier`, `auth_provider`, `cache_ttl_s`, `rate_limit_per_min`:** action, then `defaults`.
- `tier` is `http`, `impersonate` or `browser`: the ladder step to try first.
- `auth_provider` is a `connect_providers[].id`, or `null` (no login needed).
- `verify` inherits exactly like a step.

## Placeholders
Syntax: `{{namespace.path}}`, `{{namespace.path | filter}}`, `{{x | filter1 | filter2}}`.

| Namespace | Value |
|---|---|
| `base_url` | Site `base_url` |
| `params.X` | Agent input, after defaults are applied |
| `session.cookies.X` | Current cookie value (see Cookies) |
| `steps.ID.X` | Value extracted by an earlier step (must be a single value, see Extraction) |
| `gen.uuid`, `gen.hex`, `gen.now_ms` | A value made fresh for each call: a random UUID v4, 32 random hex characters, the current time in milliseconds (an integer). Added 2026-11-08, see below |

Filters (a fixed set, nothing else allowed):

| Filter | Does |
|---|---|
| `urlencode` | Percent-encodes, for use in URL paths |
| `strip_quotes` | Removes surrounding `"` |
| `int` | Converts to an integer |
| `offset:N` | 1-based page to result offset: `(value - 1) * N` |
| `escape_restli` | Percent-encodes `% ( ) , : '` and spaces, for LinkedIn's Rest.li syntax |

**Values made fresh for each call (`gen`)** are for what a page makes up anew for every request:
idempotency keys, nonces, client request ids, timestamps. The mapper writes `{{gen.uuid}}` (or `hex`,
`now_ms`) where a captured value was such a value, instead of turning it into a param or copying a recorded one.
One value per name per call: every `{{gen.uuid}}` in one call (every step, header and body, and its retries)
has the same value, and the next call gets a new one. Filters apply as usual. Added to v0.4 on 2026-11-08,
backward compatible: cards without `gen` are unchanged.

Rules:
1. Resolve placeholders with a small custom resolver. **Never a template engine.**
2. **Typed body values:** if a JSON body value is *only* a placeholder, the real type is kept. Otherwise the value is text. Literal JSON values (`true`, numbers) are sent as written.
3. **Optional params** with no value and no default: drop that key entirely.
4. **Query encoding:** the executor builds the query string itself and percent-encodes each value. Exception: a value whose template uses `escape_restli` is encoded with `( ) , : ' %` left as they are, because the filter has already escaped user input, and the template's own brackets must stay raw. **Confirm against a real capture.**
5. Use `urlencode` only inside `url` paths.

## Cookies
- **`http` / `impersonate` tiers:** one cookie jar per execution, seeded from the user's session. Each response's `Set-Cookie` updates the jar, so later steps see it.
- **Cookie rotation:** after a run that ends `SUCCESS` or `EMPTY`, and after a passing `session_check`, the site's cookies as they now stand (changed, new and deleted ones; only those the site's own host gets) are written back to the stored session, so sites that rotate their login cookie stay signed in. Never after any other result.
- **`browser` tier:** the browser profile *is* the session. Cookies come from the browser context, and cookies set by responses persist in the profile, just as for a real user. This is expected.
- `session.cookies.X` reads from whichever applies.

## Extraction
| `type` | Fields | Notes |
|---|---|---|
| `jsonpath` | `expr` = JSONPath | `jsonpath_ng.ext.parse` (supports filters like `[?(@.type=="company")]`) |
| `regex` | `expr` = regex, optional `parse: "json"`, optional `then` = JSONPath | Every match's capture group 1 (like `re.findall`). With `parse: "json"`, each is parsed as JSON; `then` picks from each, and the results are flattened into one list |
| `css` | `expr` = CSS selector | HTML pages only (`from: body`); each match is `{text, href}` (below) |

- `extract.from` is `body`, `headers` or `cookies`.
- **Every extract produces a list of matches.**
  - Used as `output.from` → the whole list.
  - Used in a placeholder or as `until` → must be **exactly one** match. Zero matches → `EMPTY` (not found); more than one → `PAGE_CHANGED` (the mapping is ambiguous).
- **`expect.paths` should check structure, not content:** use `$.elements`, not `$.elements[0].id`. A missing item is `EMPTY`, a missing structure is `PAGE_CHANGED`.
- `expect.paths` are always JSONPath, and each must have at least one match.
- `output.fields` paths are evaluated relative to each item. **A field with no match is `null`**, not an error.
- **`css`** (added 2026-11-08, for pages that render their results on the server): `expr` is a CSS selector,
  `from` must be `body` (an HTML page), no `parse`/`then`. Each matching element becomes
  `{"text": its text with whitespace collapsed, "href": its own href, else its first link's, else null}`, so
  `output.fields` read them as `$.text` and `$.href`. A selector that matches nothing gives an empty list.

Examples:
```json
"csrf":      { "from": "body", "type": "regex", "expr": "name=\"csrf\" content=\"([^\"]+)\"" }
"page_data": { "from": "body", "type": "regex", "expr": "<script id=\"__NEXT_DATA__\"[^>]*>(.*?)</script>", "parse": "json", "then": "$.props.pageProps" }
```

## `expect`
- `status`: list of allowed codes.
- `content_type`: `json` | `html` | `text` | `any`.
- `paths`: JSONPaths that must each match at least once.

## Signals
Used in `blocked_signals`, `changed_signals` and `logged_out_signals`. Any one match counts.

| Signal | Matches when |
|---|---|
| `status:<code>` | Final status equals `<code>` |
| `redirect:<prefix>` | Any redirect hop's `Location`, or the final URL path, starts with `<prefix>` |
| `body_contains:<text>` | The raw body contains `<text>` (for challenge pages that return 200) |

Redirects are followed, and the executor records every hop.

## Pagination: `output.next_params`
```json
"next_params": { "param": "page", "increment": 1, "page_size": 10, "until": "steps.search.total" }
```
- `param`: the agent-facing page param. `increment`: how much it grows per page.
- `page_size`: items per site page. `until` (optional): an extracted total-results value.
- **Next page exists when:**
  - `until` is set → `param × page_size < until`
  - `until` is absent → the site returned exactly `page_size` items.
- If so, the envelope's `next_params` = current params with `param += increment`. Otherwise `null`.
- `offset:N` stays in the request template, converting the page number to an offset at request time.
- **The executor never loops pages by itself**; the agent decides whether to call again.

## Result codes
Evaluated **in this order**, first match wins:

| # | Condition | Code | Executor does |
|---|---|---|---|
| 0 | Params fail validation, or a bad or expired `_confirmation_id` | `BAD_PARAMS` | Return the field errors |
| 0 | `auth_provider` set but the user has no session | `AUTH_REQUIRED` | Hint: `start_connection` |
| 0 | `requires_confirmation` and no `_confirmation_id` given | `NEEDS_CONFIRMATION` | Send nothing; return `confirmation` |
| 1 | 429 | `RATE_LIMITED` | Back off. Return `retry_after_s` |
| 2 | 401, or a `logged_out_signals` match | `AUTH_EXPIRED` | Hint: reconnect. (If the effective `auth_provider` is `null`, return `ERROR` instead) |
| 3 | A `blocked_signals` match | `BLOCKED` | See escalation rules. No heal |
| 4 | Timeout or 5xx on a read step, or anywhere in an `idempotent` action | `ERROR` | Retry that step once |
| 4 | Timeout or 5xx on the **write step** of a non-idempotent action | `UNKNOWN_OUTCOME` | **Never retry.** If the action has `verify`, run it: pass → `SUCCESS`, fail → stays `UNKNOWN_OUTCOME` |
| 5 | 4xx matching `changed_signals` | `PAGE_CHANGED` | Return now, queue a heal |
| 5 | Any other 4xx (400, 404, 405, 410...) | `ERROR` or `PAGE_CHANGED` | **Probe:** run one of the action's examples (reads only; one probe, never probed again). If the example fails with the same status → the endpoint itself is gone → `PAGE_CHANGED`. If the example works → the input was bad → `ERROR`. No examples (writes) → `ERROR` |
| 6 | 2xx/3xx but wrong content type, a required path missing, or a single-value extract with **more than one** match | `PAGE_CHANGED` | Return now, queue a heal in the background |
| 7 | A single-value extract with **zero** matches (structure fine, item not found) | `EMPTY` | Stop the remaining steps; return empty data |
| 8 | All checks pass, list empty | `EMPTY` | Return empty data. Update EMPTY tracking |
| 9 | All checks pass | `SUCCESS` | Return data |
| — | `verify` assert fails | `VERIFY_FAILED` | Report. Never auto-retry a non-idempotent write |

**Only `PAGE_CHANGED` (including the 4xx probe and `changed_signals`) and confirmed EMPTY spikes trigger healing.**

- **Write step** = the first step whose method is not `GET` or `HEAD`. Steps before it are safe to retry.
- **`verify`** runs after every successful run of an action that has one, and after `UNKNOWN_OUTCOME`.

### Escalating on `BLOCKED`
- **Reads, and idempotent writes:** go up the ladder (`http` → `impersonate` → `browser`) and retry from the blocked step. Later steps stay on the higher tier.
- **Non-idempotent writes:** escalate only if the block happened on a step **before** the write step (see the definition above), or the response matched `blocked_signals` without a 2xx (so the write never reached the site). Otherwise return `BLOCKED` and **do not re-send**.
- Already at the top tier (`browser`) → return `BLOCKED`.

## Result envelope
```json
{
  "status": "SUCCESS",
  "data": [ { "name": "Palantir", "subtitle": "Software", "url": "..." } ],
  "next_params": { "query": "palantir", "page": 2 },
  "confirmation": null,
  "heal": null,
  "meta": { "ms": 640, "tier": "browser", "cached": false, "degraded": false, "map_version": 3, "run_id": "r_abc" },
  "error": null
}
```
- `data`: always a list of items (truncated to `output.limit`), or `null` when `output.from` is `null`.
- `error`: `{ "code", "message", "hint", "step" }`. `step` is the failing step's `id` (`"verify"` for verify, `null` before any request).
- `heal` (when `PAGE_CHANGED`): `{ "job_id": "h_123", "retry_after_s": 30 }`. The agent can call `heal_status(job_id)`, or simply retry after that time.
- `confirmation` (when `NEEDS_CONFIRMATION`): `{ "id", "expires_at", "summary" }`.

## Caching
- **Key** = `(domain, action, canonical_params, session_id)`. `session_id` is `null` only when the effective `auth_provider` is `null`. One user's cached results can never be served to another.
- **Only `SUCCESS` and `EMPTY` results are cached.** Never errors, `PAGE_CHANGED` or confirmations.
- Writes (`kind: write`) are **never cached**, whatever `cache_ttl_s` says.
- **`canonical_params`** = params after defaults are applied, excluding `_`-prefixed keys, serialised as JSON with sorted keys and no whitespace. Used for the cache key and confirmation IDs.

## Confirmation
1. The first call to an action with `requires_confirmation: true` sends nothing to the site. It returns `NEEDS_CONFIRMATION` and `confirmation: { "id", "expires_at", "summary" }`. `summary` is the action description plus the params, for the agent to show the user.
2. `id = HMAC(server_secret, user | domain | action | canonical_params | expires_at)`, encoded together with `expires_at`. The validity window is 2 minutes, and `server_secret` lives in `.env`.
3. The second call passes `_confirmation_id` as an extra tool argument, **with identical params**. The executor recomputes the HMAC. A mismatch or an expired ID → `BAD_PARAMS`.

## Rate limits and safety
- Rate limits apply **per (domain, session)**. No-login sites use one shared bucket per domain.
- **Every outgoing request counts:** each step, each retry, each `verify`, each `session_check`, each health check, each ladder escalation.
- **Redaction:** cookie values and `csrf`/auth headers never appear in the envelope, events or stored traces. Replace them with `"[redacted]"`.

## Healing
1. **Scheduled health checks: no-login sites only.** They run each action's `examples` (pass = at least `min_items` items; fail is handled like `PAGE_CHANGED`). Login sites rely on errors from real runs, to save rate limit and ban risk.
2. **EMPTY spikes:** `action_state` tracks consecutive fresh (not cached) `EMPTY` results per action. After **5 in a row** for an action whose `examples` normally return items, run one example. If that example is also `EMPTY` → queue a heal.
3. **Pre-check:** a heal job first runs the `session_check` (login sites) and one of the broken action's examples, if it has any. If that returns `BLOCKED`, `AUTH_EXPIRED` or `RATE_LIMITED`, the heal is cancelled with that code and nothing is marked degraded. A challenge page can look like a changed page, and re-mapping won't fix it.
4. **On a heal:** one job per domain at a time → re-capture → re-map **only the broken action** → validate **only the changed actions** against their `examples` → save as a new site version (unchanged actions copied as-is).
   - If the heal changes **site-level fields** (`defaults`, `connect_providers`, `blocked_signals`), validate **every** read action, because they all inherit those fields.
5. **Activation:** the new site version is always built action by action:
   - Healed **read** actions that pass their examples → go into the new version.
   - Healed **write** actions → **go into the new version too** (changed 2026-11-07, decided by Nobel and WeiWei; previously they were held in `heal_candidates` for approval). They are **never executed to validate them**, since that would really perform the write; the heal result lists them as `unvalidated`.
   - Any action that fails validation → the new version keeps the old action.
   - Actions kept on their old definition after a failed heal are marked `degraded` in `action_state`. They still run, and the envelope shows `meta.degraded: true` so the agent knows results may fail.
   - If nothing changed in the end, no new version is saved.

## Runtime tools (not part of the card)
*Changed 2026-11-07 (decided by Nobel): generic tools only.* The MCP server exposes a small fixed set of tools:
`find_actions(query?, site?)` (search the actions of every active card; returns each action's site, description and
parameters), `run_action(site, action, params, confirmation_id?)` (runs one action and returns the result envelope),
`connection_status(domain)`, `start_connection(domain)` → URL, `poll_connection(domain)`, `disconnect(domain)` and
`heal_status(job_id)`. Sites are identified by their `domain`. There is no longer one MCP tool per action.

## Mapper notes
- **Search marker:** search with a distinctive marker value, then look for it in raw, URL-encoded, JSON-escaped and Rest.li-escaped forms. The form found decides the filter (found Rest.li-escaped → `escape_restli`).
- **`offset:N` and `page_size`:** capture page 1 and page 2. If `start=0` becomes `start=10`, emit `offset:10` and `page_size: 10`.
- **`blocked_signals`:** add `body_contains:` markers when a challenge page is seen during capture.

## Demo note
Scheduled health checks would heal the demo shop by themselves before the live break-and-heal moment. Make the health-check interval configurable, and turn it off for the demo site during judging.
