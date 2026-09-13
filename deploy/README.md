# RevenueOS — production runbook

Factual operations reference for running RevenueOS with Docker Compose in
production. For Fly.io and Railway, see `deploy/fly.toml` and
`deploy/railway.md` — both have real caveats (shared-state limitations)
called out inline; read them before using either.

## 1. Required secrets

| Variable | Used by | What happens without it |
|---|---|---|
| `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` | panel, orchestrator | LLM-backed paths (relevance gating, personalised copy, skill execution) are skipped; every worker still runs its deterministic path. `revenueos doctor` reports the credential missing. |
| `SMTP_PASSWORD` | orchestrator (outreach worker), panel (manual execute) | Outreach can't send; `REVENUEOS_DRY_RUN=1` writes drafts to `data/outputs/` instead as a workaround. |
| `IMAP_PASSWORD` | orchestrator (inbox worker) | Reply/bounce/STOP detection against the live mailbox doesn't run; `.eml` drops in `data/exports/inbox/` still work. |
| `REVENUEOS_PANEL_PASSWORD` | panel | **Required to bind anything but localhost.** `src/revenueos/panel.py` refuses a non-loopback bind without it and the process exits. Everything except `/health`, `/site/`, and `/billing/*` sits behind the session cookie this password creates via `POST /login`. |
| `REVENUEOS_WORKER_TOKEN` | orchestrator | Without it, the orchestrator's status API on :8790 (`/health`, `/runs`, `/activity`) is open to anyone who can reach the port. With it, every route on :8790 — `/health` included — requires `Authorization: Bearer <token>`; a plain unauthenticated healthcheck against :8790 will get 401, not 200. |
| `REVENUEOS_LICENSE_SIGNING_KEY` (or `REVENUEOS_LICENSE_SIGNING_KEY_FILE`, a path to a file holding it) | panel (billing/licensing) | The vendor's Ed25519 private seed, base64. Only needed on the machine that *issues* licences (`revenueos license issue`, the Stripe webhook); without it `/billing/webhook` answers "billing is not configured on this server". **Verifying** a licence needs nothing: a customer install checks the key against the public keys shipped in `billing.LICENSE_PUBLIC_KEYS`. Continuous operation (`revenueos orchestrator`'s tick loop, not `--once`) needs an installed Pro-or-higher licence; on Community tier the orchestrator container starts and exits immediately with `This feature needs RevenueOS Pro ($99/mo) or higher...` — that is expected product behaviour, not a crash. |
| `REVENUEOS_LICENSE_SECRET` | panel (licensing) | Retired: only needed to verify licence keys issued under the old HMAC scheme. New keys never use it. |
| `STRIPE_SECRET_KEY` | panel (`/billing/checkout`, `/billing/webhook`) | Checkout returns "STRIPE_SECRET_KEY is not set" instead of a Stripe URL. |
| `STRIPE_WEBHOOK_SECRET` | panel (`/billing/webhook`) | Stripe webhook signature verification fails closed; licence issuance on payment won't fire. |

Two more Stripe variables are needed for checkout to actually work, not listed
above because they weren't in the original ask but are load-bearing:
`STRIPE_PRICE_PRO`, `STRIPE_PRICE_BUSINESS`, `STRIPE_PRICE_AGENCY` — the
Stripe Price IDs for each paid tier. Checkout for a tier fails with
`STRIPE_PRICE_<TIER> is not set` if its price isn't configured.

