---
name: pmm-positioning
trigger: manual ("positioning audit", "positioning refresh") + auto-trigger when ≥3 messaging violations land in one week
load-context: identity, communication-style, domain-knowledge, goals-and-priorities, decision-log
sub-skills: hallucination-guard, messaging-rules-check
ends-with: Master messaging doc updated (draft form) + audit report written to draft table (kind=exec_memo)
model: claude-opus-4-7-[1m]
---

# pmm-positioning

Product Marketing positioning skill. Runs April Dunford's 5-step method against your current state and produces either an audit (what's working, what's drifting) or a positioning refresh draft (proposed updates to your locked decisions).

This skill exists because PMM hires are often gating. The AI CMO bridges the gap. When the human PMM lands, this becomes their tool, not a replacement.

## When to invoke

- Quarterly positioning audit (human types "positioning audit")
- Pre-fundraise positioning check (human types "positioning refresh")
- When competitive landscape shifts materially
- Auto-trigger: when 3+ messaging rule violations land in a single week

## Inputs

| Input | Source |
|---|---|
| Current locked messaging | Your master messaging doc |
| Recent external content | draft rows last 90 days |
| Customer language | meeting_log last 60 days for verbatim buyer quotes |
| Competitive set | competitive_intel last 90 days |
| Pipeline data | metric_snapshots for what's converting vs. stalling |
| Validation flag history | audit_log where action=validation_flag_raised |

## Process

### Step 1: Competitive Alternatives (always include "do nothing")

For each ICP segment:

- What would the buyer do if your product didn't exist?
- Status quo (do nothing) is always option 1.
- Named competitors.
- Build vs. buy options (internal builds, alternative architectures).

### Step 2: Unique Differentiators

For each differentiator, validate it's still true and unique:

- Is it still differentiated against named competitors?
- Is it still differentiated against status quo?
- Has it weakened (e.g., a competitor shipped matching capability)?

### Step 3: Value Translation ("so what?")

For each differentiator, translate to business outcome:

- Revenue (closed deals attributable to this feature)
- Cost (reduced operations spend)
- Risk (compliance audit, regulatory posture)
- Speed (time-to-production metrics)

If a differentiator can't be value-translated, it's a positioning weakness.

### Step 4: Who Cares Most (ICP Tier 1)

Run 10-point ICP scoring against active prospect list. Identify the segment where value is disproportionately important.

Validate from meeting_log: which prospects are leaning in? Their characteristics define ICP Tier 1.

### Step 5: Market Category

Your current category strategy. Validate.

Alternatives to consider only if Steps 1 to 4 reveal weakening:

- Head-to-Head (better than incumbent)
- Big Fish / Small Pond (dominate a niche)
- Create New Category (high risk, high education cost)

## Output (audit mode)

```markdown
# Positioning Audit — [Quarter]

## TL;DR

[2 sentences on whether positioning is holding, drifting, or needs refresh.]

## Step 1: Competitive Alternatives

[What buyers do if you don't exist. Status quo + named competitors. Any new alternative.]

## Step 2: Differentiators

| Differentiator | Still Unique? | Threat |
|---|---|---|
| [Differentiator 1] | Yes / Weakening / No | [What's threatening] |
| [Differentiator 2] | ... | ... |
| [Differentiator 3] | ... | ... |

## Step 3: Value Translation

[Outcome data from customer conversations. Verbatim quotes from meeting_log. Flag any differentiator without a value translation.]

## Step 4: ICP Validation

[Who is leaning in hardest. Who is bouncing off. Is the ICP Tier 1 still right?]

## Step 5: Category

[Current category status. Recommendation: stay or shift, with rationale.]

## Recommended Master Messaging Updates

[If any. Each proposed change references the current locked decision and proposes the new version.]

## Open Decisions for Founder

[Anything in the audit that requires founder call. Auto-routes to next decision memo.]
```

## Output (refresh mode)

When invoked as "positioning refresh," produce a redlined version of your master messaging doc with proposed changes annotated. Human + founder review and approve before the master doc updates.

## Hard gates

- No proposed change without rationale grounded in customer data, competitive data, or pipeline data
- Stage discipline: don't propose moves that imply PMF Level 3+ if you're at Level 1 to 2
- Active positioning frame protected (don't propose off-narrative shifts without explicit founder direction)
- Compliance-sensitive entity references run through disclosure gate
- Hallucination guard on all customer quotes (verbatim only)

## Failure modes

| Failure | Recovery |
|---|---|
| Insufficient customer data (<5 meetings in last 60 days mentioning positioning) | Run audit but flag confidence as low. Recommend discovery calls. |
| Differentiator weakening but no clear replacement | Flag as open question for founder. Don't force a new framing. |
| Locked messaging has drifted in recent content | Auto-fix reversible content. Surface to human for published content. |
