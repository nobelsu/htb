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
| **M1 load + filter** | HAR → candidate entries | Keep same-site hosts (registrable domain match), methods any, mime JSON/HTML/text/form, and streamed data rows (`<hex id>:<JSON>` per line, React Server Components; recognised by shape, kind `stream`). Drop images, fonts, css, js, media, known analytics/ads hosts, OPTIONS preflights | no |
| **M2 normalize** | entries → fields | Address every field (see Definitions). Flatten JSON bodies to JSONPath leaves. Parse form bodies, and Rest.li `(k:v,...)` and GraphQL `variables` query values into sub-fields | no |
| **M3 goal** | fields + markers + goal_marks → goal entry | Read: entries whose fields contain a marker (in any encoding) and whose response has a list-like structure; the earliest such entry after the marker was typed, and after the last click/Enter before the goal mark when there is one. Write: the operator marks right *after* the click, so take the last same-site non-GET/HEAD entry that started after that click and before the goal mark (fallback: the first one after the mark). Ties → LLM picks with a short summary of each candidate | ties only |
| **M4 provenance** | goal + earlier entries → labelled fields + dependency edges | See algorithm below | no |
| **M5 prune + order** | edges → `steps[]` | Keep only entries reachable backwards from the goal. Topological order by capture time. Step ids are short slugs (`company`, `people`) | naming only (optional) |
| **M6 extractors + output** | steps + responses → `extract`, `expect`, `output` | Dependencies: the exact location M4 found becomes the extract (JSONPath for JSON; regex with surrounding context for HTML/text). `expect.paths`: the structural parent of each extracted path (e.g. `$.elements`, not `$.elements[0].id`). Output: find the list in the goal response that grew with results, then pick 3–8 item fields. Streamed answers: the result rows are the layout that grew from the marker run, richest in readable values once their row pointers are filled in; the `items` extract is a `regex` + `parse: "json"` + `resolve: "rows"` that matches exactly those rows, and the LLM names the fields (fallback: link + text) | output fields |
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

The whole site is re-mapped (not only the broken action) and the new card replaces the old one whole,
**write actions included, with no human approval step** (`CARD_SPEC.md` "Healing" and Nobel's
`runtime/healing.py` were changed to match, DECISIONS.md H4). The per-call `requires_confirmation`
prompt is unchanged.

## Explorer: no human in the loop

`python -m mapper explore <start_url> --state login.state.json [--tasks "search companies" ...]
[--include-writes] [--save]` replaces the human operator. The LLM is just another **driver**: it moves
the browser and takes the goal mark, and writes the same bundles a person would. Nothing in exploration
reads network traffic; finding the goal and decoding stay in `map`.

1. **Survey.** Open the start page logged in, plus up to 4 same-site pages it links to (never log-out,
   delete or similar links), and take a text snapshot of each: headings, links, fields and buttons.
   Snapshots never contain field values.
2. **Pick tasks (LLM).** From the snapshots the LLM lists the 3–6 tasks people come to *this* site for
   (search people/companies on a network, search products/add to cart on a shop), each with a start page,
   the inputs a user would type (with a realistic example), read/write, and whether results paginate.
   `--tasks` names the tasks instead, in plain English.
3. **Live run (LLM driver, `explore/agent.py`), recorded as `real`.** The LLM does the task with the
   example input, one action at a time (click, type, press, navigate), choosing each from the page's
   accessibility tree (roles and names; typed field contents are removed). It is told to apply any
   filter the task names. It flags the one action that performs the task, and the goal mark is taken the
   instant that action is done, with no LLM call in between. When it says it is done, a separate LLM
   check confirms the final page shows the task's result and that the flagged action is the one that
   loaded it, and (for paginated reads) names the next-page control. A failed attempt is thrown away and
   redone from the start page with the reason (up to 3 attempts). Log-out/delete-style controls are refused.
   A control the site hides behind a styled label (a checkbox inside a filter pill) is clicked through its
   visible label, as a person would. Claude may go to a page by address, but never to one carrying the
   task's input (replays only swap typed text). The same failing action twice gets a firm "do something
   else"; a third ends the attempt. If everything was done right but the site found nothing for the
   example input, that is not counted as a failure: Claude picks a new example (up to 3) and the run is
   redone. Task discovery asks for examples that certainly return results (a well-known name, not a phrase).
   **Filter first, search last:** the replays use made-up words that find nothing, so Claude is told to set
   any category or filter before it types and submits the input, and the final check rejects a run whose
   flagged action needs results on screen (a filter clicked on the results page).
