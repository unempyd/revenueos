# CLAUDE.md (public)

Guidance for working in this codebase — commands, architecture, invariants, and testing
conventions. This is the contributor-facing version; see `CONTRIBUTING.md` for how to set
up a development environment and what a change needs before it merges.

## Commands

```bash
scripts/fetch-upstream.sh                 # fetch every upstream at the commit pinned in upstream/MANIFEST.tsv (upstream/*/ is gitignored)
python3 scripts/vendor.py                 # re-assemble vendored code + skills from upstream/ (idempotent; the only way to change vendored files)
uv sync --extra dev                       # Python 3.12+ env with the `revenueos` CLI
uv run pytest -q                          # Python suite; one test: uv run pytest tests/test_measure.py::test_seo_fix_is_measured_by_recrawl
uv run ruff check src tests               # lint (vendored code excluded)
cd orchestrator && npm install && npm test && npm run typecheck   # vitest + tsc; one file: npx vitest run tests/worker.test.ts
uv run revenueos workspace new <dir>      # a fresh customer workspace sharing this install's catalogue; then --root <dir> or REVENUEOS_ROOT
uv run revenueos init --answers a.json    # onboarding (keys: QUESTIONS in src/revenueos/context.py); interactive without --answers
uv run revenueos run <worker|all> [--json] [--no-llm]   # workers: discover outreach inbox seo ads-audit ads-live analytics billing content monitor measure growth heartbeat
uv run revenueos today | next | results | approve <id> | execute <id> | ignore <id> | doctor | skills search <q> | tools
uv run revenueos objective add "<title>" [--strategy ..] | list | show <id> | note <id> --kind .. "..." | pause|resume|done <id>
uv run revenueos messages [--unread] [--mark-read]       # what the heartbeat needs a human for
uv run revenueos agent run <role> "<task>" [--json]      # roles: research marketing sales measurement (specs in learning-loop/roles/)
uv run revenueos learn | lessons | refine <role> --evidence .. --change .. | refine <role> --rollback
uv run revenueos serve [--host 0.0.0.0 --port 8791]     # control panel; non-loopback needs REVENUEOS_PANEL_PASSWORD
uv run revenueos orchestrator [--once <automation>]      # the always-on worker loop over data/automations.json; continuous mode needs a Pro licence
uv run revenueos license show|install <key>|issue ...    # licence (verified offline with the shipped public key; vendor issues with REVENUEOS_LICENSE_SIGNING_KEY_FILE)
uv run revenueos billing checkout --tier pro --success-url .. --cancel-url ..   # Stripe Checkout URL (STRIPE_SECRET_KEY, STRIPE_PRICE_PRO)
docker compose up orchestrator panel; docker compose --profile outreach up      # see deploy/README.md for production
deploy/smoke.sh https://host                             # /health + /api/today
```

Environment: models are reached through an ordered provider chain that fails over on
capacity failures only (rate limit, quota, overload, timeout, connection error, 5xx — never
on a 4xx, an auth failure or a refusal). Auto-detected order: `ANTHROPIC_API_KEY` /
`ant auth login` → the Anthropic SDK (`REVENUEOS_MODEL`, `REVENUEOS_EFFORT`); a signed-in
Claude Code CLI (`claude -p`); then any OpenAI-compatible endpoint you point it at with
`REVENUEOS_LLM_BASE_URL` + `REVENUEOS_LLM_MODEL` (+ `REVENUEOS_LLM_API_KEY`).
`REVENUEOS_LLM` pins the chain (`off`, one name, or `anthropic,claude-cli`);
`REVENUEOS_LLM_RETRIES` and `REVENUEOS_LLM_BUDGET` bound the work. `revenueos doctor`
prints the chain in order; each call is recorded as an `llm_call` metric with no prompt or
response text.
`SMTP_PASSWORD`/`IMAP_PASSWORD` + `smtp.host`/`imap.host` in `revenueos.yaml` for real
mail; `REVENUEOS_DRY_RUN=1` writes emails to `data/outputs/`. `REVENUEOS_PANEL_PASSWORD`,
`REVENUEOS_WORKER_TOKEN`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
`STRIPE_PRICE_<TIER>`. Licence verification needs no key at all; issuing (vendor side)
needs `REVENUEOS_LICENSE_SIGNING_KEY` or `REVENUEOS_LICENSE_SIGNING_KEY_FILE`.

