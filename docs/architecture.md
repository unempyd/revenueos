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

## Roles and the learning loop

Some questions are not a worker's job — "what does this market actually pay", "what is the one
thing stopping revenue here", "did last month's work change anything". Those go to a **role**:
a small specialist (`research`, `marketing`, `sales`, `measurement`) whose written spec lives in
`learning-loop/roles/<role>.md` — its purpose, its inputs, the JSON shape it must answer in, and
hard rules it may not break: evidence behind every claim (a URL, an action id, a metric name, or
the words "not observed"), no invented numbers, and "cannot determine" when that is the truth.
`revenueos agent run <role> "<task>"` runs one. A role may split its task across other roles;
those run in parallel, two levels deep at most, and their answers hang off the parent's. Every
attempt — the failures included — is one row in `agent_runs`, with what was asked, what came
back, and which parent asked for it. A role that answers outside its contract is recorded as a
failure with the raw text kept; nothing is fabricated to fill the gap. Roles never act: what
they propose arrives in TODAY as ordinary pending actions, for the same human Approve/Execute.

What the system learns is written down in the same directory, in files a person can read:

- `learning-loop/CORRECTIONS.md` — standing corrections from the operator (`revenueos correct`).
- `learning-loop/LESSONS.md` — one dated block per measured outcome (`revenueos learn`). What
  was attempted, what the evidence showed, before → after: those lines are read off the store,
  never written by a model. Only the "why" and the "lesson" are analysis, and with no model
  configured they say so.

Both are injected into every prompt (corrections for 30 days, lessons for 60), so the next run
starts where the last one ended. When the evidence says a role's spec itself is wrong,
`revenueos refine <role> --evidence "..." --change "..."` edits it — a dated line, or one
rewritten section, never more than ~20 changed lines, and never the spec's `## Purpose` section
with its hard rules. Each accepted refinement snapshots the spec first and logs the evidence in
`learning-loop/REFINEMENTS.md`; `revenueos refine <role> --rollback` puts the old one back.

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

## Objectives, heartbeat and messages

So that a customer sees one system continuously working on their business rather than a
collection of scheduled jobs, three small pieces sit on top of the loop — all of them
stored in the same SQLite file and scheduled by the same orchestrator. There is no
separate daemon and no second product layer.

- **Objectives** (`objectives`, `objective_events`; module `src/revenueos/objectives.py`) —
  one row for what the business is trying to achieve, with a status
  (active/paused/done), a human-owned `strategy` and a machine-written `next_action`.
  `revenueos init` creates it from the questionnaire's objective answer; `revenueos
  objective add | list | show | set | note | pause | resume | done` maintains it by hand.
  Everything observed, done, measured or failed is appended to it as an event — kinds
  `evidence`, `action`, `result`, `failure`, `lesson`, `next` and `heartbeat` — so the
  trail behind a goal is readable without opening a log file.
- **Heartbeat** (`src/revenueos/workers/heartbeat.py`) — an ordinary worker, scheduled by
  the orchestrator every 30 minutes (`data/automations.json`, entry `heartbeat`). It sends,
  publishes and spends nothing, and works with no model available. Each run it reads the
  state the other workers wrote — pending opportunities by type, approvals waiting and
  which of them have been waiting more than 24 hours, outcomes measured vs. still awaiting
  measurement, the leads funnel, failed runs classified as *model unavailable* / *network*
  / *worker failure*, and sends that are externally blocked (no mailbox) — decides the
  single next action from `today.rank_next` plus the objective's own plan, appends exactly
  one `heartbeat` event per run to every active objective, adds one `result` event per
  newly measured outcome (deduplicated by action id) and one `failure` event per distinct
  worker and error class per day, and updates `objectives.next_action`.
- **Messages** (`messages`) — the heartbeat posts a note to the operator only when a human
  is actually needed: approvals are waiting, a send is blocked, or a failure class was seen
  for the first time today. An identical note is not posted again while the previous one is
  unread. `revenueos messages [--unread] [--mark-read]` reads them; the panel shows them on
  TODAY as "Inbox from RevenueOS" with a mark-read button, and `/api/today` carries them.

TODAY (CLI and panel) leads with the objective, its next action and the last heartbeat, so
the answer to "what is RevenueOS doing for me?" is the first thing on the page.

## Convert

The chain ends "prove the result → convert the value into revenue", and `src/revenueos/convert.py`
is that last link. `offer_state` names where a workspace stands on the pay-on-result clock that
`billing.pay_on_result` already owns — `none` (nothing measured, nothing to charge for),
`clock_running` (first measured result on a date, N free days left), `due` (the 14 days are up),
`licensed` — and carries the Pro price and the payment link from `billing.payment_link` in
`revenueos.yaml` or `REVENUEOS_PAYMENT_LINK_PRO`, or `None` when the install was never given one
(a link is never invented). The heartbeat calls the convert step on every run: `ensure_offer_action`
drafts exactly ONE pending `follow_up`, "Offer Pro to `<company>`", whose body quotes the measured
before → after that is in the store, the date, the free days remaining, the price, the link when
there is one and `revenueos license install <key>` — deduped on `offer:pro:<first result date>`, with
`executor: send_email` only when an address is on file, and never sent by anything but a human's
Approve → Execute. `offer: {state, days_left, action_id}` lands in the heartbeat's details, `· offer:
<state>` in its summary, and one operator message when the draft is created. TODAY, the panel and
`/api/today` show one line while the state is `clock_running` or `due`, and nothing otherwise. In
hosted mode (`accounts.json`) `offers_for_tenants` computes each tenant's state from that tenant's
own store and drafts the offer in the HOST workspace with the tenant's account email, so the host
operator sends it and no tenant ever sees another tenant's data. The reverse direction closes
without a webhook: the `billing` worker reads the connected Stripe account's active subscriptions
(`connections/stripe_conn.paying_customers`), records the `paying_customers` metric, writes
`data/exports/stripe-customers.json`, and for every paying email with no row in
`data/licenses.jsonl` mints a 35-day Pro key (when the Ed25519 signing key is configured; otherwise it
says so and issues nothing) and drafts a "Deliver Pro licence to `<email>`" action. `measure_offer`
closes the loop honestly: an executed offer becomes `subscribed 0 → 1` only once that same address
appears among the paying customers, and stays `pending` until it does.

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
- **objectives → objective_events, messages** — what the business is working towards, the
  trail behind it, and the notes RevenueOS leaves for a human. See *Objectives, heartbeat
  and messages* above.

See `docs/security-and-approval.md` for how the suppression list and send cap are enforced,
and `docs/proof.md` for a real run through this exact model.
