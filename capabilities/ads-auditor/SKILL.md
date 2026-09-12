---
name: revenueos-ads-auditor
description: "RevenueOS Ads Auditor — ingest a paid-media export (Google, Meta, YouTube, LinkedIn, TikTok, Microsoft, Apple, Amazon, Reddit, Pinterest, Snapchat or X) and flag wasted spend, over-pacing campaigns and dangerous spend concentration. Use when the user says \"audit our ad account\", \"where is our ad spend being wasted\", \"check this Google Ads export\", \"is any campaign overpacing its budget\", \"score our ad account health\", or hands over a CSV of ad performance data."
metadata:
  version: 0.1.0
  vendor: RevenueOS
---

# RevenueOS Ads Auditor

RevenueOS Ads Auditor ingests a CSV export from any of twelve ad platforms and runs
deterministic checks that need no LLM: campaigns spending money with zero conversions,
campaigns pacing more than 25% over their daily budget, and single campaigns holding more
than 60% of total spend. Each finding becomes an `ad_waste` or `campaign_attention` action.

## When to use this

- The user has (or can export) a campaign performance CSV from an ad platform and wants it
  audited for waste, pacing problems, or concentration risk.
- The user asks "is our ad spend healthy" or "which campaigns should we pause".
- Findings JSON from a deeper prose audit already exists and the user wants a scored report.

## How to run it

CLI:

```bash
# drop the export at data/exports/ads-<platform>.csv first, then:
revenueos run ads-audit
revenueos today                 # see ad_waste / campaign_attention actions
```

MCP tool:

- `revenueos_ads_audit(csv_path: str, platform: str)` — copies `csv_path` into
  `data/exports/ads-<platform>.csv` and runs the audit. `platform` is one of: `google`,
  `meta`, `youtube`, `linkedin`, `tiktok`, `microsoft`, `apple`, `amazon`, `reddit`,
  `pinterest`, `snapchat`, `x`.
- `revenueos_today()` / `revenueos_results()` — see queued findings / measured outcomes.
- `revenueos_measure()` — checks the next export for whether spend/conversions actually
  changed after a recommendation was executed.

## Inputs

A CSV in the 13-column generic export format: `date, account_id, account_name, campaign_id,
campaign_name, campaign_status, creative_id, creative_name, conversion_action, conversions,
budget, spend, currency`. Export this from the ad platform's UI or API and save it (or hand
its path to `revenueos_ads_audit`); the file lands at `data/exports/ads-<platform>.csv`.

Optionally, a `data/exports/ads-<platform>.findings.json` file (produced by a deeper,
skill-driven audit) unlocks a full scored report at `data/reports/ads-<platform>.md` — this
requires `ads.data_lifecycle.attested` plus `encryption_evidence` to be set in the
workspace's `revenueos.yaml`; without it, findings are still audited but no health score is
rendered over non-attested data.

## What it produces

`ad_waste` actions (spend with zero conversions) and `campaign_attention` actions
(over-pacing or spend concentration), each with the before-state (spend, conversions,
window) recorded so `measure` can compare against the next export. When a findings file and
attestation are present, a Markdown report with a deterministic health score is written to
`data/reports/ads-<platform>.md`.

## Approval rule

The audit only reads exports and queues actions — no campaign is paused, no budget is
changed, and nothing is sent to the ad platform. Executing a queued action (`revenueos
execute <id>` / `revenueos_execute` after `revenueos_approve`) runs the matching the RevenueOS ads audit engine
skill and writes its recommendation to `data/outputs/` for a human to act on manually; the
Community tier never has live ad-platform write access.
