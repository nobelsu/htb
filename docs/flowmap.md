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
Reads only for now, and sites that need a sign-in stop with `needs_sign_in` (signing in to a site that has no
card yet isn't built). Raw captures go to a temporary folder deleted when the job ends; the flow is kept in
`FLOWS_DIR` for heal. Addresses on this machine or a private network are refused unless `MAP_ALLOW_LOCAL=1`.

```bash
MAPPER=flowmap MAP_ALLOW_LOCAL=1 uv run python -m runtime.gateway   # then request_map http://127.0.0.1:8910/
```

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
