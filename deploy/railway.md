# Railway

RevenueOS's orchestrator (`orchestrator/`) is a direct port of Kairos' Railway
worker (see `upstream/kevinbadi__kairos/Dockerfile.worker`'s header — Railway
is the platform that vendored design targeted). This directory ships two
Railway service configs because RevenueOS needs two long-running processes
that don't share a start command: the orchestrator (`revenueos orchestrator`)
and the control panel (`revenueos serve`).

## Services

Railway does not run a `docker-compose.yml` for you — each process is its
own **service** inside one Railway project, pointed at the same repo/image,
each with its own config file and its own volume:

| Service | Config file | Start command | Public |
|---|---|---|---|
| `revenueos-worker`  | `deploy/railway.json`       | `revenueos orchestrator` | No |
| `revenueos-panel`   | `deploy/railway.panel.json` | `revenueos serve --host 0.0.0.0 --port $PORT` | Yes |

Setup, per service:

1. New Service → Deploy from GitHub repo (this repo) → same repo for both.
2. Service Settings → Build → **Config-as-code path**: set it to
   `deploy/railway.json` for the worker service, `deploy/railway.panel.json`
   for the panel service. (Railway reads `railway.json` at the repo root by
   default; pointing each service at its own file under `deploy/` is what
   makes one repo drive two independently-configured services.)
3. Variables (see the table below) — set per service; a Railway **Shared
   Variable** at the project level is fine for the ones both need
   (`ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN`, `TZ`, `IS_SANDBOX`).
4. Volumes → Attach a volume to `/app/data` on **both** services.
   Railway volumes, like Fly's, are per-service — the worker and panel will
   each get their own copy of `/app/data`, so they will not share the
   SQLite action store or run journal. This is the same single-workspace
   limitation described in `deploy/fly.toml` and `deploy/README.md`; if
   you need panel approvals and worker-created actions to be the same
   database, run them as one Railway service (one start command that
   launches both, e.g. `revenueos serve & revenueos orchestrator`, sharing
   the one volume) instead of two.
5. Networking → generate a public domain for the panel service only. Leave
   the worker service private (internal Railway networking, no public
   domain) — its status API (port 8790) is protected by
   `REVENUEOS_WORKER_TOKEN`, and Railway's built-in HTTP healthcheck cannot
   send an `Authorization` header, so `deploy/railway.json` deliberately
   does not set `healthcheckPath`. Railway still restarts the worker on
   crash via `restartPolicyType: ON_FAILURE`; check `/health` yourself with
   `curl -H "Authorization: Bearer $REVENUEOS_WORKER_TOKEN" https://<worker-internal-host>:8790/health`
   if you need to confirm it's alive, or just watch the Railway deploy logs.
6. Set an Anthropic Console spend limit before first deploy (same warning
   the Kairos Dockerfile.worker carries).

## Environment variables

Mirrors Kairos' `Dockerfile.worker` header, renamed for RevenueOS:

| Kairos (`Dockerfile.worker`) | RevenueOS | Where |
|---|---|---|
| `KAIROS_WORKER_TOKEN` | `REVENUEOS_WORKER_TOKEN` | both services (protects the worker's :8790 status API; also gates the panel's non-loopback bind — see below) |
| `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` | same | both services |
| `TZ` | `TZ` | both services — cron hours in `data/automations.json` run in this zone |
| `IS_SANDBOX=1` | `IS_SANDBOX=1` | both services — the container runs as root; RevenueOS's headless runner (like Kairos') refuses unattended execution as root unless told this is an isolated sandbox |
| — | `REVENUEOS_ROOT=/app` | both services |
| — | `REVENUEOS_PANEL_PASSWORD` | panel only — the panel refuses to bind to a non-loopback address without it |
| — | `SMTP_PASSWORD`, `IMAP_PASSWORD` | worker (outreach/inbox workers send and read real mail) |
| — | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `REVENUEOS_LICENSE_SECRET` | panel (billing routes) |

Full secret list and how to provision each: `deploy/README.md`.

## Notes

- `Dockerfile` builds one image for both services; `startCommand` is what
  differs. There's no `Dockerfile.worker` split like Kairos' — RevenueOS's
  Dockerfile already produces a single image with both `revenueos` and the
  orchestrator's `node_modules` installed.
- The panel's `startCommand` reads Railway's injected `$PORT` rather than
  hardcoding 8791, since Railway's public domain routes to whatever port the
  app actually binds. `deploy/fly.toml` hardcodes 8791 instead, because Fly
  has you declare `internal_port` explicitly.
