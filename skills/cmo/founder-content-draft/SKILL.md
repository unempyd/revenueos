---
name: founder-content-draft
trigger: cron, 05:00 Mon + Wed (CEO), 05:00 Wed (CTO)
load-context: identity, communication-style, domain-knowledge
inputs: Content pillar selector, source corpus, voice guide, locked messaging decisions, CORRECTIONS.md
sub-skills: disclosure-gate, messaging-rules-check, hallucination-guard, humanizer-pass
ends-with: Draft saved to draft table + Slack ping for review + Thursday review window scheduled
model: claude-opus-4-7-[1m] (founder voice quality matters; this is investor + market-facing brand)
---

# founder-content-draft

Drafts founder LinkedIn posts. The Founder TL Engine lives or dies on cadence. The agent's job: keep the pipeline full so the founder review window is the bottleneck, not the drafting.

## When to invoke

| Schedule | Voice | Pillar focus |
|---|---|---|
| Mon 05:00 | CEO | Industry POV |
| Wed 05:00 | CEO | Category Creation or Builder's Perspective |
| Wed 05:00 | CTO | Builder's Perspective (technical credibility) |

If a founder's previous week draft is still unpublished, skip and Slack-ping: "Queue at 2 drafts. Should I draft more or hold?"

## Inputs

| Input | Source |
|---|---|
| Content pillar selection | Algorithm: rotate by pillar, weighted by what's underrepresented in last 4 weeks |
| Source corpus | meeting_log (last 30 days), blog folder, podcast transcripts, panel quotes |
| Voice guide | company-context/communication-style.md |
| Locked messaging | The locked positioning decisions in your master messaging doc |
| Recent corrections | CORRECTIONS.md filtered for kind=linkedin |
| Recent published posts | draft where status=sent AND voice=[founder] (last 8 weeks) |
| Compliance context | Any relevant disclosure flags |

## Process

1. **Pillar selection.** Run the rotation algorithm. Output: pillar + 2 to 3 angle candidates from the source corpus.
2. **Source grounding.** Every post MUST cite a source. If no source available, generate a "source-needed" stub for the human, don't ship.
3. **Draft hook.** 3 variants per CEO post, 2 per CTO post. Each follows voice patterns.
4. **Draft body.** 150 to 250 words, pillar-appropriate, with one concrete vignette.
5. **Draft CTA.** Only on educational/framework posts. None on rant/accountability.
6. **Run sub-skills in order:**
   1. `disclosure-gate` — compliance check
   2. `messaging-rules-check` — banned terms, locked terminology
   3. `hallucination-guard` — every number/claim verified
   4. `humanizer-pass` — AI tells removed
7. **If any gate fails:** regenerate that section ONCE. If second attempt fails, flag draft, do not ship.
8. **Save to draft table.** kind=linkedin_[voice], source_grounded=true, all gate flags set.
9. **Slack ping:** "Thursday review queue: 1 new [voice] draft ready. Link: {dashboard URL}"
10. **Audit log:** every action with full payload.

## Hard gates

- Source-grounded (cite the meeting line, blog, podcast)
- Disclosure gate passes
- Messaging rules pass
- Hallucination guard passes
- Humanizer pass passes
- Voice fingerprint check passes (each voice has at least one signature element present)
- Word count 150 to 250

## Output format (draft.payload jsonb)

```json
{
  "voice": "ceo | cto | company",
  "pillar": "industry_pov | builders_perspective | category_creation",
  "primary_hook": "string",
  "alt_hooks": ["alt1", "alt2"],
  "body": "string (150 to 250 words)",
  "cta": "string | null",
  "source_refs": [
    {"type": "meeting", "id": "meeting_log_uuid", "line": "verbatim quote"},
    {"type": "blog", "url": "...", "section": "..."}
  ],
  "gates": {
    "hallucination_guard": true,
    "disclosure_gate": true,
    "messaging_rules": true,
    "humanizer": true,
    "voice_fingerprint": true
  },
  "voice_fingerprint_evidence": "string",
  "recommended_publish_window": "Thursday {date} 9 to 11am ET",
  "amplification_plan": "list of first-30-min commenters",
  "graphics_needed": "boolean + spec if true"
}
```

## Founder approval workflow

1. Thursday morning: dashboard shows queued drafts
2. VP of Marketing or ops lead reviews via dashboard or Notion mirror
3. Founder approves in Slack DM with a 👍 emoji (auto-detected, status=approved)
4. OR founder responds with redlines, status=needs_revision, AI CMO regenerates Friday morning
5. Once approved: manual publish (NEVER autonomous). Founder posts from their own LinkedIn.
6. After publish: human marks status=sent in dashboard. audit_log records publish timestamp.

## Failure modes

| Failure | Recovery |
|---|---|
| No source available | Don't fabricate. Generate "source-needed" stub flagging the human: "Need transcript/blog for [topic]. Source from podcast?" |
| Cadence imbalance (3 same-pillar posts in row) | Force pillar rotation. Suggest a different pillar even if human hasn't fed a source. |
| Founder hasn't approved last 2 drafts | Don't ship a third. Slack-DM "queue at 2, holding until you clear." Founder bandwidth is a hard constraint. |
| Voice fingerprint absent | Regenerate with explicit fingerprint enforcement in prompt. |

## Why Opus

Founder voice is the brand. Founders read every post. Investors see these during diligence periods. The marginal cost of Opus vs Haiku (~$0.50 to $1 per draft) is rounding error compared to one published post that sounds AI-written.
