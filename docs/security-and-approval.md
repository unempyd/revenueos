# Security and the approval model

RevenueOS is designed so that nothing irreversible happens without a human decision, and so
that no more data leaves your machine than a given piece of work requires.

## The approval model

Workers only look and propose. Every worker writes **actions** — prospects, follow-ups,
SEO fixes, ad-waste flags, content opportunities, market signals — and none of them sends
an email, publishes anything, or spends money. Only `execute`, called on an action that is
already `approved`, does that, and it is the same single choke point whether you triggered
it from the CLI, the panel, or the MCP tool surface.

Specific guardrails on the one path that can send mail:

- **Dry-run mode.** Set `REVENUEOS_DRY_RUN=1` (or `outreach.dry_run` in `revenueos.yaml`)
  and every send is written to `data/outputs/` instead of going over SMTP. This is the
  default posture for anyone evaluating RevenueOS without a mailbox connected.
- **Daily send cap.** `outreach.daily_cap` in `revenueos.yaml` (25 by default) is checked
  against the day's send count before every send; once reached, the send is refused until
  the next day rather than silently queued.
- **Suppression list.** Every recipient is checked against a persistent suppression table
  before a send goes out. An address lands on it from a reply containing "stop",
  "unsubscribe", "remove me", "opt out", or "no thanks" (matched by the inbox worker), or
  from a click on the unsubscribe link, or manually.
- **List-Unsubscribe and reply-STOP.** Every outbound message carries a
  `List-Unsubscribe` header (a `mailto:` target that files as a stop request) in addition
  to a plain-language "reply 'stop' to unsubscribe" line in the body.
- **CASL footer.** Every message identifies the sending business by name and mailing
  address in a footer block, alongside the unsubscribe instructions above — the
  identification Canada's anti-spam law requires for commercial email, applied to every
  send regardless of jurisdiction.

## Credentials

Provider keys, mailbox passwords and signing secrets come from the process environment, which
you control, and are never written to the company-context canon, `revenueos.yaml`, or the
database:

| Variable | What it unlocks |
|---|---|
| `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` | The Anthropic SDK as the LLM provider |
| *(none — auto-detected)* | A signed-in Claude Code CLI as the LLM provider, when no API key is set |
| `SMTP_PASSWORD` | Sending real outreach email |
| `IMAP_PASSWORD` | Reading the sending mailbox for replies/bounces/stops |
| `REVENUEOS_PANEL_PASSWORD` | The control panel's session login; required to bind it to anything but localhost |
| `REVENUEOS_WORKER_TOKEN` | Bearer-token protection for the orchestrator's status API |
| `REVENUEOS_LICENSE_SIGNING_KEY` / `REVENUEOS_LICENSE_SIGNING_KEY_FILE` | Vendor side only: the Ed25519 private key that signs licence keys. Verifying a key needs nothing |
| `REVENUEOS_LICENSE_SECRET` | Retired: verifying licence keys issued under the old HMAC scheme |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_PRO` / `_BUSINESS` / `_AGENCY` | Hosted billing checkout and webhook handling |
| One env var per connector (for example `AHREFS_API_KEY`, `GA4_ACCESS_TOKEN`) | That connector CLI; see `docs/integrations.md` |

`revenueos doctor` and `revenueos tools` both report what is configured without ever
printing a secret value.

### The exception: connections

A **connection** is an account you authorise once, and its proof has to outlive the process
that obtained it, so it is the one credential RevenueOS stores. `data/connections.json` holds
one record per provider — a Stripe secret key, Google and Meta OAuth access/refresh tokens, a
WordPress application password — written with mode 600.

Set `REVENUEOS_TOKEN_KEY` and those secrets are Fernet-encrypted at rest, and
`revenueos connections` tells you which state you are in. **Without that variable they are
plaintext in the file**, protected only by file permissions: anything running as your user, any
backup, and any sync client that reaches the workspace can read them. Set it, or accept that.

Two things bound the damage rather than prevent the read: the scopes the provider granted, and
`allow_write`, which is off until you turn it on, so a stolen read-only token cannot change
anything through RevenueOS. Revoke at the provider, then `revenueos disconnect <provider>`.

## Panel authentication

The control panel is deliberately small (no framework, no build step) and follows one
rule: it refuses to bind to a non-loopback address unless `REVENUEOS_PANEL_PASSWORD` is
set. With no password set, it only listens on localhost. With a password set, `/login`
exchanges it for a signed, time-limited session cookie; every route requires that cookie
except:

- `/health` — always public, so a load balancer or uptime check doesn't need credentials.
- `/billing/webhook` and `/billing/checkout` — Stripe's own signature verification and
  redirect flow are the controls there, not the session cookie.

## Orchestrator bearer token

The orchestrator's status API is unauthenticated by default — fine on localhost, not fine
exposed to a network. Set `REVENUEOS_WORKER_TOKEN` and every route on that API, including
its own health check, requires `Authorization: Bearer <token>`.

## What data leaves the machine

- **LLM prompts** contain a small, selectively assembled slice of your business canon
  (identity, audience, offer, messaging, voice, and any standing corrections from the last
  30 days) plus the specific item being worked on — one lead, one page, one skill's
  instructions. RevenueOS never sends your whole canon or your whole database in one
  prompt.
- **Crawl requests** go to your own configured website (and, for the SEO worker's
  authority check, a public domain-rating lookup against your configured competitors).
- **Market monitoring** searches Hacker News's public search index for threads relevant to
  your business.
- **Connector requests** go only to the platform a given connector CLI targets, and only
  when you have supplied that platform's API key.

Nothing else leaves the machine. There is no telemetry, analytics, or phone-home call back
to RevenueOS itself.

## Licence keys

A licence key unlocks a paid tier's gated capability (today, that is continuous
orchestrator operation). Keys are signed and verified offline: a key is a payload (tier,
customer email, expiry, `alg`, `kid`) plus an **Ed25519 signature** over that payload,
checked against the vendor public keys shipped in `billing.LICENSE_PUBLIC_KEYS` (a list, so
keys can rotate; extend it with `REVENUEOS_LICENSE_PUBLIC_KEY` if you run your own vendor
side). Verification needs no secret — a self-hosted install confirms the key it paid for
offline, and holding what it takes to verify does not let anyone mint keys; only the
vendor's private key (`REVENUEOS_LICENSE_SIGNING_KEY` / `..._FILE`) can sign one. Keys
issued under the older HMAC scheme still verify, but only where the retired
`REVENUEOS_LICENSE_SECRET` is set. Verification never calls out to a server. A missing
file, a bad signature, a key from an untrusted signer, or an expired key all fail closed to
the free Community tier; a licence never fails open.

## Reporting a vulnerability

See `SECURITY.md` for how to report a security issue and what response to expect.
