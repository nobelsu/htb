# spider: context for the pitch deck

Everything a separate session needs to build the Hack the Burgh 2026 pitch deck. Facts here come from the
code, `DECISIONS.md`, `CARD_SPEC.md` and the live gateway as of 2026-11-08 (numbers updated at the end of the hackathon). Where something isn't proven yet,
it says so: the deck must not claim more than this.

## One line

spider turns any website into actions an AI agent can call directly, without driving a browser.

## The problem

- Agents use websites the slow way: a browser agent looks at screenshots or the page, clicks, types, waits,
  and does it again on every call. A LinkedIn search takes tens of seconds and many model turns, breaks when
  the layout shifts, and costs tokens every time.
- Most sites people care about have no public API, or one that doesn't cover what users do (LinkedIn,
  Amazon, job boards, internal tools).
- Every site's own pages already call the site's own internal endpoints. Those calls are fast and
  structured; nobody hands them to agents.

## What spider does

1. **Maps a site once.** An agent (Claude) does a task in a real browser once, e.g. "search companies for
   robotics". spider replays it with planted marker values, watches which of the site's own requests carry
   the input and the results, and writes that down as an **action**: the request to send, where the inputs
   go, where the results are in the answer.
2. **Saves it as a card.** One JSON file per site (`CARD_SPEC.md`, v0.4), shared by every user. Each action
   has typed inputs, a description, safety flags, and examples used to check it still works.
3. **Runs it for any agent.** Agents connect to spider as an MCP server (works in Claude web, desktop,
   phone, Claude Code). They call `find_actions` ("search companies on linkedin.com"), then `run_action`
   with inputs, and get clean JSON back in about a second, using the user's own sign-in.
4. **Heals itself.** When a site changes and an action breaks (`PAGE_CHANGED`), spider replays the saved
   recording, re-maps just that action and saves a new version, while the user's call waits or retries.
