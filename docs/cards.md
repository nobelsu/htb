# Cards

A **card** is one JSON file that describes what an agent can do on one website. It lists the site's own
internal requests as **actions**: what to send, where the inputs go, and where the results are in the answer.
The runtime reads a card and runs its actions. No browser is needed unless the card asks for one.

`CARD_SPEC.md` is the full format (v0.4). This page explains it with a real card and lists the cards in
this repository.

## The cards in this repository

`cards/` holds the cards spider runs today. Every action in them was checked against the live site on
2026-11-08; ones that didn't work were left out.

| Card | Actions | Sign-in |
|---|---|---|
| `cards/linkedin.com.json` | Reads: `search_companies`, `search_people`, `search_jobs`, `search_posts`, `get_profile`, `get_profile_education`, `get_profile_experience`, `get_company`, `get_me`. Writes: `follow_company`, `send_connection_request`, `start_conversation` | Your LinkedIn account |
| `cards/amazon.co.uk.json` | Reads: `search_products` (title and link), `get_product_details` (title) | None |
| `tests/sites/fixture_site.json` | 21 actions on the demo shop, one for each runtime case | `demo` / `demo-password` |

Load them when you start the gateway (each is saved as a new version only if it changed):

```bash
uv run python -m spider.gateway --load cards/linkedin.com.json --load cards/amazon.co.uk.json
```

Check a card without running anything:

```bash
uv run python -m spider.card.check cards/linkedin.com.json
```

A card holds no cookies, passwords or anyone's data. Each user signs in to the site themselves (the agent
calls `start_connection`), and the runtime fills the sign-in in at run time.

## A card, piece by piece

The Amazon card, shortened:

```json
{
  "schema_version": "0.4",
  "domain": "amazon.co.uk",
  "base_url": "https://www.amazon.co.uk",
  "connect_providers": [],
  "defaults": {"tier": "http"},
  "actions": [
    {
      "name": "search_products",
      "description": "Searches the store's product catalog by keyword. Each result gives the product title and a link to its product page.",
      "kind": "read",
      "spends": false,
      "idempotent": true,
      "requires_confirmation": false,
      "params": {
        "query": {"type": "string", "required": true, "description": "Search keywords", "example": "laptop"}
      },
      "steps": [
        {
          "id": "s",
          "request": {"method": "GET", "url": "{{base_url}}/s", "query": {"k": "{{params.query}}"}},
          "expect": {"status": [200], "content_type": "html"},
          "extract": {
            "items": {"from": "body", "type": "css", "expr": "a.a-link-normal.s-line-clamp-3"}
          }
        }
      ],
      "output": {"from": "steps.s.items", "fields": {"title": "$.text", "product_url": "$.href"}},
      "examples": [{"params": {"query": "laptop"}, "min_items": 1}]
    }
  ]
}
```

### The site

| Field | What it is |
|---|---|
| `domain` | The site the card is for. There is one active card per domain. |
| `base_url` | Where requests go; steps write `{{base_url}}/...`. |
| `connect_providers` | How a user signs in, if the site needs it: where to sign in (`login_url`), a cheap request that proves a sign-in still works (`session_check`), and what a signed-out answer looks like (`logged_out_signals`). Amazon needs none; LinkedIn has one. |
| `blocked_signals`, `changed_signals` | Answers that mean "you've been blocked" (a CAPTCHA page) or "the site changed". They turn into `BLOCKED` and `PAGE_CHANGED` results. |
| `defaults` | Settings every action inherits unless it sets its own, such as `tier` and `auth_provider`. |
| `provenance` | When and how the card was made. |

### An action

| Field | What it is |
|---|---|
| `name`, `description` | What agents see in `find_actions`. The description says what the action does and what each result contains. |
| `kind` | `read` or `write`. A write changes something (follow, send, buy). |
| `spends`, `idempotent`, `requires_confirmation` | Safety flags. An action that spends money or isn't safe to repeat must need confirmation: its first call returns `NEEDS_CONFIRMATION` with a summary, and only a second call with the user's approval sends it. |
| `params` | The inputs, each with a type, a description and a public example value. |
| `steps` | One or more requests, run in order. A later step can use what an earlier one extracted. |
| `output` | Which extracted value is the result, and the fields of each item. |
| `examples` | Real inputs that must return at least `min_items` results. They are used to check the action when it's saved, healed or health-checked. Writes have none, because running them would really do the write. |
| `tier` | How requests go out: `http` (plain), `impersonate` (a real browser's TLS fingerprint) or `browser` (a real Chrome tab with the user's session). The runtime moves up a tier only when a site blocks the lower one. |

### A step

`request` is the HTTP request, written with placeholders:

| Placeholder | Filled with |
|---|---|
| `{{params.query}}` | An input the agent passed. |
| `{{steps.s.items}}` | A value an earlier step extracted. |
| `{{session.cookies.JSESSIONID}}` | A cookie from the user's own sign-in (never stored in the card). |
| `{{gen.uuid}}`, `{{gen.hex}}`, `{{gen.now_ms}}` | A fresh value per call, for idempotency keys and nonces. |
| `{{base_url}}` | The card's `base_url`. |

Placeholders take filters, such as `{{params.asin | urlencode}}` or `{{session.cookies.JSESSIONID | strip_quotes}}`.

`expect` is what a working answer looks like: the status, the content type and, for JSON, paths that must
exist. Anything else is a failure with its own result code, not silent bad data.

`extract` pulls values out of the answer. Each one has a `type`:

| Type | Reads |
|---|---|
| `jsonpath` | JSON answers, e.g. `$.included[*]`. |
| `css` | HTML pages. Each match becomes an item with its `text`, `href` and attributes. |
| `regex` | Text, with an optional `parse: json` and a follow-up (`then`) for JSON hidden inside a page. |
| `header`, `cookie` | A response header or a cookie the answer set. |

Streamed React Server Components rows are resolved into JSON first (`resolve: rows`).

## How a card is made and kept working

1. **Mapped.** flowmap (`docs/flowmap.md`) records the task once in a browser, replays it with planted
   marker values to find the request that carries the input and the results, and writes the card. The model
   only names things.
2. **Checked.** Before saving, the card is validated (`spider.card.validate_card`), every read action runs with its
   examples, and example and fixed values are checked for personal data. A write's steps up to the send are
   tried on each tier to find one that works.
3. **Saved as a version.** The registry (`src/spider/store/registry.py`) keeps every version of each site's card
   in SQLite; one is active. Older versions stay, so a bad change can be rolled back.
4. **Healed.** When an action returns `PAGE_CHANGED`, a heal job replays that action's recording, re-maps
   just that action and saves a new version, while the agent's call waits or retries.

Cards are shared: once one user's agent sets a site up, every user can use it, each with their own sign-in.

## Writing or editing a card by hand

Cards can be written by hand; some LinkedIn actions here were. Keep to `CARD_SPEC.md`, then:

1. Run `uv run python -m spider.card.check FILE`.
2. Load it with `--load` (or `Registry.save`); saving validates it and refuses an invalid card.
3. Call each action once through the gateway, or run its examples, before relying on it.

Never put a real person's ids, cookies or tokens in a card. Values that differ per user belong in `params`
or come from the user's session.
