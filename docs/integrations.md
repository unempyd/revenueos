# Integrations — connections, permissions, executors

RevenueOS reads only what a stranger can read until the business authorises an account. Each
authorisation is a **connection**: a provider, the identity on the other side, the scopes it granted,
the tokens that prove it, and one switch the owner controls: **allow changes**.

## The permission model, in order

1. The provider's scopes bound what is possible (a read-only Stripe key cannot invoice, whatever we ask).
2. The owner flips **allow changes** per connection. Off by default. A connected account is read-only
   until a human turns it on, on the Connections page or with `revenueos connect … --allow-changes`.
3. Every change still arrives as an action a human approves on TODAY. The executor checks 1 and 2 at
   run time and returns a plain sentence when it refuses ("stripe is connected read-only — turn on
   'allow changes' …"). Nothing retries around a refusal.

Tokens live in `data/connections.json` (mode 600). Set `REVENUEOS_TOKEN_KEY` and they are sealed
(Fernet) at rest; `revenueos connections` says which state you are in.

## Providers

| Provider | Reads | Changes (executor) | Needs | Connect |
|---|---|---|---|---|
| Stripe | customers, subscriptions, invoices, charges → revenue 30d, MRR, open invoices (`billing` worker; Spend page) | `send_invoice`: create and send an invoice to a lead | a secret or restricted key | `revenueos connect stripe --key sk_…` |
| Website in git (GitHub Pages, Netlify, Vercel) | HTML files | `site_deploy`: canonical, JSON-LD schema, title, meta description committed and pushed | repo, site path, a token that can push (`gh auth login` or `GITHUB_TOKEN`) | `revenueos connect github_site --repo owner/name --path website` |
| WordPress | pages | `site_deploy`: the same head fixes through the REST API; `publish_post`: a produced `content` deliverable, converted from markdown to HTML and posted via `POST /wp-json/wp/v2/posts`, after its own separate approval — measured by fetching the published URL for the deliverable's title | site URL, user, Application Password | `revenueos connect wordpress --site … --user … --app-password …` |
| Google | Search Console queries/pages, GA4 sessions/conversions (`analytics` worker); Google Ads campaigns, keywords with quality score, search terms with their added/excluded state, campaign negatives and negative-keyword lists (`ads-live` worker) | `book_call` on Google Calendar with a Meet link; `ads_pause`, `ads_budget` on Google Ads (`ads-live` worker) | an OAuth client (`GOOGLE_OAUTH_CLIENT_ID/_SECRET`); Google Ads also `GOOGLE_ADS_DEVELOPER_TOKEN` + `GOOGLE_ADS_CUSTOMER_ID` | `revenueos connect google` (browser consent) or the Connections page |
| Meta Ads | campaigns, spend, results (`ads-live` worker) | `ads_pause`, `ads_budget` | a Meta app (`META_APP_ID/_SECRET`) + `META_AD_ACCOUNT_ID` | `revenueos connect meta` |
| Calendar invites by email | — | `book_call`: RFC 5545 invite sent from the business mailbox when Google is not connected | the outreach mailbox (`smtp` in revenueos.yaml + `SMTP_PASSWORD`) | automatic |

OAuth uses the authorisation-code flow with PKCE. From the CLI a loopback listener on 127.0.0.1
catches the redirect; from the hosted panel the callback is `/connections/<provider>/callback`.
Refresh happens on demand and the refreshed token is written back.

## What was proven at runtime (2026-09-13)

- **Stripe, live account**: connected; `billing` recorded real numbers (0 customers, MRR 0.00 AUD,
  revenue 30d 0.00 AUD); `send_invoice` created a draft invoice to our own address and deleted it;
  the permission switch refused the same call while off.
- **Site executor, live**: with the public repo connected as the site, `seo` found the two head
  defects on unempyd.github.io/revenueos as deployable fixes; approved, executed → two commits pushed
  by the executor; the live page carried both tags within 20 s; `measure` recorded
  `canonical_present 0 → 1` and `local_schema_present 0 → 1`.
- **Book the call, live**: an invite went out from the RevenueOS mailbox (to itself) through the
  executor; the inbox worker read the mailbox afterwards.
- **Hosted, multi-tenant**: two customer accounts on one host, each bound to its own workspace by a
  signed session; unauthenticated and wrong-password requests get 401; each tenant sees only its own
  TODAY, Connections and Spend.
- **Google and Meta adapters**: every request shape exercised against mocked endpoints in
  `tests/test_connections.py`; live use waits on the OAuth client / app the owner registers once.
- **Google Ads specifically, still unproven live.** The campaign, keyword, search-term and
  negative-keyword reads have only ever run against mocked request shapes. Two things gate a live
  run, and neither is code: the `adwords` scope is not in `DEFAULT_SCOPES`, so it must be asked for
  (`revenueos connect google --scopes identity,searchconsole.read,analytics.read,calendar.write,ads.read`);
  and a developer token comes from the API Centre of a Google Ads **manager** account, which a Google
  account with no Google Ads account cannot have — test access reaches only test accounts, and basic
  access needs a review that takes days. Until then `ads-live` refuses with the sentence that names
  whichever prerequisite is missing rather than surfacing a 401.

## Hosted mode

```bash
revenueos workspace new /srv/revenueos           # the host root
revenueos --root /srv/revenueos accounts add owner@salon.example 'a long password'
revenueos --root /srv/revenueos serve --host 0.0.0.0 --port 8791   # accounts.json present → login by email
```

Each account gets `tenants/<slug>/`, a full workspace. The orchestrator runs per workspace
(`REVENUEOS_ROOT=/srv/revenueos/tenants/<slug> revenueos orchestrator`). `deploy/` holds the container
and Fly/Railway configs; the same image serves both single- and multi-tenant modes.
