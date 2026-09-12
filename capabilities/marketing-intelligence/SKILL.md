---
name: revenueos-marketing-intelligence
description: "RevenueOS Marketing Intelligence — match the business's channels against a unified catalogue of hundreds of vendored marketing skills and queue concrete content to produce (SEO briefs, cold email sequences, LinkedIn posts, ad copy, and more). Use when the user says \"what content should we make this week\", \"find a skill for cold email\", \"search the skill catalogue\", \"give me a content plan\", or asks what playbook covers a specific marketing task."
metadata:
  version: 0.1.0
  vendor: RevenueOS
---

# RevenueOS Marketing Intelligence

RevenueOS Marketing Intelligence turns the business's configured channels (SEO, cold email,
LinkedIn, blog, ads, and more) into concrete content to produce, by matching them against a
unified, searchable catalogue of vendored marketing skills. It can also be queried directly
to find the right skill for any marketing task.

## When to use this

- The user wants a content plan or backlog for the week/channel mix already configured.
- The user wants to find a specific skill (e.g. "which skill handles cold email subject
  lines") before approving or running an action.
- The user wants a content deliverable actually produced (a brief, a post draft, ad copy)
  from one of the matched skills.

## How to run it

CLI:

```bash
revenueos run content                    # queue content_opportunity actions for the week
revenueos skills search "cold email"     # search the unified catalogue directly
revenueos skills show <source>/<slug>    # read one skill's full SKILL.md
revenueos today                          # review queued content opportunities
```

MCP tool:

- `revenueos_run("content")` — matches channels to skills and queues one
  `content_opportunity` action per pick for the current week.
- `revenueos_search_skills(query: str, limit: int = 10)` — free-text search over every
  vendored skill (SEO, ads, cold email, growth, operations, and more); returns source, slug,
  description, and whether the skill ships runnable code.
- `revenueos_today()` — see what's queued; `revenueos_approve` / `revenueos_execute` to
  produce a deliverable for one of them.

## Inputs

No file upload. Uses `channels` from the workspace's `revenueos.yaml` (set during
`revenueos init`) to pick relevant skills; `revenueos_search_skills` takes a free-text query
directly.

## What it produces

One `content_opportunity` action per matched skill per week (deduplicated so re-running the
same week is safe), each naming the channel and the exact skill it would run. Executing one
runs that skill's SKILL.md against the business's context (offer, audience, tone,
corrections) and writes the finished Markdown deliverable to `data/outputs/`.

## Approval rule

Matching and searching are read-only. A deliverable is only produced after a human calls
`revenueos approve <id>` then `revenueos execute <id>` (or the MCP equivalents) on a specific
`content_opportunity` action — and even then, the output lands in `data/outputs/` for review;
RevenueOS Community never auto-publishes anything to a live channel.
