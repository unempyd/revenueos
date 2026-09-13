# RevenueOS Community vs. RevenueOS Hosted

RevenueOS is self-hosted software: the install is yours either way. What a paid tier adds
is unlocked in the running software by a signed licence key, and — for the tiers above
Pro — a set of commitments RevenueOS Inc. delivers as a hosted service around that
software. This page draws that line explicitly.

## RevenueOS Community — free

Everything you need to run the loop by hand, on your own machine, forever:

- The `revenueos` CLI, in full — `init`, `run`, `today`, `approve`, `execute`, `ignore`,
  `results`, `doctor`, `skills`, `tools`.
- Every worker (`discover`, `outreach`, `inbox`, `seo`, `ads-audit`, `content`, `monitor`,
  `measure`), run manually, one at a time or all at once.
- The full capability catalogue: 13 packs, 790 skills, 104 agent definitions, and all 64
  connector CLIs.
- The MCP server (`revenueos-mcp`), so an MCP host such as Claude Code or Claude Desktop
  can drive the same loop directly.
- Dry-run outreach (`REVENUEOS_DRY_RUN=1`): drafts and sends write to `data/outputs/`
  instead of a real mailbox, so the full loop is safe to try with nothing connected.

Community has no time limit, no seat limit, and no feature countdown. It is not a trial.

## What a licence unlocks in the running software

This is `TIER_FEATURES` from the licensing code, exactly as shipped — what each paid tier
adds on top of everything below it:

**Pro — $99/month**
- Continuous operation via the orchestrator (cron scheduling, always-on)
- Advanced audits
- Historical reporting
- Automated monitoring
- Persistent business memory (learning-loop corrections beyond 30 days)

**Business — $299/month** *(everything in Pro, plus)*
- Multi-brand workspaces
- Multiple users
- CRM sync
- Campaign fleets
- Approval controls / team workflows
- Centralized analytics

**Agency — $999/month** *(everything in Business, plus)*
- Multi-client management
- White-label reports
- Account fleets
- Centralized controls
- API access
- Client workspaces

## What is gated in code today, and what is a hosted-service commitment

Be precise about the difference, because it matters to what you're buying:

- **Gated in code today, pay-on-result:** continuous orchestrator operation. `revenueos orchestrator`
  runs free until the workspace has a measured result on an action you approved, then 14 more
  days, then it asks for a Pro-or-higher licence and tells you why (`billing.pay_on_result`).
  There is no clock before you have seen a result. One-shot runs
  (`revenueos run <worker>`, `revenueos run all`) are never gated — Community can run
  every worker as often as you like by hand or from your own cron.
- **Hosted-service commitments, not code gates:** the remaining Pro features (advanced
  audits, historical reporting, automated monitoring, persistent memory) and everything
  listed under Business and Agency are what RevenueOS Inc. operates and supports for you
  as a hosted product — a control panel deployed and kept running on your behalf,
  connectors configured and maintained, execution approved through a managed workflow,
  a measurement history retained over time, and billing handled end to end. They are not
  independently enforced by a second licence check in this codebase the way continuous
  operation is; they are what you are paying RevenueOS Inc. to run and stand behind.

## How upgrading works

A checkout (`/billing/checkout?tier=pro|business|agency`, backed by Stripe) issues a
signed licence key by email; `revenueos license install <key>` applies it to your
workspace, verified offline against the vendor's signing secret. `revenueos license show`
reports your current tier and every feature it unlocks. See
`docs/security-and-approval.md` for how licence verification works and fails safe to
Community.
