---
name: revenueos-lead-discovery
description: "RevenueOS Lead Discovery — find and qualify prospects for a connected business, either through an optional external prospecting service (see docs/integrations.md) (a separate process, when installed) or by importing a CSV drop of leads. Use when the user says \"find us new prospects\", \"import this lead list\", \"who should we be selling to\", \"discover qualified leads\", or hands over a CSV export from Instantly, Smartlead, or a similar prospecting tool."
metadata:
  version: 0.1.0
  vendor: RevenueOS
---

# RevenueOS Lead Discovery

RevenueOS Lead Discovery finds prospects that match the connected business's ideal customer
profile and turns each one into a `prospect` action, ready to feed RevenueOS Sales Follow-up.

## When to use this

- The user wants new prospects found for outreach.
- The user has a lead export (from Instantly, Smartlead, or any similarly-shaped CSV) to
  bring into RevenueOS.
- an optional external prospecting service (see docs/integrations.md) is installed and should be run to source fresh leads automatically.

## How to run it

CLI:

```bash
# optional: drop a lead export at data/exports/leads<anything>.csv first, then:
revenueos run discover
revenueos today                 # see the new prospect actions
```

MCP tool:

- `revenueos_discover_leads(csv_path?: str)` — pass `csv_path` to import one CSV file first
  (copied into `data/exports/leads-<timestamp>.csv`); always also picks up an optional external prospecting service (see docs/integrations.md)
  results if the `the external prospecting service` CLI or its Docker Compose service is available, and any CSV
  already sitting in `data/exports/`.
- `revenueos_today()` — see the resulting `prospect` actions.

## Inputs

A CSV using the an optional external prospecting service (see docs/integrations.md) / Instantly / Smartlead column convention: `email, first_name,
last_name, company, title, website, linkedin_url, reason`. Only `email` or `company` is
required per row; `reason` (why this lead qualifies) is used verbatim as the action's
qualification note and later personalises the first outreach line.

## What it produces

One `prospect` action per new lead (deduplicated by lead identity, so re-running is safe),
each recorded in the workspace's lead table with a `new` status ready for RevenueOS Sales
Follow-up to draft against.

## Approval rule

Discovery only reads and records — it never contacts a prospect. Nothing is sent until
RevenueOS Sales Follow-up drafts an email for the lead and that draft is separately approved
and executed.
