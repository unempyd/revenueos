---
name: competitive-digest
trigger: cron, Friday 08:00
load-context: identity, domain-knowledge, goals-and-priorities
inputs: Crayon manual export, web monitoring sources, existing competitive_intel
sub-skills: none
ends-with: 3 net-new moves + positioning implications emailed + Slack pin + competitive_intel rows
model: claude-sonnet-4-6
---

# competitive-digest

Surfaces 3 net-new competitive moves per week from your named competitors plus the status-quo "do-nothing" alternative. For each: what's the move + what it means for your positioning.

This serves your category narrative by keeping the competitive frame current. Investors, prospects, and your own team will ask. The digest ensures you have the answer ready every week.

## When to invoke

- Friday 08:00
- Skip if no new moves since last digest (return "quiet week, no new moves")

## Inputs

| Source | How to access | Frequency |
|---|---|---|
| Crayon | Manual CSV export weekly, or API if you have access | Weekly |
| Named competitor websites + blogs + LinkedIn | Web monitoring (Defuddle or similar) | Weekly scrape |
| Industry announcements | News alerts | Weekly scrape |
| Existing competitive_intel | Postgres | Dedup against |
| Founder + sales meeting transcripts | meeting_log last 7 days | "Heard in the field" section |

## Process

1. **Scrape sources.** Defuddle + WebFetch for tracked URLs. Cache responses.
2. **Diff against last week's snapshot.** What's new?
3. **For each new item, classify:**
   - **Messaging change** (homepage, positioning shift)
   - **Pricing move** (new tier, new model)
   - **Feature launch** (new capability)
   - **Exec move** (hire, departure, board change)
   - **Funding event** (raise, valuation, acquisition)
4. **Cross-reference with meeting_log.** Did anyone hear about this in customer/prospect conversations?
5. **Rank by relevance to your active positioning:**
   - HIGH: directly threatens or echoes your positioning
   - MEDIUM: orthogonal move that changes market context
   - LOW: noise
6. **Select TOP 3 net-new moves**
7. **For each, write:**
   - The move (factual, sourced)
   - The implication for your positioning
   - Recommended action (content angle, sales talking point, or "track only")
8. **Write to competitive_intel** (append-only)
9. **Compose digest email + Slack post**
10. **Send via Resend** to the VP, Slack-pin in #ai-cmo

## Output (digest email body)

```markdown
# Competitive digest — week of {Date}

## TL;DR
3 moves this week. {summary line}

---

## 1. {Competitor} — {Move headline}

**What happened:** {2 to 3 sentences, sourced}

**Source:** {URL} (observed {date})

**Implication for your positioning:**
{1 to 2 sentences. Strengthens, weakens, or shifts the comparative frame?}

**Recommended action:**
{Concrete: content angle, sales talking point, or "track only"}

---

## 2. {Competitor} — {Move}

## 3. {Competitor} — {Move}

---

## What customers + prospects mentioned this week
{From meeting_log analysis, verbatim quotes if customers brought up competitors. Critical signal, never invent.}

---

## The status-quo trap (recurring)
{Reminder: 40% of B2B deals are lost to no decision. Quick check on which prospects this week showed status-quo risk based on meeting notes.}

---

*Next digest: Friday {next}. Replies = updates to CORRECTIONS.md.*
```

## Hard gates

- Every move cites a public source (URL)
- "Heard in the field" section uses verbatim quotes from meeting_log
- Recommended actions are stage-appropriate (no Series B tactics)
- Active positioning frame protected (don't slip into off-narrative framing)
- Disclosure gate fires if compliance-sensitive entity mentioned

## Failure modes

| Failure | Recovery |
|---|---|
| Crayon export not uploaded | Email-only mode. VP uploads CSV when possible. Flag missing source. |
| No new moves observed | Send a short "quiet week" digest. Use saved time to flag a forward-looking question. |
| All moves are LOW relevance | Don't force a TOP 3. Send "1-mover digest" with extra context on field signals. |

## Status-quo tracking

The biggest competitor is often "do nothing." The status-quo-tracker script (in `scripts/`) scans meeting_log for patterns like:

- "let's pilot another quarter"
- "we have an internal POC"
- "running our own LLM"
- "not the right time"
- "budget cycle / freeze / review"
- "come back next quarter"

High-signal meetings (weight ≥7 across these patterns) write to competitive_intel as `competitor=status_quo` and feed the digest.

This catches the 40% of deals lost to status quo that named-competitor tracking misses.
