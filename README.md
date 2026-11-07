# htb

Hack the Burgh 2026: turn websites into agent-callable actions ("cards").
Spec: `CARD_SPEC.md` (v0.4, frozen). Runtime judgement calls: `DECISIONS.md`.
Mapper design: `docs/mapper-design.md`.

| Path | What | Owner |
|---|---|---|
| `CARD_SPEC.md`, `action_map.example.json` | Frozen card spec and its example card | shared |
| `schema/` | Card and envelope models, `validate_card()` | Nobel |
| `runtime/` | Executor, registry, MCP gateway | Nobel |
| `tools/check_card.py` | Standalone card checker | Nobel |
| `tests/` (except `tests/mapper/`) | Runtime tests and the fixture shop | Nobel |
| `mapper/`, `tests/mapper/`, `docs/mapper-design.md` | Capture and capture → card pipeline, its tests and fixture site | WeiWei |

## Setup

```bash
uv sync
uv run playwright install chromium   # browser for mapper captures
uv run pytest
cp .env.example .env   # then set GATEWAY_API_KEY and CONFIRMATION_SECRET
```

## Runtime

Run the fixture shop and the gateway (two terminals):

```bash
uv run uvicorn tests.fixture_site.app:app --port 8900
uv run python -m runtime.gateway --load tests/fixture_site.json
```

The MCP endpoint is `http://127.0.0.1:8765/mcp` (Streamable HTTP, `Authorization: Bearer $GATEWAY_API_KEY`).
`POST http://127.0.0.1:8900/_admin/break` moves one fixture endpoint, `/_admin/reset` restores it.

The mapper imports `schema` (`validate_card`, `Card`, `Envelope`) and saves through `runtime.registry.Registry`.

## Mapper

```bash
uv run python -m mapper capture <start_url> --task NAME --run m1|m2|real|page2 [--marker query=zqxv8841]
uv run python -m mapper map captures/<domain>/
uv run python -m mapper validate path/to/card.json
uv run python -m mapper heal captures/<domain>/ --save   # site changed: replay all recordings, re-map, save
```

Captures, HAR files, `state.json` and `.env` hold live login sessions: never commit them.
