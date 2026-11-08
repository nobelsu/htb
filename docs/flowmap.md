# flowmap (mapper v2)

A person (or Claude, with `record --agent`) records each action once. flowmap replays it with planted markers, works out the card from what the
site sent, and saves it to the registry. The LLM only names things. Healing replays the same recording.
Decisions: `DECISIONS.md`, "Mapper v2".

## Use it

```bash
# 1. Record an action once (a browser opens; do the action, close it)
uv run python -m flowmap record https://example.com/ --action search_things \
  --input query=lamp --example query=chair --results '.result h3' --next 'a.next'

# 1b. Or have Claude do it: give the task and the values to use (needs ANTHROPIC_API_KEY)
uv run python -m flowmap record https://www.linkedin.com/ --action search_companies \
  --agent "search companies for the query" --input query=robotics --example query=fintech \
  --auth linkedin --headed --profile ./profiles

# 2. Map the site's flows into the registry (runtime.db)
uv run python -m flowmap map example.com

# 3. Look at the card and the latest heal timings
uv run python -m flowmap show example.com

# 4. Heal one action now (the gateway does this by itself with REMAPPER=flowmap)
uv run python -m flowmap heal example.com search_things
```

- `--input name=value`: a value you typed while recording becomes an input. `--example` adds another real
  value. Dropdowns need two values (they can't take a marker).
- Signed-in sites: sign in once through the gateway (its Connect page, where Google sign-in works), then pass
  `--user <name>` to `record --agent`, `map` and `heal` (with `--auth <provider>` when recording). Flowmap then
  uses that sign-in's cookies, device identity and residential proxy (`PROXY_URL`), as the gateway does; the
  cookies go straight into the browser, never into a file. `--profile <dir>` (a persistent profile you sign in
  to by hand) still works. Heal inside the gateway uses the sign-in of the user whose call broke.
- The browser is your installed Google Chrome without the automation flag (`--channel chromium` for
  Playwright's own); it falls back to Chromium when Chrome isn't installed.
- Writes on real sites ask before they are replayed (`--yes` skips the question).
- `--agent "task"`: Claude does the task once in a browser, one step at a time from the page's accessibility
  tree, and names the results and the next-page control; you don't pass `--results` or `--next`. It never
  leaves the site, never clicks sign-out or delete controls, never clicks something that changes data unless
  the action is `--write` (and then it asks first), and never signs in: when a page wants a login it waits for
  you to sign in in the window (`--headed`). Use the same `--profile` for `map`, so the sign-in carries over.
  Check the saved flow before mapping.
- Labels use `ANTHROPIC_MODEL` when `ANTHROPIC_API_KEY` is set, otherwise rules (`--no-llm` forces rules).

## Map jobs in the gateway

With `MAPPER=flowmap` (and `ANTHROPIC_API_KEY`), agents get two more tools. `request_map(site, task, action,
inputs)` asks for a site with no card to be set up; the gateway does `record --agent` and `map` itself, one job
at a time, and saves the card once its examples work through the executor. `map_status(job_id)` waits for the
result. A site that already has a card isn't mapped again: if an action is broken, the request starts its heal.
Reads only for now. Raw captures go to a temporary folder deleted when the job ends; the flow is kept in
`FLOWS_DIR` for heal. Addresses on this machine or a private network are refused unless `MAP_ALLOW_LOCAL=1`.

```bash
MAPPER=flowmap MAP_ALLOW_LOCAL=1 uv run python -m runtime.gateway   # then request_map http://127.0.0.1:8910/
```

### Pages rendered on the server (level 2)

Most actions are mapped to the site's data request (level 1). When no data request carries the results but the
page itself does (a GET form whose results come back in the HTML, a page rendered on the server), the action
fetches that page and reads the results with the recorder's CSS selector: a `css` extract (CARD_SPEC
"Extraction"), each result `{text, href}`. Inference scores HTML responses by how many of the visible results
the selector finds there, and needs at least 60% in every real run. Pages that fill their results in with
scripts from data flowmap can't see still fail; replaying the flow in a browser (level 3) isn't built.

### Explore

`request_map` with only a site (and, optionally, what the user wants to do there as `task`) explores it
(`flowmap/explore.py`). Claude looks at the start page and proposes up to four read tasks (name, task, start
page, input values); each is recorded, captured and mapped into one card, and kept only if it passes. A second
round proposes follow-ups that take a value from what the kept actions returned (an item's page by an id from
the search), with the values copied from those results. At most six actions; reads only.

Checked in code, whatever Claude says: names are snake_case and new, start pages are on the site, inputs are at
most four short strings, and a task worded as a change (follow, buy, add to cart...) is dropped before it runs.
An action that sends exactly the requests of one already kept is dropped as a duplicate. A task that wants a
sign-in pauses the whole job for it; it explores again once the requester has signed in. The runtime then
runs every example and saves the actions that pass (`result.dropped` names the others). The job's report lists
every task tried, kept or not and why.

Explore also proposes up to two writes per round (add to cart, follow, post...; never delete, settings or
payment). Writes never reach the site while mapping: `flowmap/holdback.py` holds the committing request back
(before the commit click only PUT/PATCH/DELETE, GraphQL mutations and form posts; from it, every non-GET),
answers it itself and keeps it, so it is still mapped. Capture holds writes back the same way. The saved write
is marked never run for real; `run_action` says so with its confirmation, and its first real success clears it.

A dropdown input needs two real values; when only one is given (as in an explore), the recorder takes another
option of that dropdown as the second.

### Sites behind a sign-in

When the agent finds a sign-in page, the job stops with `needs_sign_in`, and `map_status` gives the requester
a Connect link for that page (it's on their account page too). The site has no card yet, so nothing can check
the sign-in: like a site with a weak check, the user clicks "I've signed in", which only counts once signing in
has changed the site's cookies. The job then carries on by itself, mapping with their stored sign-in (provider
id: the site's name, `linkedin` for linkedin.com), and the action gets that `auth_provider`.

The card's connect provider is worked out afterwards (`flowmap/signin.py`). Each step of the mapped action that
needs no earlier step's value (example values written in), then the start page, is sent with the sign-in and
with no cookies; the first that passes signed in and fails signed out becomes the `session_check`, and what
only the signed-out response showed (a 401 or 403, a redirect to the login page) becomes its
`logged_out_signals`. If none can tell the two apart, the first that passes is kept as a weak check (reported as
`sign_in.strength: weak`). The runtime runs the check with the requester's sign-in before saving. Cookie
values never go into the card. On the test bookshop, `/orders` is behind a sign-in (reader / reader-pass).

## Try it on the test bookshop

```bash
uv run uvicorn tests.flowmap.site:app --port 8910
uv run python -m flowmap map bookshop.test --no-llm --pause 0
curl -X POST http://127.0.0.1:8910/_admin/break/endpoint      # or nested, param, field
uv run python -m flowmap heal bookshop.test search_books --pause 0
```

## Files

| Module | Does |
|---|---|
| `flowmap/flows.py` | The flow format (`flows/{domain}/{action}.json`), replay, the codegen converter |
| `flowmap/agent.py` | `record --agent`: Claude does the action once; its steps become the flow |
| `flowmap/browser.py` | The browser flowmap drives, set up like the gateway's: Chrome, a stored sign-in, identity, proxy |
| `flowmap/capture.py` | Marker and real runs in Playwright; traffic tagged by step; secrets hashed |
| `flowmap/infer.py` | Picks the request, templates it by comparing runs, finds the output, pagination, writes |
| `flowmap/minimise.py` | Cheapest tier and fewest headers, by replaying with the executor |
| `flowmap/label.py` | Names and descriptions (the LLM's only job), enforced in code |
| `flowmap/assemble.py` | Card, validation, replay of a fresh example, `registry.save()` |
| `flowmap/heal.py` | The runtime's `Remapper`: re-captures and re-infers only the broken action |
| `flowmap/server.py` | The runtime's `Mapper`: a map job (agent recording, capture, map) inside the gateway |
| `flowmap/explore.py` | Explore: Claude proposes read tasks (and follow-ups from returned ids), checked in code |
| `flowmap/holdback.py` | Holds writes back while mapping: the committing request never reaches the site |
| `flowmap/signin.py` | A new card's sign-in check and logged-out signals, from the action mapped while signed in |
