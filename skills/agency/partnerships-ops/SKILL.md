---
name: partnerships-ops
description: "Partner, channel, and ecosystem marketing for B2B SaaS. Use this skill for co-marketing with technology alliances and channel partners, integration launches, joint campaigns and content, partner tiering and enablement, and cloud-marketplace go-to-market (AWS, Azure, GCP listings, private offers, committed-spend drawdown). Also triggers on: partner marketing, co-marketing, channel, reseller, alliance, ecosystem, integration launch, marketplace listing, AWS Marketplace, private offer, MDF, joint webinar, co-branded."
---

# Partnerships Operations

## Step 0 (always first): Load brand context

**Before producing any deliverable, look for a `brand-context.md` file** in the user's project root (also check `./.claude/brand-context.md` and `./docs/brand-context.md`). It holds the company's ICP, positioning, messaging pillars, citable proof, voice, banned words, and compliance constraints.

- **If it exists:** read it in full and treat it as binding for this run. Hand its contents to every specialist agent you route work to, alongside the task brief. Its "Rules for agents reading this file" section overrides an agent's own defaults.
- **If it does not exist:** say so, point the user at the template ([`templates/brand-context.md`](../../templates/brand-context.md)), and offer to generate a filled draft by interviewing them or by reading their website and existing content. Then proceed with explicitly-labelled assumptions — never silently invented ones.

**Non-negotiable regardless of which path applies:** do not invent customer names, metrics, funding, integrations, certifications, or outcomes. Only proof recorded in `brand-context.md` (or supplied directly in the request) may be used as fact. Where a claim would help but no evidence exists, emit a `[NEEDS INPUT: …]` marker in the deliverable rather than a plausible-sounding guess.

---

## What This Is

Partnerships Operations markets *with and through other companies* rather than directly to buyers. It covers technology alliances and integrations, channel and reseller motions, and the cloud-marketplace buying path that increasingly decides how enterprise software actually gets purchased — the listing, the private offer, and the committed-spend drawdown that makes a deal easy for a buyer to say yes to.

## The Team: 1 Specialist Agent

| # | Agent | File | What They Do |
|---|-------|------|-------------|
| 1 | Partner Ecosystem Marketer | `agents/partner-ecosystem-marketer.md` | Owns partner tiering and joint business plans, co-marketing campaign design (joint webinars, co-authored research, co-branded assets, integration launches), partner enablement and MDF programs, and cloud-marketplace GTM: listing content, pricing dimensions, private offers, CPPO, and committed-spend strategy. |

## How to Use

### Routing User Requests

**Co-marketing & alliances** → Partner Ecosystem Marketer
- "Design a joint campaign with our integration partner"
- "Plan the launch of our new integration"
- "Build a partner tiering model and joint business plan"
- "Create a co-marketing kit our partners can actually run"

**Marketplace GTM** → Partner Ecosystem Marketer
- "Write our AWS Marketplace listing and pricing dimensions"
- "How should we use private offers and committed spend?"
- "Plan a marketplace-led motion for enterprise buyers"

### Working Method

1. **Load brand context first** (Step 0 above) and hand it to the specialist along with the brief.
2. **Route to the specialist** whose remit matches the request. Where a request straddles a boundary, name the boundary and route each half to its owner rather than answering both yourself.
3. **Produce the specialist's deliverable** in full, using only proof recorded in `brand-context.md` or supplied in the request. Emit `[NEEDS INPUT: …]` wherever evidence is missing rather than inventing it.
4. **Name the handoffs.** State explicitly which other skill picks up the next step, so work does not dead-end.

**Boundaries this skill respects.** The counterparty test decides ownership: if it is a **person**, it belongs to `social-influencer-partnerships`; if it is a **company with a contract or a listing**, it belongs here. Earned media and third-party gatekeepers stay with the comms skill; link acquisition stays with `seo-link-building-strategist`.

## Output Standards

- Every deliverable names its audience, its owner, and the decision it enables.
- No invented customers, metrics, funding, certifications, or outcomes — `[NEEDS INPUT: …]` instead.
- Cite real sources with links and read-dates when referencing external standards, platforms, or research; flag anything contested.
- Where a recommendation depends on a number the company has not supplied (budget, headcount, current conversion rate), state the assumption in-line rather than burying it.
