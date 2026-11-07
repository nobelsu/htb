# htb

Runtime for website action cards: registry, executor and MCP gateway. Spec: `CARD_SPEC.md` (v0.4).
Judgement calls: `DECISIONS.md`.

```bash
uv sync
uv run pytest
cp .env.example .env   # then set GATEWAY_API_KEY and CONFIRMATION_SECRET
```

Run the fixture shop and the gateway (two terminals):

```bash
uv run uvicorn tests.fixture_site.app:app --port 8900
uv run python -m runtime.gateway --load tests/fixture_site.json
```

The MCP endpoint is `http://127.0.0.1:8765/mcp` (Streamable HTTP, `Authorization: Bearer $GATEWAY_API_KEY`).
`POST http://127.0.0.1:8900/_admin/break` moves one fixture endpoint, `/_admin/reset` restores it.

The mapper imports `schema` (`validate_card`, `Card`, `Envelope`) and saves through `runtime.registry.Registry`.