Put secrets in a `.env` file next to `docker-compose.yml` (not committed —
already covered by `.gitignore`'s `.env*` pattern) or export them in the
shell that runs `docker compose`. `docker-compose.yml` reads all of the
above with `${VAR:-}` defaults, so an unset secret degrades the
corresponding feature rather than failing the whole stack — except
`REVENUEOS_PANEL_PASSWORD`, which the panel process itself refuses to run
without once it's asked to bind non-loopback (see the table above).

## 2. First bring-up

```bash
git clone <this repo> revenueos && cd revenueos
scripts/fetch-upstream.sh
python3 scripts/vendor.py

# In containers revenueos.yaml lives on the ./data volume (REVENUEOS_CONFIG_IN_DATA=1
# is set in docker-compose.yml), so nothing needs to exist before the first `up`;
# onboarding through the panel's /onboard form (or `docker compose exec panel revenueos init`)
# writes data/revenueos.yaml.

cp deploy/.env.example .env   # if you keep one; otherwise export the secrets from §1 in your shell
echo "REVENUEOS_PANEL_PASSWORD=$(openssl rand -hex 24)" >> .env
echo "REVENUEOS_WORKER_TOKEN=$(openssl rand -hex 24)" >> .env
# ... plus ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN, SMTP_PASSWORD, etc.

docker compose build
docker compose run --rm panel revenueos init --answers company-answers.json   # or omit --answers for the interactive prompt
docker compose up -d panel orchestrator
```

`orchestrator` will start and exit (nonzero) if the install has no Pro (or
higher) licence — that's the licence gate in §1, not a packaging failure.
Community-tier installs run workers on demand instead:

```bash
docker compose run --rm panel revenueos run all
```

Bring the panel up behind TLS with Caddy once you have a real domain:

```bash
echo "REVENUEOS_DOMAIN=revenueos.example.com" >> .env
docker compose --profile proxy up -d panel caddy
```

Caddy reaches the panel over the compose network (`panel:8791`); the
panel's own `127.0.0.1:8791` port publish stays loopback-only, so Caddy is
the only public ingress. `deploy/Caddyfile` is intentionally minimal — it
just terminates TLS (Caddy auto-provisions the certificate via ACME for a
real domain) and reverse-proxies; it does not add auth or rate limiting,
so keep `REVENUEOS_PANEL_PASSWORD` long and random.

Verify with the smoke script:

```bash
deploy/smoke.sh https://revenueos.example.com "$REVENUEOS_PANEL_PASSWORD"
```

## 3. What to persist

Everything RevenueOS writes lives under four host paths, all bind-mounted
in `docker-compose.yml`:

| Path | Contents |
|---|---|
| `./data` | `revenueos.db` (SQLite: actions, outcomes, pipeline), `automations.json` (the cron schedule — baked into the image too, but the volume copy is what the running orchestrator actually reads and can be hot-edited), `license.json` / `licenses.jsonl`, `exports/`, `outputs/`, `reports/` |
| `./logs` | `runs.jsonl` — the orchestrator's run journal (`orchestrator/src/storage/jsonlStore.js`) |
| `./company-context` | the markster-os 12-file business canon (`revenueos init` / panel `/onboard` writes here) |
| `./learning-loop` | `CORRECTIONS.md` and related — corrections fed into every worker prompt for 30 days |
| `./data/revenueos.yaml` (written by onboarding) | website, competitors, channels, sender, SMTP/IMAP config, `ads.data_lifecycle` |

None of these are baked into the image except `data/automations.json`,
which ships as a fallback default; the volume-mounted copy always wins at
runtime once the volume exists.

## 4. Backup

Stop writers before copying the SQLite file (or use `sqlite3 .backup`,
which is safe to run against a live database and is preferable to a raw
copy):

```bash
docker compose stop panel orchestrator
tar czf "revenueos-backup-$(date +%Y%m%d).tar.gz" \
  data logs company-context learning-loop
docker compose start panel orchestrator
```

or, without stopping anything:

```bash
docker compose exec panel sh -c \
  "python3 -c \"import sqlite3; sqlite3.connect('/app/data/revenueos.db').backup(sqlite3.connect('/app/data/revenueos.db.backup'))\""
docker cp revenueos-panel-1:/app/data/revenueos.db.backup ./revenueos.db.backup
tar czf "revenueos-backup-$(date +%Y%m%d).tar.gz" \
  data logs company-context learning-loop revenueos.db.backup
```

Restore by extracting the tarball over a stopped stack's `./data`,
`./logs`, `./company-context`, `./learning-loop`, `./revenueos.yaml`, then
`docker compose up -d`.

## 5. Upgrade

```bash
git pull
scripts/fetch-upstream.sh    # only if upstream/MANIFEST.tsv changed
python3 scripts/vendor.py    # re-assembles vendored code + skills; idempotent
uv sync --extra dev          # if you also run the CLI outside Docker
docker compose build
docker compose up -d panel orchestrator
deploy/smoke.sh https://revenueos.example.com "$REVENUEOS_PANEL_PASSWORD"
```

Back up first (§4) — schema or vendored-skill changes aren't guaranteed
backward compatible.

## 6. Rollback

```bash
docker compose down
git log --oneline -5                 # find the last known-good commit/tag
git checkout <previous-tag-or-sha>
python3 scripts/vendor.py            # re-assemble the tree at that commit
docker compose build
# restore ./data, ./logs, ./company-context, ./learning-loop, ./revenueos.yaml
# from the backup taken before the upgrade (§4) if the schema moved forward
docker compose up -d panel orchestrator
deploy/smoke.sh https://revenueos.example.com "$REVENUEOS_PANEL_PASSWORD"
git checkout main   # once you've decided the rollback is done, or forward-fix and re-upgrade
```

There's no automatic migration/rollback tooling — `data/revenueos.db`'s
schema is whatever the running code's `store.py` created. Rolling back
past a schema change means restoring the matching data backup, not just
the code.

## 7. Health and smoke checks

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET panel:8791/health` | none (deliberately, for load balancers) | `{"ok": true, "service": "revenueos-panel", "version": ..., "onboarded": ...}` |
| `GET panel:8791/api/today` | session cookie (`REVENUEOS_PANEL_PASSWORD` via `/login`) once the panel is bound non-loopback | the brief: counts, pipeline value, pending actions |
| `GET orchestrator:8790/health` | `Authorization: Bearer $REVENUEOS_WORKER_TOKEN` if that's set, else none | service, uptime, schedule, current run |

`deploy/smoke.sh <base-url> [panel-password]` checks the first two and
exits non-zero on any real failure (unreachable, 5xx, malformed body). Run
it without a password and a 401 on `/api/today` is logged as a warning,
not a failure — it proves the panel is up and correctly enforcing auth
rather than broken; pass the password to check the authenticated path for
real. The Dockerfile's own `HEALTHCHECK` curls `panel:8791/health` — that's
what `docker compose`'s `service_healthy` condition (used by the `caddy`
service) is watching.

## 8. Known limitations of this packaging

- **Fly.io / Railway split storage.** Both platforms attach volumes per
  machine, not shared across machines. Running `panel` and `worker` as
  separate process groups/services (as `deploy/fly.toml` and
  `deploy/railway.md` literally describe) means they do **not** share
  `/app/data` — the SQLite action store and run journal diverge. Docker
  Compose (this file) is the only topology here where panel and
  orchestrator genuinely share state, because both mount the same host
  `./data` directory. Read the caveat comments at the top of
  `deploy/fly.toml` and in `deploy/railway.md` before using either for
  anything beyond a single-process or demo deployment.
- **Single workspace.** RevenueOS's stores are single-workspace (per
  `CLAUDE.md`) — this packaging runs one company's instance per deployment,
  not a multi-tenant SaaS.
- **No auth on `/login` beyond the password itself** — no rate limiting,
  no lockout. Use a long random `REVENUEOS_PANEL_PASSWORD` and put Caddy
  (or another TLS-terminating proxy) in front rather than exposing the
  panel port directly.
