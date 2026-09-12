---
name: pmf-assessment
trigger: weekly cron (Sun 18:00) + on-demand
load-context: identity, goals-and-priorities, domain-knowledge, decision-log
inputs: metric_snapshots (last 90 days), competitive_intel, pmf_assessment history, meeting_log
sub-skills: 4ps-diagnosis
ends-with: New row in pmf_assessment + dashboard card refresh + Slack post if score shifted by 2+ points
model: claude-sonnet-4-6 (analytical task, mid-complexity)
---

# pmf-assessment

5-domain PMF assessment scored 1 to 5 per domain. Auto-computed on Sundays. The differentiator card on the Overview tab. Generic AI CMO dashboards don't have this.

## When to invoke

- Sunday 18:00 (weekly automatic)
- Manual: human types "pmf check"
- After any major event: recomputes immediately

## Inputs

Pull last 90 days of:

- metric_snapshots (trendlines)
- meeting_log (qualitative signal, advocacy)
- outreach (open/reply rates, qualification rates)
- event_pipeline (qualified conversations, conversion)
- draft (content cadence + overrides)
- competitive_intel (recent moves)
- Prior pmf_assessment rows (trend over time)

## The 5 Domains

### Domain 1: PMF Signals (1 to 5)

| Score | Looks like |
|---|---|
| 5 | NRR >120%, referrals >10% of pipeline, unsolicited advocacy in transcripts |
| 4 | Referrals starting, retention strong, expansion in marquee logos |
| 3 | Logo retention >80%, first referrals appearing |
| 2 | Pilot-to-paid >50%, repeatable in 1+ vertical |
| 1 | Handful of engaged customers, founder-sold |

### Domain 2: Positioning & Messaging (1 to 5)

| Score | Looks like |
|---|---|
| 5 | Buyer says back the message verbatim. Hero use case visible in 30 seconds. |
| 4 | Locked messaging deployed across all surfaces. Competitive frame holds. |
| 3 | Core message decided but not consistently applied across surfaces |
| 2 | Multiple competing narratives (website ≠ sales deck ≠ founder posts) |
| 1 | Buyer asks "what do you actually do" in second meeting |

### Domain 3: ICP Definition Quality (1 to 5)

| Score | Looks like |
|---|---|
| 5 | Data-driven ICP. Tier 1 list of 50+ accounts with 8/10 ICP score. Trigger events mapped. |
| 4 | 3-tier ICP with firmographic + situational + behavioral. 10-point scoring in use. |
| 3 | Written ICP with firmographic + some situational. Behavioral missing. |
| 2 | Vague ICP. No scoring. |
| 1 | "Anyone in [industry]." |

### Domain 4: GTM Motion & Channel Fit (1 to 5)

| Score | Looks like |
|---|---|
| 5 | 2+ scalable non-founder channels with positive unit economics |
| 4 | 1 scalable non-founder channel proven, 1 to 2 in test |
| 3 | Founder-led + 1 marketing-sourced lead/quarter |
| 2 | Founder-led only, first marketing experiments running |
| 1 | All pipeline from founder network |

### Domain 5: Marketing Ops & Measurement (1 to 5)

| Score | Looks like |
|---|---|
| 5 | Closed-loop attribution, weekly review, AI-augmented forecasting |
| 4 | CRM clean, all inbound captured, weekly reporting, attribution >80% |
| 3 | CRM in use, manual hygiene, monthly reporting |
| 2 | CRM exists but inconsistent hygiene, ad-hoc reporting |
| 1 | No CRM, no measurement, no reporting |

## Process

1. Pull last 90 days of input data
2. Compute each domain score using the rubrics
3. Check manual_override table — use overrides if set within last 7 days
4. Sum total (5 to 25)
5. Run `4ps-diagnosis` sub-skill with domain scores + recent meeting context. Identify binding constraint (Persona | Problem | Promise | Product).
6. Write to pmf_assessment (append-only)
7. Compare to last week — if total changed by ≥2 points OR binding constraint changed, Slack post
8. Refresh dashboard card

## Output

```
PMF check — {Date}

Total: 12/25 (was 11 last week, +1)
Score interpretation: TARGETED FIXES BEFORE SCALING

Domains:
- PMF Signals:        2/5  →  no change
- Positioning:        3/5  →  +1 (messaging decisions now deployed across more surfaces)
- ICP Quality:        3/5  →  no change
- GTM Motion:         2/5  →  no change
- Marketing Ops:      2/5  →  no change

Binding constraint (4 Ps): PROMISE
  Why: {Specific reason based on recent signal}

What this changes for the week:
  1. {Specific action}
  2. {Specific action}
  3. {Specific action}
```

Score interpretation:

- 5 to 10: Foundational work first — don't invest in demand gen yet
- 11 to 16: Targeted fixes before scaling
- 17 to 21: Ready to scale winning signals
- 22 to 25: Efficiency + channel expansion

## Failure modes

| Failure | Recovery |
|---|---|
| Insufficient historical data (<30 days) | Return "insufficient data" instead of guessing. First reliable run requires 30 days of metric_snapshots. |
| Manual override conflicts with auto-compute by >2 points | Use override, log divergence, flag for human |
| Binding constraint unstable week-to-week | Require 2 consecutive weeks of same diagnosis before changing the dashboard call |

## Why Sunday 18:00

The human does Monday strategy work. The PMF check fires Sunday so they walk into Monday with a fresh assessment. Pattern source: morning routine starts the night before.