## The loop (what the product is)

connect (`init` / panel `/onboard`) → analyse (workers) → opportunity (`actions` rows) →
present (`today`, panel `/`) → approve → execute (`execute_action`: `send_email` or
`run_skill`) → measure (`measure` worker → `outcomes` rows) → record and display
(`results`, panel `/results`, `/api/today`).

Proven on a real business in `docs/PROOF.md` (content opportunity → the matching skill
runs → deliverable measured). The outreach variant (send → reply via IMAP → booked →
pipeline $) and the ads variant (waste → next export delta) are covered by tests against
real state changes and need the customer's mailbox / exports to run live.

## Architecture

RevenueOS is an assembly, not a fresh codebase. A large share of its capability is
vendored from pinned upstream sources — `VENDOR.json` records the repository, commit,
licence, and every copied path for each; `NOTICE.md` is the full, human-readable account
of what came from where and why some sources were deliberately excluded. New code is the
glue: business context, registry, the orchestration adapter, the approval surface,
measurement, billing, packaging.

**Flow.** `revenueos init` (or the panel's `/onboard` form) writes the questionnaire into
`company-context/` (a 12-file canon with fixed headings; `context.py:replace_section` edits
bodies without breaking the vendored validator) and `revenueos.yaml` (website, competitors,
channels, sender, SMTP/IMAP, `ads.data_lifecycle`). Workers (`src/revenueos/workers/`) read
`BusinessContext`, call vendored code, and write **actions** into SQLite (`store.py`;
schema lineage in its docstring; `outcomes` is RevenueOS's own addition). `today.py` renders
the brief and RESULTS; `cli.py`/`panel.py`/`mcp_server.py` are the approval surfaces.
`execute_action` dispatches on `context["executor"]`: `send_email` (outreach) or
`run_skill` (content/seo/ads — runs a SKILL.md as the system prompt with
`ctx.prompt_summary()`; output to `data/outputs/`). Every worker records a `before`
snapshot in the action context so `measure.py` can diff it. The orchestrator
(`orchestrator/`, TypeScript) spawns `revenueos run <worker> --json` on cron and parses the
last stdout line.

**Invariants.**
- Workers never send, publish or spend; only `execute_action` does, after a human
  decision. Workers are idempotent: every `create_action` passes a `dedupe_key`.
- Workers must work with `llm=None` and must not import an LLM SDK directly; `llm.py` is
  the single client. Tests force `REVENUEOS_LLM=off`.
- Outcomes are honest: `pending` until evidence exists, `measured`/`no_effect` from
  evidence, `unmeasurable` with a note saying what would measure it. Never synthesise a
  metric.
- Do not edit anything under `src/revenueos/vendor/`, `skills/`, `agents/`, `tools/`,
  `orchestrator/src/{worker/schedule,worker/automations,worker/server,storage/*,util/activityLog}.ts`,
  `company-context/` templates, `methodology/`, `playbooks/`: all regenerated by
  `scripts/vendor.py`. Change `VENDOR_MAP`/`PATCHES` in that script instead.
  RevenueOS-owned files inside vendor directories: `vendor/*/__init__.py`,
  `vendor/sales_agent/settings.py`.
- Licence gate: `vendor.py` refuses non-MIT/Apache upstreams. The one GPL source RevenueOS
  integrates with runs across a process boundary only, never in-process. Unlicensed
  sources stay reference-only and are never vendored.
- `company-context/` must keep validating (`revenueos validate`). Never add files there;
  machine config goes in `revenueos.yaml`.
- Corrections go to `learning-loop/CORRECTIONS.md`, newest first, never deleted;
  `ctx.prompt_summary()` injects the last 30 days.
- Panel: `/health` is public; everything else requires the session cookie when
  `REVENUEOS_PANEL_PASSWORD` is set; it refuses non-loopback binds without it.
  `/billing/*` is handled by `billing.billing_http`; `/site/` serves `website/`.
- Tiers: Community = manual runs; Pro = continuous operation (`orchestrator` without
  `--once`), gated by `billing.require_tier`. Licence = Ed25519-signed key in
  `data/license.json`, verified offline against the public keys shipped in
  `billing.LICENSE_PUBLIC_KEYS` — an install needs no secret to verify what it paid for
  (add your own vendor key with `REVENUEOS_LICENSE_PUBLIC_KEY`). The vendor signs with
  `REVENUEOS_LICENSE_SIGNING_KEY` / `REVENUEOS_LICENSE_SIGNING_KEY_FILE`; the Stripe webhook
  issues keys and appends `data/licenses.jsonl`.

**Where each worker's substance comes from:** see `NOTICE.md` for the specific upstream
project behind each worker's vendored core; in brief — `ads-audit` runs adapters/scoring/
reporting over `data/exports/ads-<platform>.csv` (a 13-column generic export format), with
Google's keyword and search-term downloads read as `ads-<platform>.keywords.csv` /
`ads-<platform>.search-terms.csv` beside it (`ads-live` reads the same from a connected account);
`monitor` runs a Hacker News search plus a default-reject relevance gate built from a
business "brain" derived from the canon; `seo` runs a site crawl, a domain-authority
comparison, and an optional local project probe, each finding pointing at a matching SEO
skill; `outreach` runs recipe-based drafting and a CASL-compliant renderer with an
approval state machine, daily cap, and suppression list; `inbox` runs reply-quotation
stripping over IMAP or dropped `.eml` files; `discover` reads an external lead-generation
service's JSON output (process boundary) or CSV drops; `content` matches the registry to
`ctx.channels`; `measure` re-crawls (seo), reads send rows (outreach), reads the next
export (ads), or checks deliverable presence (content).

**Objectives, heartbeat, roles, lessons.** A workspace may carry its governing document at its root (`REVENUEOS_OPERATOR_MANDATE.md` or `MANDATE.md`): the heartbeat derives the active objective from it when none exists and records one evidence event per file digest, and the orchestrator reads the same file at start-up and reports it on `/health`. `store.py` holds `objectives`, `objective_events`, `messages` and `agent_runs`. `workers/heartbeat.py` (scheduled every 30 minutes) is deterministic and works without a model: it reads what is pending, approved but not run, measured, failed and blocked, writes one dated event per run on each active objective, sets the next action from `today.rank_next`, and messages the operator only when a human is needed. `roles.py` runs a role spec (`learning-loop/roles/<role>.md`) through `llm.py` with a JSON output contract, records every attempt in `agent_runs`, recurses at most two levels, and may only propose pending actions. `learning.py` turns every measured outcome into a dated lesson in `learning-loop/LESSONS.md` (injected into prompts by `ctx.prompt_summary()`) and refines role specs with snapshots and rollback; `## Purpose` in a spec is immutable.

**Orchestrator.** `orchestrator/src/worker/index.ts` runs a tick loop; `runner.ts` spawns
the CLI (`REVENUEOS_BIN` overrides). Schedule: `data/automations.json`. The status API on
:8790 is open unless `REVENUEOS_WORKER_TOKEN` is set. A failure classified as transient
gets exactly one retry.

**Deployment.** `Dockerfile` (Python + Node in one image, HEALTHCHECK on `/health`),
`docker-compose.yml` (orchestrator, panel, optional GPL outreach-service profile),
`deploy/` (Fly.io, Railway, Caddy TLS, runbook, `smoke.sh`). In containers `revenueos.yaml`
lives on the `data/` volume (`REVENUEOS_CONFIG_IN_DATA=1`); never bind-mount the file
itself.

**Testing conventions.** `tests/conftest.py` builds a throwaway workspace (real
`company-context/` copy, symlinked `skills/`, fresh DB, `REVENUEOS_LLM=off`) and an
`onboarded` fixture with a fictional company. Network is monkeypatched at the crawl,
search, and authority-check call sites in the workers that use them. The panel is tested
over real HTTP (`tests/test_panel.py`), billing with a mock HTTP transport
(`tests/test_billing.py`), the CLI LLM provider with a fake subprocess call.

## Provenance

`NOTICE.md` and `THIRD_PARTY_LICENSES/` are the complete, authoritative record of which
upstream projects RevenueOS is assembled from, under which licence, and why a handful of
candidate sources were deliberately excluded. `VENDOR.json` maps every vendored file back
to its source repository and commit. Read those three before asking "where did this code
come from."
