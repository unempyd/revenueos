# Integrations

What RevenueOS connects to today, and how each connection actually works.

## Website (crawl)

The `seo` and `measure` workers crawl the website configured in `revenueos.yaml`
(`website`) directly — no API key needed. The crawl looks for missing or duplicate page
titles and meta descriptions, thin pages, and a missing sitemap; `measure` re-crawls a
page after a fix is executed to confirm whether it actually changed. A domain-authority
comparison against your configured competitors uses a public domain-rating lookup; when
that endpoint is unavailable, the comparison is reported as unavailable rather than
guessed.

## Hacker News

The `monitor` worker searches Hacker News's public search index for threads relevant to
your business (built from your canon: what you do, your wedge, your audience, and a set
of search seeds). No API key is required. Every candidate thread passes a strict,
default-reject relevance gate before it becomes a market-signal action — most candidates
are rejected.

## Ad exports (CSV)

The `ads-audit` worker reads CSV files you drop in `data/exports/`, named
`ads-<platform>.csv` (for example `ads-google.csv`, `ads-meta.csv`), where `<platform>` is
one of `google`, `meta`, `youtube`, `linkedin`, `tiktok`, `microsoft`, `apple`, `amazon`,
`reddit`, `pinterest`, `snapchat`, `x`. Each file is a generic 13-column export:

```
date, account_id, account_name, campaign_id, campaign_name, campaign_status,
creative_id, creative_name, conversion_action, conversions, budget, spend, currency
```

The worker flags campaigns spending with zero conversions, spend concentrated in one
campaign (over 60% of total spend), and campaigns pacing more than 25% over their daily
budget — all deterministically, no LLM required. Dropping an updated export after a fix
lets `measure` compute the spend/conversions delta.

## Lead CSV drops

The `discover` worker reads any file matching `data/exports/leads*.csv`, using the column
set common to lead-export tools such as Instantly and Smartlead:

```
email, first_name, last_name, company, title, website, linkedin_url, reason
```

Each new row becomes one `prospect` action.

## SMTP / IMAP mailbox

Real outreach sending and reply/bounce detection use your own mailbox: `smtp.host` and
`imap.host` in `revenueos.yaml`, with `SMTP_PASSWORD` / `IMAP_PASSWORD` supplied via the
environment (never stored in the workspace). Without a mailbox connected, `outreach` still
drafts (or writes sends to `data/outputs/` in dry-run mode) and `inbox` still processes
`.eml` files dropped in `data/exports/inbox/` for offline testing.

## Connector CLIs

`tools/clis/` ships 64 small, dependency-free connector scripts, each a standalone Node.js
file that talks to one external platform once you export its API key. Run any of them
directly (`node tools/clis/<name>.js <resource> <action> ...`); `revenueos tools` lists
every connector and marks which ones have a credential present in your environment.

**Analytics** — Adobe Analytics, Amplitude, Coupler, GA4, Hotjar, Mixpanel, Optimizely,
Pendo, Plausible, Segment, SimilarWeb, Supermetrics

**CRM / sales engagement** — ActiveCampaign, Close, Crossbeam, Customer.io, Intercom,
Kit, Klaviyo, Mailchimp, Outreach, PartnerStack

**Email** — Beehiiv, Brevo, Demio, Instantly, Lemlist, Postmark, Resend, SendGrid

**SEO** — Ahrefs, DataForSEO, Google Search Console, Keywords Everywhere, RankParse,
SEMrush

**Ads** — Google Ads, LinkedIn Ads, Meta Ads, TikTok Ads

**Enrichment** — Apollo, Clay, Clearbit, Exa, G2, GitHub Prospects, Hunter, Snov,
Trustpilot, ZoomInfo

**Other** — AirOps, Buffer, Calendly, Dub, Livestorm, Mention Me, OneSignal, Paddle,
Rewardful, SavvyCal, Tolt, Typeform, Wistia, Zapier

Each connector reads exactly one credential set from the environment (for example
`AHREFS_API_KEY`, or `GA4_ACCESS_TOKEN`) and does nothing without it — there is no shared
credential store and no fallback to a stored key. Note that the `outreach` connector CLI
talks to the Outreach sales-engagement platform; it is unrelated to RevenueOS's own
`outreach` worker described in `docs/architecture.md`.

## An optional external lead-generation service

The `discover` worker can source prospects from OpenOutreach, an independent,
separately-licensed lead-generation service that runs as its own process or container
(`docker compose --profile outreach`). RevenueOS never links its code in-process; the
only contract between the two is OpenOutreach's own JSON-Lines output on stdout, which
`discover` reads and ingests exactly like a CSV drop. Nothing else is shared.

## Search Console and GA4 — connector present, measurement wiring not yet built

`tools/clis/google-search-console.js` and `tools/clis/ga4.js` exist and will talk to
their respective APIs once you supply an access token, but neither is wired into the
`measure` worker yet. Today, SEO outcomes are measured by re-crawling the page directly;
connecting Search Console or GA4 output to `measure` so that indexing and traffic changes
become recorded outcomes is on the roadmap, not shipped.
