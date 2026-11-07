# flowmap (mapper v2)

A person records each action once. flowmap replays it with planted markers, works out the card from what the
site sent, and saves it to the registry. The LLM only names things. Healing replays the same recording.
Decisions: `DECISIONS.md`, "Mapper v2".

## Use it

```bash
# 1. Record an action once (a browser opens; do the action, close it)
uv run python -m flowmap record https://example.com/ --action search_things \
  --input query=lamp --example query=chair --results '.result h3' --next 'a.next'

# 2. Map the site's flows into the registry (runtime.db)
uv run python -m flowmap map example.com

# 3. Look at the card and the latest heal timings
uv run python -m flowmap show example.com

# 4. Heal one action now (the gateway does this by itself with REMAPPER=flowmap)
uv run python -m flowmap heal example.com search_things
```

- `--input name=value`: a value you typed while recording becomes an input. `--example` adds another real
  value. Dropdowns need two values (they can't take a marker).
- Signed-in sites: `--auth <provider>` when recording, `--profile <dir>` when mapping (a persistent browser
  profile), or the user's sign-in from the session store when healing.
- Writes on real sites ask before they are replayed (`--yes` skips the question).
- Labels use `ANTHROPIC_MODEL` when `ANTHROPIC_API_KEY` is set, otherwise rules (`--no-llm` forces rules).

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
| `flowmap/capture.py` | Marker and real runs in Playwright; traffic tagged by step; secrets hashed |
| `flowmap/infer.py` | Picks the request, templates it by comparing runs, finds the output, pagination, writes |
| `flowmap/minimise.py` | Cheapest tier and fewest headers, by replaying with the executor |
| `flowmap/label.py` | Names and descriptions (the LLM's only job), enforced in code |
| `flowmap/assemble.py` | Card, validation, replay of a fresh example, `registry.save()` |
| `flowmap/heal.py` | The runtime's `Remapper`: re-captures and re-infers only the broken action |
