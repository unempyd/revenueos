---
name: granola-to-notion
trigger: cron, nightly 23:00
load-context: identity, current-projects, team-and-relationships, preferences-and-constraints
inputs: Granola meeting transcripts (via MCP) since last sync, existing Notion tasks, meeting_log
sub-skills: transcript-cleanup, disclosure-gate
ends-with: New Notion tasks created, meeting_log appended, Slack ping if compliance-sensitive mentioned externally
model: claude-opus-4-7-[1m] for action item extraction, claude-haiku-4-5 for dedup
---

# granola-to-notion

The "second brain" automation scoped to your company. Pulls last 24h of meetings, runs them through transcript cleanup, extracts action items + decisions + validation flags + pipeline signals, and pushes new tasks into Notion. Dedupes against existing tasks.

## When to invoke

- 23:00 nightly
- Skip if no new meetings since last sync

## Inputs

Granola MCP primary. Pantry MCP fallback. Fathom MCP as alternative if your team uses Fathom instead.

| Input | Source |
|---|---|
| New meetings | `mcp__claude_ai_Granola__list_meetings` since `max(meeting_log.taken_at)` |
| Transcripts | `mcp__claude_ai_Granola__get_meeting_transcript` per meeting |
| Existing tasks | Notion DB query for your task DBs |
| Founder decision queue | Items in notion_task where awaiting_founder=true (for dedup) |

## Process

1. **Pull new meetings** from Granola since last sync timestamp
2. **For each meeting:**
   - Save full transcript to meeting_log (append-only)
   - Run transcript-cleanup silently
   - Run extraction prompt with Opus, returns:
     - Summary (3 to 5 bullets)
     - Action items (with assigned owner via `[NAME] will [VERB]` pattern)
     - Decisions made (with decision-maker)
     - Validation flags (statements needing clearance)
     - Pipeline signals (named accounts, deal stages, competitive mentions)
     - Compliance-sensitive references
3. **Dedup against Notion.** For each action item:
   - Fuzzy match against existing tasks (title similarity + owner)
   - 80%+ similarity: SKIP (log as dedup)
   - 50 to 79%: flag for human review
   - <50%: CREATE new Notion task
4. **For compliance-sensitive references:**
   - Run disclosure-gate to classify (factual mention vs. endorsement-shaped)
   - If endorsement-shaped: write to competitive_intel AND audit_log validation_flag
   - Slack-ping: "Meeting on {date} contained {N} compliance-sensitive references that may need handling if external content."
5. **For pipeline signals:**
   - Extract account names + stage signals
   - Write to outreach notes field if account already tracked
   - Update event_pipeline qualified_conversations if event-related
6. **Update meeting_log** with extracted fields populated
7. **Slack summary:**
   ```
   🌙 Granola sync — {N} meetings processed
   - {meeting 1 title}: Created {X} tasks, {Y} validation flags.
   - {meeting 2 title}: ...
   Total new tasks: {N}. Compliance-sensitive mentions: {N}.
   ```

## Action item extraction prompt

```
You are processing a meeting transcript for an in-house marketing function.

Meeting: {title}
Date: {date}
Attendees: {list}

Transcript (cleaned):
{transcript}

Extract:

## Summary
3 to 5 bullets, each 1 sentence. What was discussed and what was decided.

## Action items
For each:
- Owner (named person who agreed; if "we" or unclear, mark TBD)
- Action (verb-first imperative)
- Due (date if mentioned, "?" if not)
- Source quote (verbatim from transcript)

Look for "[Name] will [verb]" or "[Name] is going to [verb]" patterns. Do NOT
invent action items not explicitly stated.

## Decisions made
- Decision (one sentence)
- Decision-maker (named person)
- Source quote

## Validation flags
Statements that may need clearance (named individuals, $ figures, public claims):
- Statement
- Why this needs validation
- Who needs to clear it

## Pipeline signals
For each mentioned account or deal:
- Account name
- Signal (e.g., "moving to procurement," "POC starting," "competitive bake-off vs X")
- Source quote

## Compliance-sensitive references
- Span quoted from transcript
- Classification (factual mention | partnership discussion | endorsement-shaped)

Use ONLY information explicitly in the transcript. Do NOT fabricate names,
dates, or actions.
```

## Hard gates

- Dedup runs before any Notion write (no duplicates)
- Disclosure gate runs on every compliance-sensitive reference
- Action items always carry source quote
- Tasks scoped with `source = "granola_sync"` for traceability
- Extraction failures don't block the batch (log + skip)

## Failure modes

| Failure | Recovery |
|---|---|
| Granola MCP unauthorized | Fall back to Pantry. If both fail, Slack-ping with fix steps. |
| Notion API rate limit | Exponential backoff, max 5 retries, queue for next sync |
| Transcript extraction returns garbage | Log to audit_log, skip meeting, continue batch |
| Founder calendars not connected | Manual upload via scripts/manual-meeting-upload.ts |

## Why this is high-leverage

Every meeting generates action items, validation flags, and pipeline signals that traditionally live in scattered notes or get lost. This skill captures that work automatically and keeps founder decisions visible to the AI CMO dashboard.

Pattern source: meeting-driven Notion workflow proven across multiple operator setups.
