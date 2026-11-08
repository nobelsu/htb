# Mapper design

The mapper turns a **capture** of a person (or agent) using a website into a **card** (see
`CARD_SPEC.md`). The executor (Nobel's) runs the cards. The mapper never runs anything live
except during validation.

```
explorer (capture) ──▶ capture bundle ──▶ mapper M0–M8 ──▶ card JSON ──▶ validate ──▶ save
```

## Definitions

| Term | Meaning |
|---|---|
| capture | One recorded run of a task in a real, rendered browser |
| bundle | The folder one capture writes (format below) |
| HAR | JSON file of every HTTP request and response in the run |
| marker (canary) | A unique fake input (e.g. `zqxv8841`) typed during capture, used to find where user input travels |
| goal request | The single request that actually performs the task |
| field | One value inside a request, addressed as `url.path[2]`, `query.q`, `header.csrf-token`, `cookie.JSESSIONID`, `body.$.variables.keywords` |
| provenance | Where a field's value came from: `param`, `constant`, `dependency`, `session_cookie`, `unknown` |
| param (leaf) | A field whose value comes from the agent's input |
| dependency (edge) | A field whose value was in an earlier response → that request becomes an earlier step |
| step | One request template plus its `expect` and `extract` |
| card | One site's full action map (spec v0.4) |

## Capture protocol (per task)

Each task gets **3 runs**, plus a 4th if it paginates. Each run is its own bundle:

| Run | Input | Why |
|---|---|---|
| `m1` | marker A (e.g. `zqxv8841`) | Find which request carries user input, and in which encoding |
| `m2` | marker B (e.g. `kjtr5527`) | Diff against m1: same value in both = constant, different but unexplained = unknown |
| `real` | a realistic input (e.g. `palantir`) | Real response shape for output fields; becomes the card's `examples` entry |
| `page2` | the real input, then go to page 2 | Infer `offset:N` and `page_size` (only if the task paginates) |

## Bundle format

```
captures/<domain>/<task>/<run>/
  traffic.har      # Playwright record_har, content embedded
  state.json       # Playwright storage_state at the end (SENSITIVE)
  actions.jsonl    # one UI event per line, see below
  meta.json
```

`meta.json`:
```json
{
  "bundle_version": "1",
  "domain": "linkedin.com",
  "start_url": "https://www.linkedin.com/feed/",
  "task": "search_companies",
  "task_description": "Search companies by keyword",
  "run": "m1",
  "markers": {"query": "zqxv8841"},
  "real_inputs": {},
  "page": 1,
  "kind_hint": "read",
  "driver": "human",
  "goal_marks": ["2026-11-07T15:02:11.512Z"],
  "started_at": "2026-11-07T15:01:40.000Z",
  "ended_at": "2026-11-07T15:02:20.000Z",
  "mapper_version": "0.1.0"
}
```
- `markers` is filled for `m1`/`m2` and `real_inputs` for `real`/`page2`; keys are the agent-facing param names.
- `goal_marks`: timestamps when the operator pressed the "mark" key (Enter in the CLI) right after the click
  that does the task. Used by M3, especially for writes.
- `kind_hint`: `read` or `write`, as the operator states it. M7 still checks it.

`actions.jsonl` (written by an init script injected into every page):
```json
{"t": "2026-11-07T15:01:52.101Z", "type": "input", "selector": "input[name=q]", "value": "zqxv8841", "url": "..."}
{"t": "2026-11-07T15:01:53.330Z", "type": "click", "selector": "button[type=submit]", "text": "Search", "url": "..."}
{"t": "2026-11-07T15:01:53.400Z", "type": "navigate", "url": "..."}
```
`actions.jsonl` doubles as a replayable script for healing: re-run the same UI steps with new markers.
Event types: `input` (final value of a field), `click`, `key` (Enter only, so form submits replay), and
`navigate` (main frame). Password and other secret-looking fields are always logged as `"[redacted]"`.
`traffic.har` and `state.json` are written with mode `0600`.

## Pipeline

| Stage | Input → output | How | LLM |
|---|---|---|---|
| **M0 recon** (stretch) | domain → platform guess | Look for `__NEXT_DATA__`, JSON-LD, Shopify/WordPress markers (also Nuxt, GraphQL, Rest.li). Embedded JSON blobs get a suggested `regex` + `parse: "json"` extract. Informational: goes into the report and `python -m mapper recon`, never into the card | no |
| **M1 load + filter** | HAR → candidate entries | Keep same-site hosts (registrable domain match), methods any, mime JSON/HTML/text/form. Drop images, fonts, css, js, media, known analytics/ads hosts, OPTIONS preflights | no |
| **M2 normalize** | entries → fields | Address every field (see Definitions). Flatten JSON bodies to JSONPath leaves. Parse form bodies, and Rest.li `(k:v,...)` and GraphQL `variables` query values into sub-fields | no |
| **M3 goal** | fields + markers + goal_marks → goal entry | Read: entries whose fields contain a marker (in any encoding) and whose response has a list-like structure; the earliest such entry after the marker was typed, and after the last click/Enter before the goal mark when there is one. Write: the operator marks right *after* the click, so take the last same-site non-GET/HEAD entry that started after that click and before the goal mark (fallback: the first one after the mark). Ties → LLM picks with a short summary of each candidate | ties only |
| **M4 provenance** | goal + earlier entries → labelled fields + dependency edges | See algorithm below | no |
| **M5 prune + order** | edges → `steps[]` | Keep only entries reachable backwards from the goal. Topological order by capture time. Step ids are short slugs (`company`, `people`) | naming only (optional) |
| **M6 extractors + output** | steps + responses → `extract`, `expect`, `output` | Dependencies: the exact location M4 found becomes the extract (JSONPath for JSON; regex with surrounding context for HTML/text). `expect.paths`: the structural parent of each extracted path (e.g. `$.elements`, not `$.elements[0].id`). Output: find the list in the goal response that grew with results, then pick 3–8 item fields | output fields |
| **M7 annotate** | draft action → full action | Heuristics: GET = read. Non-GET to anything that looks like order, pay, checkout or purchase = `spends`, `requires_confirmation`. Writes default `requires_confirmation: true` and `idempotent: false` unless clearly idempotent (follow, set, PUT). The LLM writes `description` (one or two sentences; say read-only/safe-to-retry, or state-changing/ask first) and param descriptions | descriptions |
| **M8 assemble + validate** | actions → card | Fill site-level fields (`schema_version: "0.4"`, `tool_prefix`, `provenance`, `defaults`, `blocked_signals`). Run the save-time validation from the spec (shared `schema/` + `tools/check_card.py` when present). Live-run the examples through the executor when its interface exists | no |

### M4 provenance algorithm (core)

For each field `f` of the goal request (and recursively for each new step, max depth 4):

1. **Marker match.** If `f`'s value contains marker A (run m1) and the same field contains marker B in m2,
   then `f` is a `param`. The encoding the marker was found in decides the filter:
   raw → none · percent-encoded inside a URL path → `urlencode` · Rest.li-escaped → `escape_restli`
   · JSON-escaped → none (the executor JSON-encodes). If the marker is only part of the value
   (e.g. `(keywords:zqxv8841,start:0)`), template the rest literally.
2. **Session cookie.** If the value equals (or equals after stripping quotes) a cookie that existed in
   `state.json` before the goal request → `{{session.cookies.NAME}}`, adding `| strip_quotes` if needed.
   This needs an `auth_provider` (M8 adds `connect_providers` when any session cookie is used).
   `cookie.*` fields themselves are **never templated**: the executor's cookie jar sends them (spec,
   Cookies). A session cookie on a request only marks the action as needing an `auth_provider`.
