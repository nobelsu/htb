# spider: websites → agent-callable actions (Hack the Burgh 2026)

We turn websites into "action maps" (cards): JSON describing a site's own internal HTTP calls,
so an AI agent can call `linkedin_search_companies(query=...)` as a tool, without a browser.

## The contract
`CARD_SPEC.md` is the frozen card spec. It is the single source of truth.
If code and spec disagree, the spec wins; raise a question rather than changing the spec.

## Ownership (do not edit the other side's code)
| Area | Owner | Paths |
|---|---|---|
| Mapper, capture scripts, card generation | Mapper owner | `src/spider/harmap/`, `docs/harmap.md` |
| Executor, MCP server, browser worker, login flow, shared `src/spider/card/` package | Runtime owner | everything else |

## Repo-wide rules
- Never commit captures, HAR files, `state.json`, cookies or `.env`. They contain live login sessions.
- Never print cookie values or csrf/auth headers in logs. Use `"[redacted]"`.
- Python 3.12. Pin dependencies in `pyproject.toml`.
