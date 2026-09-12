---
name: pmm-battlecard
trigger: manual ("battlecard for [competitor]") + auto-trigger when competitive_intel logs ≥3 HIGH-relevance moves from one competitor in a quarter
load-context: identity, domain-knowledge, communication-style, goals-and-priorities
sub-skills: hallucination-guard
ends-with: Battlecard written to draft table (kind=battlecard) + saved to your Battlecards folder
model: claude-opus-4-7-[1m]
---

# pmm-battlecard

Produces named-competitor battlecards for sales conversations. Internal use only. Battlecards never go external. Your locked messaging keeps named competitors out of public content.

Each battlecard answers: how do we win this specific competitive opp.

## When to invoke

- Manual: human types "battlecard for [competitor]"
- Quarterly competitive refresh
- Auto-trigger: 3+ HIGH-relevance moves from one competitor in a quarter

## Inputs

| Input | Source |
|---|---|
| Competitive intel | competitive_intel last 180 days for named competitor |
| Recent meeting signals | meeting_log last 60 days mentioning the competitor |
| Public sources | Defuddle/WebFetch scrape of competitor website, blog, press |
| Win/loss data | Manual paste from human |
| Your locked positioning | Your master messaging doc |

## Battlecard Structure

```markdown
# Battlecard: [Your Company] vs. [Competitor]

**Last updated:** [Date]
**Confidence:** [High / Medium / Low based on data freshness]
**Internal use only. Do not share externally.**

## Quick read

[3 sentences. Who [Competitor] is. Where they win. Where you win.]

## Who they are

[1 paragraph. Stage, funding, customer base, primary positioning. Sourced.]

## Their pitch

[How [Competitor] positions against you. What they lead with. What they downplay.]

## How they win

[2 to 3 scenarios where [Competitor] beats you. Be honest. If they're better at workflow automation for analysts, say so.]

## How we win

[3 to 5 scenarios where you beat [Competitor]. Tied to your differentiators.]

## Discovery questions to ask the buyer

[5 to 7 questions the AE can ask to surface whether this deal is winnable. Pull the prospect toward your strengths.]

## Common objections + responses

| Objection | Response |
|---|---|
| "We're already piloting [Competitor]" | [Response] |
| "[Competitor] has more X" | [Response] |
| ... | ... |

## Proof points to use

[Anonymized customer references that resonate against this competitor.]

## What NOT to say

[Things that would make you look defensive or generic. Disclosure rules. Voice rules.]

## Recent moves to know

[Last 90 days from competitive_intel. What changed. What it means.]

## Updates trigger

[When to refresh this battlecard.]
```

## Process

1. Pull all competitive_intel rows for the named competitor
2. Pull meeting_log mentions last 60 days
3. Scrape competitor's current website, pricing page, blog
4. Cross-reference with your locked positioning
5. Build battlecard following the structure above
6. Run hallucination guard (every claim about competitor must be sourced)
7. Save to your Battlecards folder
8. Write draft row, kind=battlecard, status=ready_for_review
9. Slack ping the VP: "Battlecard for [Competitor] ready. Confidence [level]."

## Hard gates

- Every claim about the competitor cites a source (URL or meeting_log line)
- Your positioning aligned with locked messaging
- Disclosure rules apply (no compliance-sensitive endorsement claims)
- Internal-only marking visible at top of doc
- Hallucination guard passes

## Status quo battlecard (the biggest competitor)

The largest competitor for most B2B companies isn't a named vendor — it's status quo / do-nothing. 40% of deals lost here. Build a status quo battlecard that addresses:

- The POC graveyard most buyers have
- Internal-build resistance ("we'll do it ourselves")
- Budget cycle defenses ("come back next quarter")
- "Good enough" incumbents
- Risk-aversion framing

The status-quo-tracker script feeds this battlecard with field signals from meeting_log keyword patterns.

## Why Opus

Battlecards are sales enablement. Sales conversations turn on these. Accuracy matters more than cost.