4. **Replays.** The `real` run's `actions.jsonl` becomes a driver script exactly as in healing (typed
   inputs → `{{params.NAME}}`, marks where they were), recorded again with fresh markers as m1 and m2, and
   with the next-page click and a second mark added as page2. The three replays run at the same time, and
   start on the page where the live run began working (page loads it only passed through, like the feed
   before it went to the companies search page, are skipped). Replays use the same per-step timeout as the
   live run. If a replay breaks (a step that only exists when there are results), that whole try is thrown
   away and counts as failed; Claude is told which step broke, and the live run is redone.
5. **Check + map.** M3 must find a goal in m1 (on the saved files, after the browser is closed); then
   `map` builds the card as usual.

**Driver timing** (all drivers that run scripts, so replays and heal too): the goal mark comes right
after the triggering click/Enter. The run then waits until the requests that action started (within
0.5 s of it, the same grace M3 gives the goal) are answered, at most 10 s, so the answer lands in the HAR;
background traffic is ignored. After typing it waits the same way for the lookups the typing started
(at most 2 s), so suggestions finish before the goal window opens. After any other click, Enter or page load the driver pauses a flat 1 s
instead of waiting for the network: busy sites keep prefetching pages, tracking and running bot-check
sensors in the background, which never changes what is on screen (the HAR still records all of it).

**Parallel:** up to `--parallel` tasks (default 4) are explored at the same time, each with its live run
then its replays, and never more than `--max-browsers` browsers (default 8) open at once. Every browser is
another session of the same account, so lower both on sites that are quick to flag bots. After typing, the driver pauses until the page settles (at most 2 s),
like a person glancing before pressing Enter, so suggestion lookups happen before the goal window opens.
Connections a page holds open for good (event streams, websockets) don't count as loading.

Write tasks are skipped unless `--include-writes` is given: exploring one performs it for real, about
three times (real + m1 + m2), more if an attempt has to be redone. The explorer's LLM calls use Claude
Opus 5.5 at high effort (the mapper's own optional LLM steps stay on Claude Sonnet 5.5 at low effort);
`explore` needs `ANTHROPIC_API_KEY` (set it in `.env`, which the mapper CLI reads; see `.env.example`),
and the rest of the pipeline still runs offline. Everything the LLM did and decided (tasks, every live
attempt's actions and why it failed, the replay script) is saved in `captures/<domain>/explore.json`
and summarised in the mapping report.

## Open items for Nobel

- **Please review: spec addition `resolve: "rows"` (made on the mapper side, 2026-11-07).** LinkedIn's web app
  answers searches with streamed React Server Components rows (`<hex id>:<JSON>` per line, labelled
  `application/octet-stream`), and one result is spread over ~4 rows that point to each other. The frozen
  `regex` extract reads rows one at a time, so complete results were impossible. Added, all optional and
  backwards compatible: `resolve: "rows"` on `regex` extracts (needs `parse: "json"`, `from: "body"`) in
  `CARD_SPEC.md` (Extraction, Streamed rows), `schema.card.Extract` (+ validator), `runtime/extract.py`
  (`stream_rows`, `resolve_rows`, used by `run_extract`; the mapper imports the same functions), and
  `tools/check_card.py`; test in `tests/test_units.py`. Also new from the mapper: read actions that use
  POST (explorer recordings set read/write themselves), and goal steps with `content_type: "any"`.

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
