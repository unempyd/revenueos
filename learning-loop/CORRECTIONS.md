# CORRECTIONS.md — The Async Correction Loop

Every time you redirect the AI CMO (in Slack, email reply, or editor session), the correction lands here. The agent loads this file at every session start and at the top of every cron prompt. Over time, this file IS the moat.

Pattern source: Sabrina Ramonov's two-layer memory model (immutable playbook plus mutable corrections) and Tiago Forte's Personal Context Management thesis.

---

## 2026-09-14 — copy assembled from the ledger reads as machine-written, and the audience says so
**Context:** the Hacker News submission (news.ycombinator.com/item?id=49683832, 2026-09-13 13:33 UTC) reached 6 points and 5 comments. Two of the five called the copy "AI slop" — one unprompted, one replying directly to our first comment asking for "a human to write your blurb". A genuine question from `graemep` (why the name ends in OS) was answered with a long structured comment, and that reply is now `[flagged]`. The copy had been produced by RevenueOS's own skills from its own ledger, which is exactly why it read that way: assembled from records rather than written by someone with something to say.
**Correction:** public copy is not a ledger rendering. If a paragraph could have been assembled from stored facts, it is not ready to post. Lead with the single most falsifiable sentence; state what is not proven before anyone asks; keep a launch comment under 100 words; never answer a one-line question with a wall of structure — match the length and register of what was asked. A comment that lists capabilities is worse than one that admits a limit.
**Apply when:** drafting any public post, comment, reply, launch copy, directory description or README paragraph — every surface a stranger reads
**Source:** hacker news thread 49683832

## Format

Each correction is its own dated block. Append new corrections to the TOP. Never delete. Historical corrections explain why rules exist.

```markdown
## YYYY-MM-DD — short title
**Context:** What the AI did wrong / what you redirected
**Correction:** The new rule
**Apply when:** Trigger conditions for future sessions
**Source:** Slack DM / email reply / editor session
```

---

## How this file gets used

Every cron prompt includes (filtered for relevance):

```
<corrections>
{last 30 days of relevant corrections, filtered by topic}
</corrections>
```

Every editor session starts with:

```
Loading CORRECTIONS.md... {N} corrections, most recent {date}.
```

When you correct an output:
1. The correction is captured (Slack thread reply, email reply via Resend inbound webhook, or inline comment in editor)
2. It's classified by topic (voice / messaging / process / integration)
3. Appended to the TOP of this file
4. Acknowledged in next session: "Loaded new correction: {title}."

---

## What NOT to put here

- One-off task statuses (use TodoWrite or Notion)
- Project state (use SESSION-CONTEXT.md or current-projects.md)
- Static company facts (use `company-context/` files)
- Personal preferences unrelated to AI CMO outputs

This file is only about how the AI CMO should behave differently going forward.

---

## Seed corrections (start here)

Add these on Day 1 so the agent has a starting calibration. Customize for your company.

### YYYY-MM-DD — Voice — never restate the close

**Context:** AI drafts often end with a sentence restating what was just said.
**Correction:** Do not restate the close. Two sentences saying the same thing at the end is a refrain-style AI tell.
**Apply when:** Any long-form content.
**Source:** Initial calibration.

### YYYY-MM-DD — Decision memos bundle 4+ items

**Context:** Single decisions to founders / execs often stall. Bundled memos with multiple decisions get faster responses.
**Correction:** When 4 or more decisions are open and awaiting an exec for >5 days, build a consolidated decision memo. Never send 4 separate Slack DMs.
**Apply when:** decision-memo-builder auto-trigger logic.
**Source:** Initial calibration.

### YYYY-MM-DD — Always cite sources for founder content

**Context:** Founder content needs to be source-grounded. Cited transcript line, blog quote, or podcast moment.
**Correction:** Every founder content draft includes verbatim source quote. No invented attribution.
**Apply when:** Any founder LinkedIn or blog draft.
**Source:** Initial calibration.

### YYYY-MM-DD — Hard punctuation rules

**Context:** Em dashes and semicolons are AI tells.
**Correction:** No em dashes (use parentheses, commas, or rewrite). No semicolons (use commas, periods, or parentheses).
**Apply when:** All content drafts.
**Source:** Initial calibration.

### YYYY-MM-DD — No bold-prose-list pattern

**Context:** The "**Bold term:** explanation sentence" prose list format is the most recognizable AI writing pattern.
**Correction:** Bold is for labels only (section headers, scenario labels). Never use bold for emphasis inside running prose.
**Apply when:** All content drafts.
**Source:** Initial calibration.

---

*Customize these seed corrections for your company. Add new ones as you build.*