3. **Earlier response.** Search every earlier entry (time < goal start, nearest first) in: JSON response
   bodies (record the exact JSONPath), raw HTML/text bodies (record a regex built from about 20 chars of
   stable context on each side), response headers, and `Set-Cookie`. If found → `dependency` on that
   entry, and that entry becomes a candidate step (recurse on its own fields). Prefer JSON sources over
   HTML, and the nearest in time.
4. **Constant.** Same value in m1 and m2, and not found anywhere → `constant` (template it literally).
5. **Unknown.** Differs between m1 and m2 and not found → `unknown` (likely computed in JS: a signature or
   timestamp). Timestamps (epoch s/ms close to the capture time) are flagged as such. Any `unknown` on the
   goal request → set `tier: "browser"` for the action and write a warning into the mapping report.

Ignore trivial values (length < 4, `true`/`false`, `0`/`1`, common locale and version strings) during
the earlier-response search to avoid false dependencies. A value that is the **same** in m1 and m2 is
only searched for if it looks like a token (≥ 8 chars mixing letters and digits); otherwise words such
as a `search` path segment would match the HTML of the search page. Browser-managed headers
(`user-agent`, `accept-encoding`, `referer`, `sec-*`, …) are omitted from templates: the executor's
tier sets them. A dependency found in m1 is confirmed by evaluating the same extract on m2.

Steps, requests and the rest of the card follow `action_map.example.json` exactly.

**Pagination** (`page2` run): diff the goal request against `real`. A numeric field that changed by N
→ template `{{params.page | offset:N}}`, `page_size: N`, `next_params.param: "page"`. If a total-count
field exists in the response, use it as `until` (an extract on the goal step).
In practice the page-1 and page-2 requests are both taken from the `page2` run (same session and query;
`real` is the fallback). Only a field that goes `0 → N` is an offset. A field that goes `1 → 2` is a page
number: `{{params.page}}` with no filter (`offset:N` would send 0 for page 1), and `page_size` = items on
page 1. `page` is an optional integer param, default 1, and `output.limit` = `page_size`. A total must
have a total-like name (`total` first, then `totalCount`/`numFound`/…, then `count`), be equal on both
pages and be at least the page's item count.

