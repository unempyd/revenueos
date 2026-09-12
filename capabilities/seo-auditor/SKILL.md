---
name: revenueos-seo-auditor
description: "RevenueOS SEO Auditor — crawl a connected business's website, diagnose missing titles/descriptions, thin pages, duplicate titles, a missing sitemap, and how it stacks up against competitors on domain authority; queue a prioritised fix for each finding. Use when the user says \"audit our SEO\", \"crawl the site for SEO issues\", \"find technical SEO problems\", \"check meta descriptions\", \"compare our domain authority to competitors\", \"why aren't we ranking\", or asks RevenueOS to re-check whether an SEO fix landed."
metadata:
  version: 0.1.0
  vendor: RevenueOS
---

# RevenueOS SEO Auditor

RevenueOS SEO Auditor crawls the connected business's website, finds concrete on-page and
technical defects, and compares domain authority against its configured competitors. Every
finding is queued as a `seo_opportunity` action pointing at the RevenueOS skill that would
fix it — nothing on the site is ever changed by the audit itself.

## When to use this

- The user asks for an SEO audit, a site crawl, or "what's wrong with our SEO".
- The user wants to know how their domain authority compares to named competitors.
- The user wants to confirm whether a previously executed SEO fix actually took effect.
- A daily/weekly check-in should surface new SEO opportunities for review.

## How to run it

CLI (run inside the workspace, or with `--root <workspace>` / `REVENUEOS_ROOT` set):

```bash
revenueos run seo               # crawl the site, compare authority, queue findings
revenueos today                 # see the seo_opportunity actions this created
revenueos run measure           # after a fix is executed: re-crawl and record what changed
```

MCP tool (same workspace resolution via `REVENUEOS_ROOT` / cwd):

- `revenueos_seo_audit(website?: str)` — runs the audit; pass `website` to check a different
  URL for this one run without editing the workspace's saved config. Returns the worker
  summary plus every pending `seo_opportunity` action.
- `revenueos_measure()` — re-crawls pages behind executed SEO actions and records whether
  the defect is actually gone.
- `revenueos_today()` / `revenueos_results()` — see pending findings / measured outcomes.

## Inputs

No file upload is required. The audit reads:
- `website` from the workspace's `revenueos.yaml` (set during `revenueos init`), or the
  `website` argument passed to `revenueos_seo_audit`.
- `competitors` from `revenueos.yaml`, for the domain-authority comparison.
- Optionally, `seo.site_repo` in `revenueos.yaml` — a local path to the site's source, if the
  business's website source lives on disk, for a sitemap/robots surface probe.

## What it produces

One `seo_opportunity` action per defect, each carrying: the URL, why it qualifies, and the
specific RevenueOS skill that would fix it (e.g. rewriting a missing title/description,
resolving a duplicate-title collision, refreshing a thin page, adding technical indexing
surface, or closing a competitor's authority gap). Running `measure` afterwards re-crawls the
page and records `measured` / `no_effect` — never a fabricated result.

## Approval rule

The audit only ever queues actions. Nothing on the live website is touched until a human
(or the calling agent, on the human's behalf) calls `revenueos approve <id>` /
`revenueos_approve` and then `revenueos execute <id>` / `revenueos_execute` — which runs the
matching skill and writes the deliverable to `data/outputs/` for review, it does not publish
directly to the site.
