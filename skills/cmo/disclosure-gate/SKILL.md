---
name: disclosure-gate
trigger: sub-skill, called by every content skill
load-context: identity, team-and-relationships, preferences-and-constraints
inputs: Draft text + voice + intended publish surface
sub-skills: none (leaf skill)
ends-with: Pass | Fail boolean + suggested disclosure language if Fail
model: claude-haiku-4-5
---

# disclosure-gate

Enforces compliance and disclosure rules from your company-context. This is a load-bearing gate. Getting it wrong creates legal/compliance exposure. The skill is deliberately conservative: when in doubt, require disclosure.

## The constraint

Your company likely has compliance constraints that affect external content. Common categories:

- **Investor / partner indirect financial interest.** If a partner or customer is also an investor (directly or via affiliated entity), public endorsements require disclosure.
- **Customer NDAs.** Some customers don't permit naming.
- **Regulated industry constraints.** Healthcare, financial services, education, government, defense.
- **Pricing transparency restrictions.** Public pricing claims during certain periods.

Document the specific entities and the required disclosure language in `company-context/preferences-and-constraints.md` and `company-context/team-and-relationships.md`. This skill reads them.

## Trigger entities

Scan draft text for any mention of:

- Your investors (named, including any affiliated entities)
- Your customers in the "named-but-undisclosed" category
- Specific executives at investor/customer entities (named individuals)
- Any compliance-sensitive language patterns specific to your industry

(You define this list in your team-and-relationships.md file.)

## Pass / fail logic by surface

| Surface | If sensitive entity mentioned | Verdict |
|---|---|---|
| Internal Slack | Always OK | PASS |
| Internal email to VP / ops lead | Always OK | PASS |
| Founder LinkedIn (public) | Quote/endorsement: require disclosure. Mention without endorsement: REVIEW | PASS w/ disclosure required \| REVIEW |
| Company LinkedIn | Quote/endorsement: require disclosure. Logo use: require disclosure. Name-drop only: REVIEW | PASS w/ disclosure \| REVIEW |
| Website testimonial | Always require disclosure | PASS w/ disclosure \| FAIL if not present |
| Press release | Always require disclosure OR escalate to legal/compliance | FAIL until cleared |
| Closed-door event invitation | OK (allowed category for most disclosure frameworks) | PASS |
| Pitch deck (private) | OK with disclosure language in deck footer | PASS |
| Sales reference call (1:1) | OK | PASS |

## Endorsement vs mention classification

Endorsement triggers stricter rules. The skill must classify which it is.

**Endorsement:**

- Direct quote from an investor/partner/customer employee about your product/value
- Testimonial language ("[Company] is helping us...", "we use [Your Co] to...")
- Logo placement on website/deck implying customer status
- Use of the entity's name in a way that implies they vouch for you

**Mention (no endorsement):**

- Cited as an event co-host or panel attendee (factual)
- Listed as one example of "[industry]" or "[stage]" generically
- Historical context ("Our CEO worked at [Company] before founding us")

## Required disclosure language

When disclosure required, insert appropriate format for the surface:

**Long form (blog, press release, pitch deck footer):**

> *[Entity] has [direct/indirect] financial interest in [Your Company] through [relationship]. This relationship has been disclosed in accordance with [Entity]'s compliance and corporate communications policies.*

**Short form (LinkedIn post, social):**

> *Disclosure: [Entity] holds [direct/indirect] financial interest in [Your Company].*

**Footnote/inline:**

> ¹ [Entity] holds [direct/indirect] financial interest in [Your Company]; disclosed per their compliance policy.

Customize per your specific compliance requirements. Get the exact language pre-cleared by the entity's compliance team if applicable.

## Escalation path

When the skill returns REVIEW or FAIL:

1. Block the draft from auto-publish
2. Set draft.disclosure_gate_passed = false
3. Write audit_log entry with offending span quoted
4. Slack-DM the VP: "Disclosure review needed on draft {id}. Reason: {classification}. Suggested action: {add disclosure | escalate to legal/compliance | rewrite to avoid mention}."
5. If VP doesn't respond in 24h on critical timeline: escalate to ops lead

## Examples

**Pass:**

> "At our event this week, our CTO moderated a panel with [Investor] partner [Name] and [other panelist]. The conversation focused on..."
>
> → PASS (factual mention of co-panelists, no endorsement, no claim about [Investor]'s use of your product)

**Pass with disclosure required:**

> "Working with [Investor] has shown us how seriously enterprise teams treat..."
>
> → PASS with disclosure required (endorsement-shaped language) — insert short-form disclosure

**Fail until cleared:**

> "[Investor] says [Your Product] is the best platform in the market."
>
> → FAIL (direct endorsement quote, blocks publish until compliance cleared)

## Why conservative

False positives cost a 5-minute Slack to the VP. False negatives cost the relationship. Erring on the side of "require disclosure" protects the relationship.
