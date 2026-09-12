# Architecture

Connect your business. RevenueOS finds opportunities, executes approved revenue work, and
measures what happened.

## The loop

```
Connect → Discover → Approve → Execute → Measure
```

- **Connect** — a questionnaire (`revenueos init`, or the panel's `/onboard` form) writes
  your business into a workspace: who you are, who you sell to, what you offer, how you
  sound, and which channels you run.
- **Discover** — the workers read that workspace and your connected surfaces (website, ad
  exports, inbox, the open web) and turn every finding into one **action**: a prospect, a
  follow-up, an SEO fix, wasted ad spend, a content gap, a market signal worth joining.
- **Approve** — a human reads the actions and decides: approve, execute, or ignore. Workers
  never send, publish, or spend on their own — only an approved Execute does.
- **Execute** — the approved action runs: the email sends, the skill runs and writes a
  deliverable, the fix goes out.
- **Measure** — RevenueOS checks what actually happened — a reply, a re-crawl, the next ad
  export, a written file — and records it as an outcome. Nothing is guessed.

```
                        ┌────────────────────────────────────────────┐
                        │                 WORKSPACE                  │
                        │  company-context/  (the business canon)    │
                        │  revenueos.yaml     (connections, config)  │
                        │  data/revenueos.db  (state)                │
                        └────────────────────────────────────────────┘
                                          │
   CONNECT                                │
   revenueos init / panel /onboard  ─────►│
                                          ▼
                              ┌────────────────────┐
                              │      DISCOVER       │
                              │ discover  outreach  │
                              │ inbox     seo       │
                              │ ads-audit content   │
                              │ monitor             │
                              └─────────┬───────────┘
                                        │ writes actions
                                        ▼
                              ┌────────────────────┐
                     ignore ◄─┤       APPROVE        │
                              │  today / panel / MCP │
                              └─────────┬───────────┘
                                        │ approved
                                        ▼
                              ┌────────────────────┐
                              │       EXECUTE       │
                              │ send_email          │
                              │ run_skill            │
                              └─────────┬───────────┘
                                        │
                                        ▼
                              ┌────────────────────┐
                              │       MEASURE       │
                              │  re-crawl · replies  │
                              │  next export · file   │
                              └─────────┬───────────┘
                                        │ writes outcomes
                                        └───────────► results / RESULTS / /api/today
```

## The workspace model

One RevenueOS install is one workspace. Everything it knows about your business lives in
three places:

1. **The company-context canon** — a fixed set of files under `company-context/`
   (identity, audience, offer, messaging, voice, channels, themes, truth rules, and a
   quality checklist, plus a manifest) with headings a validator enforces. `revenueos init`
   and the panel's onboarding form write into these headings; nothing else may add files
   here. Workers read the canon through a single accessor that assembles only the sections
   a given task needs — never the whole canon — so prompts stay small and on-topic.
2. **`revenueos.yaml`** — machine configuration: website, competitors, channels, sender
   identity, SMTP/IMAP hosts, and the ad-data lifecycle settings that gate ad reporting on
   real data. Credentials never live here — only hostnames and non-secret settings; the
   corresponding passwords and API keys come from the environment (see
   `docs/security-and-approval.md`).
3. **SQLite state** (`data/revenueos.db`) — one file, no server. It holds the actions queue,
   every recorded outcome, the lead/draft/send pipeline, the suppression list, and a run
   journal. `revenueos workspace new <dir>` creates an independent workspace — its own
   canon, config, and database — that still shares one install's skill and connector
   catalogue.

A fourth file, `learning-loop/CORRECTIONS.md`, holds standing corrections an operator has
recorded (`revenueos correct`); the last 30 days of corrections are injected into every
worker's prompt automatically.

## Workers

Each worker is a small program: it reads the workspace, does one job, and writes actions.
None of them send, publish, or spend — that only happens in Execute, and only for an
approved action.

| Worker | Finds | Executes | Measures |
|---|---|---|---|
| `discover` | Qualified prospects, from an optional external lead-generation service across a process boundary, or from CSV drops | — | Feeds `outreach`'s pipeline |
| `outreach` | First-touch email drafts built from the business canon (deterministic hook + subject; an LLM personalises the opening line when one is available) | Sends the approved draft — subject to the daily cap and suppression list | Replies, bounces, and booked/pipeline value from the send record |
| `inbox` | Replies, bounces, and STOP requests on the sending mailbox (or dropped `.eml` files) | — | Feeds `outreach`'s reply/bounce/booked outcomes |
| `seo` | Crawl defects (missing or duplicate titles/descriptions, thin pages, no sitemap), a domain-authority gap against competitors, and — when a local site source is configured — concrete content/template surfaces | Runs the matching SEO skill on Execute | Re-crawls the page: fixed or not |
| `ads-audit` | Wasted spend, over-pacing, and dangerous spend concentration in dropped ad exports | Runs the matching ads skill on Execute | The delta in the next export |
| `content` | Skills from the capability packs that match the business's channels | Claude runs the skill and writes the deliverable | Whether the deliverable was written |
| `monitor` | Public conversations (from a Hacker News search) that pass a strict, default-reject relevance gate against the business | — | — (a market signal is a prompt to act elsewhere, not itself measured) |
| `measure` | — | — | Runs after execution and records one outcome per executed action, honestly: `pending` until evidence exists, `measured` or `no_effect` once it does, `unmeasurable` with a note on what would measure it |

## The approval surface

Three ways to see what RevenueOS found and decide what happens to it — all backed by the
same actions table and the same rule (workers propose, only Execute acts):

- **CLI** — `revenueos today`, `approve <id>`, `execute <id>`, `ignore <id>`, `results`.
  Scriptable, `--json` everywhere the orchestrator needs it.
- **Panel** — a small, dependency-free control panel (`revenueos serve`) with the same
  brief, the same three buttons (Approve / Execute / Ignore), and a RESULTS view. It also
  hosts onboarding and health/billing routes.
- **MCP** — an MCP server (`revenueos-mcp`) exposes the same loop as tools — `revenueos_today`,
  `revenueos_run`, `revenueos_approve`, `revenueos_execute`, `revenueos_ignore`,
  `revenueos_results`, plus worker-specific tools for SEO, ads, lead discovery, and skill
  search — so an MCP host such as Claude Code or Claude Desktop can drive one workspace
  directly. `revenueos_execute` is the only tool in that surface that sends, publishes, or
  writes anything, and only for an already-approved action.

## The orchestrator

Continuous operation is a small scheduler (Node/TypeScript) that turns a list of named
automations (`data/automations.json`, each naming a worker and a cron schedule) into runs:
it spawns `revenueos run <worker> --json` as a child process on schedule, one run at a
time (serial, not concurrent), reads the last line of stdout as the result, and journals it.
A failure classified as transient (rate limits, network blips, 5xx, timeouts) gets exactly
one retry; anything else fails without retrying blindly. A small status API reports
service health, the schedule with next-run times, the run journal, and the per-action
activity log; it is open by default and can be placed behind a bearer token. Continuous
operation is the one capability gated by tier — see `docs/community-vs-hosted.md`.

## Capability packs

RevenueOS ships 13 capability packs — ads, agency, cmo, core, creative, growth,
growth-lab, gtm, office, operations, pipelines, playbooks, and seo — totalling 790 skills
(87 of which ship runnable code alongside their instructions), 104 agent definitions, and
64 connector CLIs. A skill is a written procedure for the LLM to follow (grounded in your
business canon and labelled for what is known vs. estimated); an agent definition packages
a skill for a specific role; a connector CLI is a small, dependency-free script that talks
to one external platform once you supply its API key. `revenueos skills index` rebuilds the
catalogue and reports the counts above; `revenueos skills search <query>` and
`revenueos tools` (which shows which connectors have a credential configured) are how you
explore it. See `docs/integrations.md` for what the connectors actually reach.

## Data model

Everything the loop produces is one SQLite file with a small set of tables:

- **actions → outcomes** — every worker finding is one row in `actions` (typed, deduplicated,
  with a status of pending/approved/executed/ignored/failed); every measured result is one
  row in `outcomes` linked back to the action it measures (status pending/measured/
  no_effect/unmeasurable, with a named metric and a before/after value where one exists).
- **leads → drafts → sends → unsubscribes** — the outreach pipeline: a `leads` row per
  prospect, an `email_drafts` row per generated draft with its own approval state, an
  `email_sends` row per message actually sent (immutable once written), and an
  `unsubscribes` table that both the sender (List-Unsubscribe / reply-STOP) and the inbox
  worker write to — checked before every send.
- **runs** — one row per worker invocation (worker name, workflow/automation name, status,
  summary), the record the orchestrator and `revenueos doctor` read.

See `docs/security-and-approval.md` for how the suppression list and send cap are enforced,
and `docs/proof.md` for a real run through this exact model.
