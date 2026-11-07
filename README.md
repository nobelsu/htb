# htb

Hack the Burgh 2026: turn websites into agent-callable actions ("cards").

| Path | What | Owner |
|---|---|---|
| `docs/card-spec-v0.4.md` | Frozen card spec (the contract) | shared |
| `docs/mapper-design.md` | Mapper design: capture protocol, bundle format, M0–M8 pipeline | WeiWei |
| `mapper/` | Capture scripts and the capture → card pipeline | WeiWei |
| `tests/mapper/` | Mapper tests and the offline fixture site | WeiWei |
| `schema/`, executor, MCP server | Shared models and runtime | Nobel |

See `CLAUDE.md` for repo-wide rules. Never commit captures, HAR files, `state.json` or `.env`.
