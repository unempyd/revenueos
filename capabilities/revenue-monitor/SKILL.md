---
name: revenueos-revenue-monitor
description: "RevenueOS Revenue Monitor — the daily brief and results ledger: what RevenueOS found, what it did, and what measurable result occurred, plus watching Hacker News for conversations worth joining. Use when the user says \"what's today's brief\", \"show me the results\", \"what has RevenueOS done so far\", \"is anyone talking about us online\", or asks for the current pipeline value or measured outcomes."
metadata:
  version: 0.1.0
  vendor: RevenueOS
---

# RevenueOS Revenue Monitor

RevenueOS Revenue Monitor is the customer-facing surface of the whole loop: TODAY (pending
opportunities and pipeline value) and RESULTS (what was executed and what measurably
happened), plus a market-signal watch over Hacker News for conversations the business
genuinely belongs in.

## When to use this

- The user wants the daily brief: what's pending, what needs a decision.
- The user wants to know what RevenueOS has actually done and whether it worked.
- The user wants to check for relevant public conversations to join.
- Any executed action needs its measurable result checked or refreshed.

## How to run it

CLI:

```bash
revenueos today                 # the brief: pending actions, pipeline value, metrics
revenueos results                # what was executed and what measurably happened
revenueos run monitor           # scan Hacker News for on-topic conversations
revenueos run measure           # record outcomes for every executed action
```

MCP tool / resource:

- `revenueos_today()` (or resource `revenueos://today`) — pending actions, pipeline value,
  latest metrics, as JSON or rendered text.
- `revenueos_results()` (or resource `revenueos://results`) — executed actions with their
  outcomes, plus the summary line (found / executed / measured / sent / replies / booked).
- `revenueos_run("monitor")` — finds Hacker News threads that pass a strict, default-reject
  relevance gate and queues a `market_signal` action with a suggested angle for each.
- `revenueos_measure()` — checks every executed action (SEO re-crawl, sent-email replies,
  next ad export, written deliverables) for a real, evidenced result.

## Inputs

No file upload. Reads the workspace's own action/outcome history plus, for `monitor`, the
business's category and search seeds derived from the onboarding canon (no API key needed —
Hacker News search is public).

## What it produces

The TODAY brief (counts by opportunity type, total pipeline value, the pending action list)
and the RESULTS ledger (every executed action with its outcome: `measured`, `no_effect`,
`pending`, or `unmeasurable` — each with a note on what would measure it if it can't yet be
measured). `monitor` additionally queues `market_signal` actions for threads worth joining.

## Approval rule

This capability is entirely read-only reporting plus discovery — it creates `market_signal`
actions for a human to review, but never posts or replies anywhere on its own. Outcomes are
never fabricated: a result is `measured` only when real evidence (a re-crawl, a reply, an
export delta, a file on disk) confirms it.
