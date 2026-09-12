# Proof — a measured run of the loop

This page is a customer-facing account of one real run of RevenueOS end to end. The
internal, unedited run log this is based on stays at `docs/PROOF.md` for the engineering
record; the facts, numbers, and caveats below are the same.

**Date:** 2026-09-12. **Machine:** macOS, no `ANTHROPIC_API_KEY` set — a signed-in Claude
Code CLI was the LLM provider for this run. **Network:** live. **Business connected:**
Plausible Analytics (plausible.io), a real company, connected read-only via its public web
presence. No email was sent to anyone during this run.

The loop under test: Connect → Discover → Approve → Execute → Measure, with the results
recorded and displayed at every step.

## What happened, in order

1. **Connect.** A fresh workspace was created and onboarded with the business's
   information. The company-context canon validated cleanly.
2. **Discover — SEO.** A real crawl of plausible.io found 12 pages, 0 crawl defects
   (every page already had a title, a description, and enough body text), and the
   domain-authority comparison came back unavailable (the public rating endpoint used
   for that check returned HTTP 403 during the run) — reported as unavailable rather
   than guessed.
3. **Discover — monitor.** A real Hacker News search surfaced 3 candidate threads; the
   relevance gate, which default-rejects, passed none of them through. Zero market
   signals were created.
4. **Discover — content.** Matching the business's channels (SEO, blog, Twitter) against
   the skill catalogue produced 6 genuine content opportunities for the week.
5. **Present.** The daily brief showed all 6 content opportunities, $0 pipeline generated
   (accurate — no outreach had run).
6. **Approve.** One opportunity — an SEO content brief — was approved.
7. **Execute.** Approving triggered the matching skill; the LLM produced a full SEO
   content brief (14,014 bytes, about 1 minute 23 seconds wall clock) targeting a real
   query, written to disk.
8. **Measure.** The measure step recorded 1 result measured, 0 still pending: the
   deliverable's presence on disk, observed going from absent to present.
9. **Record and display.** RESULTS showed: 6 opportunities found, 1 executed, 1
   measured; 0 emails sent, 0 replies, 0 bounces, 0 booked, $0 pipeline.

## The same loop over HTTP

The control panel was run with a password set, and exercised over real HTTP requests:

| Request | Result |
|---|---|
| Health check | `{"ok": true, "onboarded": true}` |
| The marketing site, served from the panel | 200 OK |
| A billing checkout attempt with no Stripe key configured | 400, "STRIPE_SECRET_KEY is not set" — the correct behaviour without vendor keys installed |
| The results view, unauthenticated | 401 |
| The results view, after logging in | 1 executed, 1 measured, the deliverable's before/after recorded |
| The brief, as JSON | `{"found": 6, "executed": 1, "measured": 1, ...}` |

## What is honest about these numbers

- **Opportunity quality.** The crawl genuinely found nothing wrong with plausible.io.
  The domain-authority comparison was reported unavailable rather than fabricated when its
  data source returned an error. The relevance gate rejected every candidate market
  signal. The six content opportunities are the real output of matching this business's
  channels against the skill catalogue — nothing was seeded or staged.
- **Measurement.** "A deliverable now exists on disk" is what RevenueOS can observe for a
  content action without a search-console connection. The other measurement paths — a
  fixed meta description confirmed by re-crawl, a reply confirmed over the mailbox, an ad
  spend delta confirmed by the next export — are exercised by the automated test suite
  against real state changes, not demonstrated live in this run because it had no ad
  exports or mailbox connected.
- **Revenue.** $0 pipeline is true and expected: no lead was contacted in this run. The
  pipeline figure only moves once the outreach loop runs against a real mailbox with real
  replies — a path this run did not have credentials for, though it is covered end to end
  by tests using a dry-run sender and simulated replies.
- **Cost.** One LLM-run skill execution cost roughly $0.20–$0.50 in API-equivalent tokens
  on this run's provider; the direct API path is typically cheaper.

## Reproduce it

```bash
uv sync --extra dev
revenueos workspace new /tmp/ros-demo
revenueos --root /tmp/ros-demo init                 # or --answers file.json
revenueos --root /tmp/ros-demo run all && revenueos --root /tmp/ros-demo today
revenueos --root /tmp/ros-demo approve <id> && revenueos --root /tmp/ros-demo execute <id>
revenueos --root /tmp/ros-demo run measure && revenueos --root /tmp/ros-demo results
```