**Examples:** from the `real` run → `{"params": real_inputs, "min_items": 1}`.

**Form bodies** (decided 2026-11-07). The spec only shows JSON bodies. A form-encoded body
(`a=1&b=2`) is written as a **string** body, which the executor sends as-is, with the captured
`content-type: application/x-www-form-urlencoded` header. Every value is percent-encoded: literals at map
time, placeholders with `| urlencode` (so `Tom & Jerry` can't break the body). This stretches spec rule 5
("use `urlencode` only inside `url` paths") to form bodies; the validators accept it.
```json
"body": "csrf={{steps.compose.csrf | urlencode}}&text={{params.text | urlencode}}"
```

**`blocked_signals`:** default `["status:403"]`, plus a `body_contains:` marker for any challenge page
seen during capture (e.g. a captcha page returned with 200).

**`tool_prefix`:** the registrable domain minus its TLD, lowercased, `[a-z0-9_]` only, max 20 chars
(`linkedin.com` → `linkedin`).

**`connect_providers` (owned by Nobel's login flow, see "Open items").** When any request carries session
cookies, M8 adds one provider (`id` = `tool_prefix`) and sets `defaults.auth_provider` to it. The mapper
only knows logged-in traffic, so it writes the smallest schema-valid placeholder: `login_url` = the
capture's `start_url`, `session_check` = a parameter-free logged-in GET from the capture, and
`logged_out_signals: []` (a 401 already means `AUTH_EXPIRED`). The mapping report flags it.

## Healing (decided 2026-11-07, for the hackathon)

`python -m mapper heal captures/<domain>/ [--live] [--save --db runtime.db]` re-records the **whole site**
by replaying every saved recording (`actions.jsonl` → driver script; fresh markers for m1/m2, same real
inputs, each bundle's own `state.json` as the session unless `--state` is given), re-maps every action,
validates, and saves the new card as the active version. Old bundles are kept in `<domain>.prev/`.

This deliberately overrides `CARD_SPEC.md` "Healing" points 4–5 for this project: the whole site is
re-mapped (not only the broken action) and the new card replaces the old one whole, **write actions
included, with no human approval step**. The per-call `requires_confirmation` prompt is unchanged.

## Open items for Nobel

- **Healing differs from the spec (decided by WeiWei).** The mapper's `heal` re-maps the whole site and
  saves the result as the active version, write actions included, with no approval step. Nobel's
  `runtime/healing.py` still holds healed write actions back (`heal_candidates`, "writes are never
  auto-activated", around line 239); per this decision that hold-back should go.
- **Next (mapper): `Remapper` adapter.** Wire `mapper heal` in as the runtime's `runtime.healing.Remapper`
  (`async remap(card, actions, reason) -> card`), so a `PAGE_CHANGED` heal job re-records and re-maps the
  site automatically. Until then, run `python -m mapper heal captures/<domain>/ --save` by hand.

- **HIGH PRIORITY: real `connect_providers` values.** Login works through our hosted server: it generates a
  link, the user opens it from the chat and logs in to the site in a browser running on our server, and
  actions then run server-side in that logged-in session (which also gets past anti-bot checks). The
  provider's `login_url`, `session_check` and `logged_out_signals` belong to that flow. The mapper's values
  are placeholders that only keep the card valid.

## Mapping report

Every `map` run also writes `mapping_report.md` next to the card: per action, the goal request, every
field with its provenance, unknowns, warnings, and the LLM calls made. This is how we debug, and it is
demo material ("here is what we found hidden in the site").

## CLI

```
python -m mapper capture <start_url> --task NAME --run m1|m2|real|page2 \
    [--marker query=zqxv8841] [--input query=palantir] [--kind read|write] [--state path/to/state.json] \
    [--headless --script steps.jsonl] [--out captures] [--force]
python -m mapper map captures/<domain>/            # all tasks → card + report
python -m mapper map captures/<domain>/ --task NAME # one action (used by healing)
    [--live] [--save --db runtime.db] [--out DIR]
python -m mapper validate path/to/card.json [--live --state path/to/state.json]
python -m mapper heal captures/<domain>/ [--live] [--save --db runtime.db]   # replay everything, re-map, save
```

Validation = `schema.validate_card` + `tools/check_card.py` + `mapper/checks.py` (no live session value may
appear in the card). `--live` runs each read action's examples through `runtime.executor.Executor` (via the
`Executor` protocol in `mapper/validate.py`) with the capture's session; it sends real requests, so it is
opt-in. `--save` writes a valid card to `runtime.registry.Registry`. Optional LLM steps use Claude Sonnet 5.5
(`claude-sonnet-5-5`) when `ANTHROPIC_API_KEY` is set and the `[llm]` extra is installed.