5. **Sets up new sites on request.** If an agent finds nothing for a site, it calls `request_map`: spider
   maps it on the server (one named action in a few minutes, or explores the whole site: a crawl that
   proposes the site's main reads and follows links from the results to deeper pages).

## How it works (for an architecture slide)

```
agent (Claude) ──MCP──▶ spider gateway ──▶ executor ──▶ the site's own HTTP endpoints
                           │                  │
                           │                  └─ user's sign-in (encrypted), their browser identity,
                           │                     residential proxy, fallback tiers (plain HTTP,
                           │                     browser fingerprint, a real Chrome tab)
                           │
                           ├─ registry: cards, versioned per site (SQLite)
                           ├─ map jobs: flowmap records with Claude, replays, infers, checks, saves
                           └─ heal jobs: broken action → replay recording → re-map → new version
```

- **Mapping is evidence-based, not guessed.** The LLM only does the task and names things. Which request
  is the action is decided from data: marker values planted in replays, values shown on the page matched
  against responses, and a blocking test (replay with one request blocked; if the results vanish, that
  request is the source).
- **Three kinds of site answer handled:** JSON APIs; pages rendered on the server (read with a CSS
  selector); streamed React Server Components rows (resolved into JSON). Browser replay for sites that
  build everything in scripts is not built.
- **Sign-in without sharing passwords with the agent:** the user signs in to the site in a private remote
  browser (Neko, streamed to their screen); spider keeps the session cookies encrypted and checks they're
  still valid. The agent never sees a password or cookie.
- **Privacy in shared cards:** before a card is saved, example values and fixed request values are
  checked (rules plus Claude) for personal data; the requester's own ids become inputs with a public
  example, or the action isn't saved.
- **Writes are safe by design:** while mapping a write (follow, send, add to cart), the committing request
  is held back in the browser so the site never gets it. The saved action always needs the user's
  confirmation, and its first call says it has never been sent for real.

## What's live (numbers to use)

- Gateway running behind a public URL, connected as a custom connector in regular Claude.
- **linkedin.com:** 20 actions live (searches, profiles, company pages, jobs, posts, follow a company,
  connect, start a conversation), most about 1 second each; signed-in pages use the browser tier.
- **amazon.co.uk:** search_products (about 20 results in about a second) and get_product_details.
- **fixture.test** (our own demo shop): 21 actions covering every runtime case (reads, writes needing
  confirmation, rate limits, bot challenges, pages that change, browser-only endpoints).
- 25 map jobs run on the server today (2026-11-08) against LinkedIn and Amazon; 5 succeeded. Most failures
  were real-site problems that each led to a fix (see "Honest limits").
- About 30,000 lines of Python; 661 automated tests; 135 commits (Nobel and Wei Wei).
- Model used for mapping and labelling: Claude (Anthropic API).

## Demo ideas

1. In Claude (phone or desktop) with the spider connector: "find robotics startups in London on LinkedIn
   and the people hiring there". Claude calls `find_actions`, then `search_companies` and `search_people`,
   answers in seconds, no browser.
2. Side by side: the same task with a browser agent (slow, many screenshots) vs spider (one call).
3. A site with nothing yet: Claude calls `request_map`, the account page shows the job's progress, then the
   new action works.
4. Healing: break the demo shop's endpoint (`POST /_admin/break`), call the action, watch it re-map and
   carry on.

## Who it's for / why now

- Anyone building or using AI agents that need websites without APIs: assistants, recruiters, research,
  sales, ops.
- Agents are moving from chat to doing things; browser agents are the bottleneck (speed, cost,
  reliability). MCP gives a standard way to plug tools into every agent.
- Shared cards: once one user maps a site, every user gets it instantly; healing keeps it working.

## Related work the user mentioned

- AgentWeb (pulls off something similar; we compared approaches). Browser agents (computer use, browser
  use) are the main alternative.

## Honest limits (don't claim these)

- Sites that render everything in scripts with no readable request (level 3, browser replay) aren't
  supported.
- Writes on real sites are early: the safety path is built and tested on our demo shop; on LinkedIn,
  follow_company is saved and connect has worked for real. On 2026-11-08 a replay bug let one LinkedIn follow through (a
  recording clicked Follow twice and the replay only held the last click back); it's fixed and tested.
- Amazon product pages give only the title so far; actions are set up from one real example.
- Hosting is a laptop behind ngrok; no always-on server yet. No per-user quotas.

## Team and ownership

- **Nobel Suhendra:** runtime (executor, MCP gateway, sign-in, healing, map jobs) and flowmap, the mapper
  the product runs.
- **Wei Wei (pig8pig):** the original mapper (`mapper/`): capture pipeline and an explorer; his streamed-rows
  support is in the product.

## Design profile (use for the deck's look)

From the product's own pages (`runtime/ui.py`) and Nobel's design rules.

- **Name:** spider, always lowercase, as a wordmark. Theme: web crawling, spiders, orb webs. ("Silk Road"
  was rejected.)
- **Palette, Linen (light):**
  - paper `#F5EDE2` (background)
  - ink `#2A2421` (text)
  - muted `#6A5E55` (secondary text)
  - umber `#6B5A35` (actions, accents)
  - brick `#9C3F33` (errors, emphasis)
  - line `#E0D3C1`, tint `#EDE2D2` (hairlines, panels)
- **Inverted (dark slides):** paper `#2A2421`, ink `#F5EDE2`, muted `#BBAE9F`, action `#D9C08A`,
  error `#E8A08C`, line `#4A403A`, tint `#352D29`.
- **Type:** IBM Plex Sans (400, 500) for everything, small and calm (pages use 14px); IBM Plex Mono for
  code, ids and request lines.
- **Layout, "Margin":** a hairline divides a left art panel from a calm content column. The art is a
  generative spider spinning orb webs endlessly; webs persist and fade; motion slightly random. Use the
  fixed orb pattern, not a tangle.
- **Rules:**
  - Minimal copy: short headlines, almost no helper text, no eyebrow labels, step counters or subtitles.
  - No emojis. Simple stroke icons only. No symbols in text (no arrows, checkmarks, bullets as glyphs).
  - Colour as a palette (more than one base colour), not one accent.
  - Not a standard template layout: mock up first, then build.
  - Rejected directions: "Spine" (black and white, giant type), "one blue accent, Arial" (too vanilla).

## Where to look for more

- `README.md` (setup, pages, users and keys), `CARD_SPEC.md` (the card format), `DECISIONS.md` (every
  judgement call: L-series runtime and sign-in, M-series mapper), `docs/flowmap.md` (the mapper),
  `docs/mapper-design.md` (Wei Wei's mapper), `action_map.example.json` (an example card).
