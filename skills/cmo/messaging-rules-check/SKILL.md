---
name: messaging-rules-check
trigger: sub-skill, runs on every external content output
load-context: communication-style, preferences-and-constraints
inputs: Draft text + intended voice + locked messaging decisions
sub-skills: none
ends-with: Pass | Fail + violation log written to messaging_violation table if Fail
model: deterministic regex + claude-haiku-4-5 for semantic checks
---

# messaging-rules-check

Encodes your full writing style guide PLUS your locked messaging decisions as deterministic rules. Every external content output passes through this gate.

## Hierarchy

When rules conflict, follow the more specific rule.

1. Founder voice (most specific) > Company voice
2. Locked messaging decisions > Writing style guide > humanizer rules
3. Hard punctuation rules (no em dash, no semicolon) always apply

## Locked Company Terminology

[Customize per your company. Examples below from `company-context/communication-style.md`.]

### Must use

- [Your preferred terminology, e.g., "specialized AI workers" instead of "agents" for external use]
- [Your product component names — capitalized, exact spellings]
- [Your preferred verbs]

### Must NOT use

- [Banned company-specific terms]
- [Wrong buyer mentions]
- [Off-brand language]

### Locked numerical facts

- [Your ACV range]
- [Your current ARR]
- [Your customer count]
- [Your accuracy/performance claims]
- [Your founding facts]

### Three differentiators (only these three)

[List the three. Don't list four. Accuracy or another claim is woven through, not its own pillar.]

### Verticals (only these for external content)

[List 3 to 5 verticals. Sub-verticals OK in use-case materials only.]

## Writing Style Guide — Banned Words

The skill regex-scans for these case-insensitive. Auto-fail on any hit. Customize per your voice.

A common B2B marketing voice guide banned word list (most companies should ban most of these):

may, just, very, really, literally, actually, certainly, probably, basically, maybe, delve, embark, enlightening, esteemed, shed light, craft, crafting, imagine, realm, game-changer, unlock, skyrocket, abyss, revolutionize, disruptive, utilize, utilizing, dive deep, tapestry, illuminate, unveil, pivotal, intricate, elucidate, hence, furthermore, harness, exciting, groundbreaking, cutting-edge, remarkable, remains to be seen, glimpse into, navigating, landscape, stark, testament, in summary, in conclusion, moreover, boost, skyrocketing, opened up, powerful, inquiries, ever-evolving, underscore, bolster, foster, leverage, unpack, pave the way, transformative, game-changing, robust, comprehensive, seamless, nuanced (as empty praise), vibrant, multifaceted, holistic

## Banned Phrases

- "In today's fast-paced / rapidly evolving / digital world"
- "It's important / worth noting that"
- "One of the most important / significant / crucial"
- "When it comes to" / "At its core" / "At the end of the day"
- "This is where X comes in" / "Let's break it down" / "Let's get into it"
- "Plays a crucial role in" / "It cannot be overstated"
- "Underscoring the importance of"
- "Highlighting the need for"
- "Reflecting a broader trend toward"
- "Marking a significant shift in"
- "Emphasizing the significance of"
- "Reflecting the continued relevance of"
- "Here's the thing:" / "Here's the problem:" / "Here's what changed:"

## Banned Structures

- "It's not just X, it's Y"
- "Not only X, but Y"
- "This isn't about X. It's about Y."
- "No X. No Y. Just Z."
- "Here's the [noun]:" as a framing device

## Hard Punctuation Rules

- **No em dashes.** Includes unicode em dash, double-hyphen substitute, any variation. Replace with parentheses, commas, or rewrite.
- **No semicolons.** Use commas, periods, or parentheses.

## Formatting Rules

- No hashtags.
- No emoji unless explicitly requested.
- No bold for emphasis in running prose. Bold for labels only.
- Never the "**Bold term:** explanation sentence" list format in prose. Most recognizable AI pattern.
- No nested bullets.
- No headers below H2 in newsletter or prose content.

## Process

### Stage 1: Regex scans (deterministic)

1. Banned word scan (case-insensitive)
2. Banned phrase scan
3. Banned structure scan
4. Em dash detection (unicode `—` plus double-hyphen `--`)
5. Semicolon detection
6. Bold-prose-list pattern detection (`**Word:**` followed by sentence)
7. Locked terminology violation scan
8. Numerical fact violation (claims wrong ACV, wrong customer count, etc.)
9. Differentiator overshoot (more than 3 listed)
10. Vertical violation (non-approved verticals named externally)

### Stage 2: Semantic checks (claude-haiku)

For nuanced cases:

- Buyer mismatch (semantically positions wrong buyer)
- Headcount replacement framing without banned words
- Endorsement-shaped language without disclosure
- Voice fingerprint mismatch (CEO draft sounds like CTO, vice versa)

### Stage 3: Aggregate verdict

If any deterministic check fails OR semantic check returns high-confidence violation:

- verdict: FAIL
- blocks_publish: true
- Write to messaging_violation table
- Return structured violations with suggested fixes

Otherwise:

- verdict: PASS
- blocks_publish: false

## Regeneration loop

If verdict=FAIL:

1. Regenerate the section containing the violation with stricter prompt: "Previous draft contained these violations: [list]. Fix and avoid."
2. Re-check.
3. If second attempt fails: write all violations to messaging_violation table, flag draft for human review, do NOT ship.

## Output

```json
{
  "verdict": "PASS" | "FAIL",
  "violations": [
    {
      "rule": "banned_term:leverage",
      "offending_span": "We leverage [Your Product] to drive efficiency",
      "suggested_fix": "Replace 'leverage' with 'use' or rewrite: 'We use [Your Product] to execute high-judgment workflows.'",
      "severity": "blocking",
      "category": "voice"
    },
    {
      "rule": "em_dash_detected",
      "offending_span": "The locked decisions — done in April — apply",
      "suggested_fix": "Replace em dashes with parentheses: 'The locked decisions (done in April) apply.'",
      "severity": "blocking",
      "category": "punctuation"
    },
    {
      "rule": "bold_prose_list",
      "offending_span": "**Differentiator 1:** Compliance is core...",
      "suggested_fix": "Convert to prose paragraph or remove bold from prose entry. Bold is for labels only.",
      "severity": "blocking",
      "category": "formatting"
    }
  ],
  "regeneration_required": true,
  "blocks_publish": true
}
```

## Why this exists

Your locked messaging decisions took weeks or months to lock down. Your writing style guide took years of practice and feedback to refine. Without enforcement, the AI CMO drifts back to generic AI marketing language inside two weeks. This skill is the structural counterpart of the human discipline that produced the decisions and the style guide.

## Maintenance

When you update your writing style guide or your locked messaging decisions:

1. Update `company-context/communication-style.md`
2. Update this skill's banned word/phrase/structure lists
3. Log the update in CORRECTIONS.md
4. Run `scripts/style-guide-audit.ts` against recent drafts to retroactively flag violations
