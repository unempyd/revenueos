# RevenueOS Community vs. RevenueOS Hosted

RevenueOS is software you run: the install is yours either way, and everything runs free
until it has measured a result you approved, then 14 more days. What a paid tier adds is
unlocked in the running software by a signed licence key, and — for the tiers above Pro —
a set of things we do for you around that software, on request. This page draws that
line explicitly.

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
adds on top of everything below it. Read it as a price list, not a feature inventory: several
of these names describe work we would do for you, and the Business and Agency ones describe
software that **does not exist yet** (see the next section).

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
- **Built, but not gated:** the remaining Pro names describe things Community already has in
  full — the 414-control ads audit and the SEO audit run for everyone, every measured outcome is
  kept in the workspace database, `monitor` and the orchestrator schedule are available to
  everyone, and corrections are injected for the last 30 days on every tier. Paying for Pro buys
  continuous operation and our support for it, not access to those.
- **Not built yet:** everything listed under Business and Agency. RevenueOS runs one business per
  install today; multi-brand workspaces, multiple users, CRM sync, centralised analytics, client
  fleets, white-label reports and a documented API do not exist, and no licence check gates them
  because there is nothing to gate. A workspace per client (`revenueos workspace new`) and the
  multi-tenant panel are the closest things that work, and both are free. Do not buy those tiers
  expecting the list; ask us what is real before paying.
- **Things we do for you, not code gates:** running RevenueOS on your behalf when you ask — the
  panel kept running, connectors configured and maintained, execution approved through the same
  approval surface, a measurement history retained over time, and billing handled end to end.
  None of it is enforced by a second licence check the way continuous operation is; it is what
  you are paying us to run and stand behind. Today it is arranged by request (the "Request"
  links on the pricing page), not by a self-serve sign-up.

## How upgrading works

When RevenueOS has measured a result you approved, you get a Stripe payment link for the
tier you asked for; after Stripe confirms the payment, a signed licence key is emailed to
you and `revenueos license install <key>` applies it to your workspace, verified offline
against the vendor's public key, which ships with RevenueOS — your install needs no secret
of ours to confirm the licence you paid for, and nothing it holds could mint one. (The
`/billing/checkout?tier=…` endpoint and the
Stripe webhook that issues keys exist in the code for a self-serve flow; they are not
the way early-access customers pay today.) `revenueos license show`
reports your current tier and every feature it unlocks. See
`docs/security-and-approval.md` for how licence verification works and fails safe to
Community.
