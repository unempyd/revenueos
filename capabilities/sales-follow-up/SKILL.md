---
name: revenueos-sales-follow-up
description: "RevenueOS Sales Follow-up — draft first-touch cold emails for newly discovered prospects from the business's own canon, and process inbound replies, bounces and STOP requests. Use when the user says \"draft follow-up emails\", \"write outreach for these leads\", \"check for replies\", \"who unsubscribed\", or asks RevenueOS to send a cold email — sending always requires an explicit approval step from the human."
metadata:
  version: 0.1.0
  vendor: RevenueOS
---

# RevenueOS Sales Follow-up

RevenueOS Sales Follow-up drafts deterministic, personalised first-touch emails for
prospects found by RevenueOS Lead Discovery, and reads the inbox to classify replies,
bounces and unsubscribe requests. **Every send requires a human approval step — this
capability never sends on its own initiative.**

## When to use this

- New prospects exist (from RevenueOS Lead Discovery) and need a first-touch draft.
- The user wants to check for replies, bounces, or STOP requests on a sending mailbox.
- The user explicitly asks to send an approved draft.

## How to run it

CLI:

```bash
revenueos run outreach          # draft follow_up actions for undrafted prospects
revenueos run inbox             # classify replies / bounces / STOPs against sent mail
revenueos today                 # review drafts awaiting approval
revenueos approve <id>          # approve one draft
revenueos execute <id>          # send it (only after approve)
```

MCP tool:

- `revenueos_draft_followups()` — drafts first-touch emails for qualified prospects; never
  sends. Each draft becomes a `follow_up` action.
- `revenueos_approve(action_id)` then `revenueos_execute(action_id)` — the only path that
  sends an email, and only for an action already approved.
- `revenueos_today()` / `revenueos_results()` — review drafts / delivery and reply outcomes.

## Inputs

No file upload — drafts are built from the business's own canon (offer, differentiators,
pain points, tone) plus each lead's qualification `reason`. Sending needs `smtp.host` /
`sender.email` in `revenueos.yaml` and `SMTP_PASSWORD` in the environment; reading replies
needs `imap.host` and `IMAP_PASSWORD` (or `.eml` files dropped in `data/exports/inbox/`).

## What it produces

One `follow_up` action per drafted email, holding the subject and body chosen by a
deterministic hook/subject split (personalised further when an LLM credential is present).
Approving and executing a `follow_up` action sends the email (recorded immutably) and
inbox processing later updates it with `replied` / `bounced` outcomes.

## Approval rule — the safe default

**`REVENUEOS_DRY_RUN=1` is the Community default**: with it set, "sending" writes the
rendered email to `data/outputs/` instead of using real SMTP, so the whole loop can be
exercised with zero risk of contacting a real prospect. Drafts always sit as `pending`
actions; nothing sends until a human calls `revenueos approve` and then `revenueos execute`
(or the equivalent MCP tools) on that specific action. A daily send cap and a suppression
list (STOP replies, bounces) are enforced at execute time regardless of dry-run.
