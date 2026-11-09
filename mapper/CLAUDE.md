# Mapper agent instructions

You build the **mapper**: capture scripts plus the pipeline that turns captures into cards.
Read `CARD_SPEC.md` (the contract) and `docs/mapper-design.md` (the design) before writing code.

## Scope
- Work only in `mapper/`, `tests/mapper/` and `docs/mapper-design.md`.
- Do **not** create or edit `schema/`, the executor, the MCP server or the login flow. They belong to the runtime.
  - If `schema/` (the shared Pydantic models) is missing, emit plain dicts and run the save-time rules
    from the spec in `mapper/checks.py`. Switch to `schema/` once it lands, and keep `checks.py` only for
    rules the schema doesn't cover.
  - If `tools/check_card.py` exists, call it in `validate`.
  - Live validation through the executor: do not write your own HTTP replayer. Leave a clearly marked hook
    in `mapper/validate.py` and ask for the executor's function signature.
- Never change the spec. If it is ambiguous or blocks you, stop and ask, quoting the section.

## Stack
Python 3.11+, `playwright` (capture), `jsonpath-ng` (use `jsonpath_ng.ext.parse`), `anthropic`
(optional LLM steps), `pytest`. Dependencies go in `pyproject.toml`.

LLM steps (goal tie-break, output fields, descriptions) must be **optional**: with no
`ANTHROPIC_API_KEY`, use deterministic fallbacks (all scalar leaves of the first item, capped at 8;
templated descriptions). The whole pipeline must run and be tested offline.

## Safety rules
- Bundles contain live session cookies. `captures/` is gitignored; keep it that way.
- Never print or log cookie values or csrf/auth headers. Redact as `"[redacted]"` in reports.
- Tests use synthetic fixtures only, never real captures.

## Build order (finish and test each before the next)
1. **Scaffold:** `pyproject.toml`, `mapper/__init__.py`, `mapper/cli.py` (`python -m mapper ...`), pytest set up.
2. **Fixture site:** `tests/mapper/fixture_site/`, a tiny local web app (stdlib `http.server` or a small
   Flask app) with: a search page whose JS calls `GET /api/search?q=...&start=N` (paginated, with a
   `total`); a token flow where `GET /api/session` returns `{"token": ...}` that must be sent as an
   `x-token` header; an HTML page with a `<meta name="csrf" content="...">` token used by a form POST;
   a `__NEXT_DATA__` script blob; and a login cookie. This is the offline stand-in for real sites.
3. **Capture:** `mapper/capture/record.py` plus an injected `actions.jsonl` logger, producing bundles exactly
   as in the design doc. Headed by default, `--headless` with a scripted driver for tests. Enter in the
   terminal = goal mark; typing `done` ends the run.
4. **M1 + M2** with unit tests on synthetic HAR entries.
5. **M3 + M4 + M5:** from fixture bundles (m1, m2, real), produce correct `steps[]` for `search` (with the
   session-token dependency). Test that the marker is found in the raw, percent-encoded, JSON-escaped and
   Rest.li-escaped forms, and that the right filter is emitted.
6. **M6 + M7 + M8:** a full card for the fixture site that passes every save-time rule in the spec, plus
   `mapping_report.md`.
7. **Pagination:** the `page2` run → `offset:N`, `page_size`, `next_params` (with `until` from `total`).
8. **Write action:** the fixture form POST with the csrf regex extract, `requires_confirmation: true`,
   `examples: []`.
9. **Single-action re-map** (`--task`) for healing; replaying `actions.jsonl` with new markers (stretch).
10. **M0 recon** (stretch).

## Definition of done (per step)
- `pytest` passes.
- For steps 5–8: the generated card for the fixture site passes validation, and a hand-check of
  `mapping_report.md` shows every goal field with the right provenance.

## Working style
- Small commits on a branch `mapper/<topic>`; open a PR into `main` when a numbered step is done.
- When you need a decision (spec ambiguity, executor interface, LLM model choice), stop and ask with
  2–3 concrete options.
