---
name: hallucination-guard
trigger: sub-skill, runs on every AI output containing numbers
load-context: identity, preferences-and-constraints
inputs: Draft text + ground-truth values block + tolerance
sub-skills: none
ends-with: Pass | Fail boolean + diff report
model: claude-haiku-4-5 + deterministic regex validation
---

# hallucination-guard

The single most important defensive skill. Exec memos, board decks, decision memos, and founder posts depend on this. ONE wrong number in a client-facing artifact erodes more trust than ten right ones rebuild.

Pattern source: SaaStr 10K's "bad context equals bad output" lesson. Explicit ground-truth grounding is the difference between trusted output and slop.

## When to invoke

Every AI output that mentions:

- Dollar amounts ($, USD, K, M, B)
- Percentages
- Counts (X clients, Y prospects)
- Dates and timeframes
- Founder names + titles
- Company names + facts (your customers, your competitors)
- Specific quotes attributed to anyone

## The pattern (two-step)

### Step 1: Prompt-time grounding (preventive)

Every content prompt must explicitly list ground-truth values:

```
GROUND-TRUTH VALUES (use ONLY these numbers):

ARR position: $[current] current, $[base goal] base goal, $[stretch goal] stretch goal
Clients (named only internally): [list]
Customer cap (concentration risk): $[cap]
ACV range: [min] to [max]
Pipeline (primary): {current from metric_snapshots}
Pipeline (total): {current from metric_snapshots}
Days to next event: {value}
Next event: {name, date}

DO NOT invent or extrapolate. If the data you need is not in the list above,
write [DATA NEEDED] inline. The human will fill in or remove. Never guess.
```

### Step 2: Post-generation validation (detective)

After the AI returns the draft:

1. **Regex-extract every numeric substring.** Patterns:
   - `\$[\d,]+(\.\d+)?[KMB]?` (dollar amounts)
   - `\d+(\.\d+)?%` (percentages)
   - `\d+ (clients?|customers?|logos?|prospects?|accounts?|partners?)` (counts)
   - `(Q[1-4] |H[12] |20\d\d|January|February|...)` (dates)
   - Quoted strings attributed to a person

2. **For each extraction, classify as:**
   - **Direct match** to ground-truth value (PASS)
   - **Within tolerance** (±2% for dollars, ±1 for counts) (PASS with note)
   - **Recognized safe pattern** (year, generic %, benchmark with cited source) (PASS)
   - **Unverified**: no match in ground-truth, no safe pattern → **FAIL**

3. **For each FAIL:**
   - Quote the offending span
   - Show what ground truth says
   - Show the AI's claim
   - Suggested fix

4. **Return verdict:**
   - `pass: true` — all extractions matched or safe
   - `pass: false` — at least one unverified claim. Return diff report.

5. **On FAIL:**
   - Regenerate ONCE with stricter prompt: "USE ONLY these numbers: [list]. The previous draft contained these errors: [list]."
   - If second attempt also fails: block the draft, write detailed log to audit_log, flag for human review.

## What counts as a "safe pattern"

False positives the extractor will catch. Pass without ground-truth lookup:

- **Years** (1995 to current year): historical references
- **Generic percentages** in benchmarks ("CIOs typically reject 70% of POCs") only if a cited public source accompanies
- **Round numbers in framing** ("3 marquee logos") only if they match ground truth
- **Time horizons** ("in the next 5 years," "over the past decade") — qualitative

## Ground-truth values that NEVER change without manual update

Stored in `companies.voice_overrides.ground_truth_lock`:

```json
{
  "static": {
    "company_name": "[Your Company]",
    "founder_ceo_full": "[CEO Full Name]",
    "founder_ceo_role": "CEO and Co-founder",
    "founder_cto_full": "[CTO Full Name]",
    "founder_cto_role": "CTO and Co-founder",
    "founding_year": [year],
    "public_launch_date": "[date or null]",
    "products": ["[product 1]", "[product 2]", "[product 3]"],
    "verticals": ["[vertical 1]", "[vertical 2]", "[vertical 3]"],
    "preferred_buyer_terms": ["[buyer 1]", "[buyer 2]"],
    "off_limits_buyer_terms": ["[do not say buyer]"],
    "competitive_set": ["[competitor 1]", "[competitor 2]", "status quo"],
    "named_clients_anonymized": ["[anonymized framing for marquee references]"],
    "named_clients_internal_only": ["[real names, never externalized]"]
  },
  "dynamic": {
    "current_arr": "PULLED FROM metric_snapshots",
    "pipeline_primary": "PULLED",
    "pipeline_total": "PULLED",
    "founder_reach_30d": "PULLED",
    "days_to_next_event": "PULLED"
  },
  "anti_facts": {
    "ARR_must_not_say": ["[your stretch goal]", "[your base goal]"],
    "client_count_must_not_say": ["[wrong counts]"],
    "headcount_must_not_say": ["[wrong headcount levels]"]
  }
}
```

The `anti_facts` block catches a class of plausible-sounding hallucinations where the AI says the target as if it were achieved.

## Output format

```json
{
  "verdict": "PASS" | "FAIL",
  "extractions": [
    {
      "type": "dollar_amount",
      "value": "$5M",
      "context": "Current ARR sits at $5M across three marquee logos.",
      "match": "ground_truth_direct",
      "verdict": "PASS"
    },
    {
      "type": "count",
      "value": "5",
      "context": "We have 5 enterprise clients in production.",
      "match": null,
      "verdict": "FAIL",
      "ground_truth_says": "3 (the actual count)",
      "suggested_fix": "Change '5 enterprise clients' to '3 enterprise clients' OR rewrite to remove count."
    }
  ],
  "regeneration_count": 1,
  "fails": 1,
  "blocks_publish": true
}
```

## Internal vs external policy

The daily-ideas email to the VP is internal: failed guard appends a "⚠️ flagged" note and continues. External outputs (anything that touches a non-team person): always BLOCK on failure.

## Why this is the moat

Every other AI CMO out there sends emails with made-up numbers. This skill is what separates "I tried it once and it was sloppy" from "I trust this thing with my client-facing work."

The structural counterpart for AI numerical patterns. The voice guide catches AI words. This catches AI numbers.
