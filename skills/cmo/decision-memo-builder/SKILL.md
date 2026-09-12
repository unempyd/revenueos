---
name: decision-memo-builder
trigger: manual ("decision memo") + auto-trigger when ≥4 items have `awaiting_founder = true` for >5 days
load-context: identity, current-projects, decision-log, team-and-relationships, goals-and-priorities
inputs: All open items awaiting founder decisions + meeting context + VP's recommendation on each
sub-skills: hallucination-guard
ends-with: Memo written to draft table (kind=exec_memo) + Slack-ready format + recommended send window
model: claude-opus-4-7-[1m] (executive-grade output, founder reads, decision quality matters)
---

# decision-memo-builder

Founders are hard to reach. Decisions stall. The proven antidote: bundle multiple stalled decisions into ONE consolidated memo with the VP's recommendation on each, hard ask for response by a specific date.

This skill builds that memo on command.

## When to invoke

- **Manual:** Human types "build decision memo" or "draft decision memo for [founder]"
- **Auto-trigger:** when ≥4 items in notion_task or audit_log have been flagged `awaiting_founder = true` for >5 business days. Slack-DM: "Suggesting a consolidated decision memo. {N} decisions stalled. Build?"

## Inputs

| Input | Source |
|---|---|
| Open decisions | notion_task where status in (new, in_triage) AND owner=[founder] AND age>5d |
| Carryover items | Your session context or notes — parse for "awaiting founder" tags |
| Validation flags | audit_log where action=validation_flag_raised AND resolved=false |
| Meeting context | meeting_log last 14 days — decisions discussed but not resolved |
| VP positions | VP must provide recommendation per item; if missing, skill asks for it |

## Process

1. **Pull all open items** awaiting the founder — dedupe across sources.
2. **Categorize into 3 buckets:**
   - **Foundational** (strategic decisions, hires, narrative)
   - **Execution** (events, content, partnerships)
   - **Small-but-Blocking** (one-pagers, calendar holds, intros)
3. **For each decision, include:**
   - Decision needed (one sentence)
   - VP's recommendation (lead with "Here's what I'd do")
   - What changes if founder picks differently
   - Blocker if undecided (what stalls without this)
   - Checkbox response format ("☐ Approve | ☐ Modify (specify) | ☐ Park (revisit by X date)")
4. **Set hard ask:** response by {Friday EOD this week or next}
5. **Estimate read time** (~6 min target) and decision time (~25 min for full memo)
6. **Run hallucination-guard.** Every number, date, dollar figure verified.
7. **Generate Slack DM version** (truncated, link to full Notion doc) AND full Notion doc version.
8. **Write to draft table** kind=exec_memo.
9. **Slack-ping the VP:** "Decision memo ready. {N} decisions, {X} foundational, {Y} execution. Estimated founder read: 6 min. Suggested send window: tomorrow 9am ET."

## Output format (Notion doc body)

```markdown
# Decision Memo — [Founder Name] — {Date}

**TL;DR:** {N} decisions need your call by {Hard deadline}. Read time ~6 min. Decision time ~25 min.

## How to respond

Reply in Slack with the decision number + your call. Or open this doc and check boxes inline. Either works.

## Why these are bundled

These have all been pending {avg X days}. Several are blocking downstream work. I've grouped them so you can knock them out in one focused 25-min session rather than five separate Slack threads.

---

## 🏗 Foundational (impacts everything downstream)

### 1. {Decision title}
**Decision needed:** {One sentence}

**Here's what I'd do:** {VP's recommendation, opinionated}

**Why:** {2 to 3 sentence rationale}

**If you pick differently:** {What changes downstream}

**If you don't decide:** {What stalls}

☐ Approve as written
☐ Modify (specify): _______________
☐ Park until {date}

---

### 2. {next decision}

---

## ⚙️ Execution

### 3. {decision}

---

## 🔑 Small-but-Blocking

### 6. {decision}

---

## The hard ask

Response by **{Day, Date} EOD**. Slack DM works. Default to my recommendation if you don't have a strong opinion. I'll proceed and flag if anything turns out differently than expected.

Validation flags re-surfaced from prior memos:

- {flag 1}
- {flag 2}
```

## Hard gates

- Every decision includes the VP's explicit recommendation (no neutral memos)
- Hard deadline is set and ≤7 days out
- Read time estimate is in the TL;DR
- Categorized into 3 buckets
- Hallucination guard passes
- Disclosure gate passes (if relevant entities mentioned)

## When NOT to build a decision memo

- Fewer than 3 stalled decisions: individual Slack DMs are better.
- VP's recommendation is unclear on any item: skill asks the VP to take a position first.
- Urgent single decision (e.g., "should we cancel the event Friday"): don't bundle. Single urgent Slack.

## Slack DM version

```
Hey [Founder] — decision memo ready when you have ~25 min.

{N} decisions stalled, mostly foundational + execution. I've put my recommendations on each. Hard ask: response by {Day} EOD so I can keep things moving.

Link: {Notion URL}

Happy to walk through any of them on a call if it's faster.
```

## Why Opus

Founders read every word. The memo's job is to compress 25 minutes of their time into the smallest possible cognitive load. Opus-grade compression and judgment is worth the cost.
