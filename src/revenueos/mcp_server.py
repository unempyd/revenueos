"""revenueos-mcp — RevenueOS Community over the Model Context Protocol.

Exposes the same loop the `revenueos` CLI and control panel expose — connect, analyse,
opportunity, present, approve, execute, measure, record — as MCP tools, so any MCP host
(Claude Desktop, Claude Code, another agent) can drive one RevenueOS workspace.

Workspace resolution is identical to the CLI: `Workspace.locate()` walks up from
$REVENUEOS_ROOT or the current directory to find `company-context/` + `skills/`. Point the
host's working directory (or REVENUEOS_ROOT) at an onboarded workspace before connecting.

No tool here sends an email, publishes content or spends money. Workers only create
`actions`; only `revenueos_execute`, after `revenueos_approve`, calls `execute_action`.
"""
from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .context import BusinessContext
from .llm import maybe_llm
from .paths import Workspace
from .registry import search_skills
from .store import Store
from .today import build_brief
from .workers import all_workers, execute_action, refused, run_worker

mcp = FastMCP(
    "revenueos",
    instructions=(
        "RevenueOS Community: connect a business, run its capability workers, and see what they found. "
        "Nothing sends, publishes or spends except revenueos_execute, which acts only on an action already "
        "approved with revenueos_approve. The worker tools read the outside world and write findings into "
        "this workspace; they never act on it. Each tool's annotations say which is which. "
        "Start with revenueos_today."
    ),
)


def _boot() -> tuple[Workspace, Store, BusinessContext]:
    ws = Workspace.locate()
    return ws, Store(ws.db), BusinessContext.load(ws)


def _run(name: str) -> dict[str, Any]:
    ws, store, ctx = _boot()
    llm = maybe_llm()
    if name == "all":
        results = {n: run_worker(n, ws, store, ctx, llm) for n in all_workers()}
        return {
            "ok": all(r.ok for r in results.values()),
            "results": {n: r.as_json() for n, r in results.items()},
        }
    return run_worker(name, ws, store, ctx, llm).as_json()


# ── tools ─────────────────────────────────────────────────────────────────
# Tool annotations are hints a host shows the user before it lets a model call a tool, so
# they are written from what the code below actually does, not from what is convenient:
#   readOnlyHint      true only when the tool changes nothing but the SQLite schema `Store()`
#                     creates on first open. Anything that writes an action, a draft, a lead,
#                     an outcome or a file is false.
#   destructiveHint   true only for revenueos_execute, the single tool that acts on the world
#                     (SMTP send, site deploy, campaign pause/budget, invoice, deliverable).
#                     A tool that writes to the store but changes nothing outside it is false.
#   idempotentHint    true where repeating the call adds nothing: every worker passes a
#                     `dedupe_key` to `create_action`, and approve/ignore set a status.
#                     False for revenueos_execute — a second execute is a second send.
#   openWorldHint     true when the call can reach a host off this machine: the configured
#                     model provider, a crawl target, an ad platform, IMAP/SMTP, a lead source.
@mcp.tool(
    title="Today's revenue brief",
    annotations=ToolAnnotations(
        title="Today's revenue brief",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def revenueos_today() -> dict[str, Any]:
    """The RevenueOS Community brief: pending opportunities awaiting a decision, current
    pipeline value, and the latest metrics for this connected business. Call this first to
    see what RevenueOS found and what is waiting on approve/execute/ignore."""
    ws, store, _ctx = _boot()
    brief = build_brief(store, ws)
    return {"counts": brief.counts, "pipeline_value": brief.pipeline_value, "actions": brief.actions,
            "metrics": brief.metrics, "offer": brief.offer}


@mcp.tool(
    title="Results and measured outcomes",
    annotations=ToolAnnotations(
        title="Results and measured outcomes",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def revenueos_results() -> dict[str, Any]:
    """What RevenueOS Community has done and what measurable result occurred: every executed
    action with its outcome (emails sent, replies, SEO fixes confirmed by re-crawl, ad spend
    deltas, deliverables written). Outcomes are honest — pending until real evidence exists."""
    _ws, store, _ctx = _boot()
    brief = build_brief(store)
    return {"summary": brief.summary, "results": brief.results}


@mcp.tool(
    title="Run a RevenueOS worker",
    annotations=ToolAnnotations(
        title="Run a RevenueOS worker",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
def revenueos_run(worker: str) -> dict[str, Any]:
    """Run one RevenueOS Community worker now and return what it found: discover, outreach,
    inbox, seo, ads-audit, content, monitor, measure, or "all" to run every worker in order.
    Workers only analyse and queue actions — they never send, publish or spend."""
    return _run(worker)


@mcp.tool(
    title="Audit the website for SEO defects",
    annotations=ToolAnnotations(
        title="Audit the website for SEO defects",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
def revenueos_seo_audit(website: str | None = None) -> dict[str, Any]:
    """RevenueOS SEO Auditor: crawl the business's website for missing titles/descriptions,
    thin pages, duplicate titles and a missing sitemap, and compare domain authority against
    its configured competitors. Pass `website` to audit a different URL for this one run.
    Each defect is queued as a seo_opportunity action pointing at the SEO skill that fixes
    it — nothing is changed until you call revenueos_approve then revenueos_execute."""
    ws, store, ctx = _boot()
    if website:
        ctx.config["website"] = website
    llm = maybe_llm()
    result = run_worker("seo", ws, store, ctx, llm)
    actions = store.list_actions("pending", action_type="seo_opportunity")
    return {"result": result.as_json(), "actions": actions}


@mcp.tool(
    title="Audit an ad platform export (overwrites the staged export)",
    annotations=ToolAnnotations(
        title="Audit an ad platform export (overwrites the staged export)",
        readOnlyHint=False,
        # It copies the given CSV over data/exports/ads-<platform>.csv, replacing whatever export
        # was staged there. That is RevenueOS's own slot, but the bytes came from the customer and
        # the previous file is gone, so a host deciding whether to auto-approve should be told.
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
def revenueos_ads_audit(csv_path: str, platform: str) -> dict[str, Any]:
    """RevenueOS Ads Auditor: ingest an ad platform export and flag wasted spend, over-pacing
    campaigns and dangerous spend concentration. `csv_path` must be the 13-column claude-ads
    generic export format (date, account_id, account_name, campaign_id, campaign_name,
    campaign_status, creative_id, creative_name, conversion_action, conversions, budget,
    spend, currency); `platform` is one of google, meta, youtube, linkedin, tiktok,
    microsoft, apple, amazon, reddit, pinterest, snapchat, x. The file is copied into
    data/exports/ads-<platform>.csv before the audit runs."""
    ws, store, ctx = _boot()
    dest = ws.exports / f"ads-{platform}.csv"
    shutil.copy2(Path(csv_path).expanduser(), dest)
    llm = maybe_llm()
    result = run_worker("ads-audit", ws, store, ctx, llm)
    return result.as_json()


@mcp.tool(
    title="Discover and qualify leads",
    annotations=ToolAnnotations(
        title="Discover and qualify leads",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
def revenueos_discover_leads(csv_path: str | None = None) -> dict[str, Any]:
    """RevenueOS Lead Discovery: read prospects from OpenOutreach (when installed, across a
    process boundary) and any CSV drop already in data/exports/, then run the qualification
    gate. Pass `csv_path` (Instantly/Smartlead columns: email, first_name, last_name, company,
    title, website, linkedin_url, reason) to import one more file first — it is copied into
    data/exports/leads-<timestamp>.csv. Only a QUALIFIED lead (business email + business
    website + a real company name) becomes a prospect action; the result reports found,
    contactable and qualified as separate numbers. A qualified_at column is ignored."""
    ws, store, ctx = _boot()
    if csv_path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        shutil.copy2(Path(csv_path).expanduser(), ws.exports / f"leads-{stamp}.csv")
    llm = maybe_llm()
    result = run_worker("discover", ws, store, ctx, llm)
    return result.as_json()


@mcp.tool(
    title="Draft outreach emails (never sends)",
    annotations=ToolAnnotations(
        title="Draft outreach emails (never sends)",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
def revenueos_draft_followups() -> dict[str, Any]:
    """RevenueOS Sales Follow-up: draft first-touch cold emails for newly discovered
    prospects from the business's own canon (offer, differentiators, pain points). Drafts
    are queued as follow_up actions and are never sent by this tool — call revenueos_approve
    then revenueos_execute to send one. Set REVENUEOS_DRY_RUN=1 (the Community default) to
    write sends to data/outputs/ instead of over real SMTP."""
    return _run("outreach")


@mcp.tool(
    title="Approve a pending action",
    annotations=ToolAnnotations(
        title="Approve a pending action",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def revenueos_approve(action_id: int) -> dict[str, Any]:
    """Approve a pending action from revenueos_today so it can be executed. Approving alone
    never sends, publishes or spends anything — call revenueos_execute afterwards to act."""
    _ws, store, _ctx = _boot()
    action = store.get_action(action_id)
    if not action:
        return {"ok": False, "error": f"no action {action_id}"}
    store.set_action_status(action_id, "approved")
    if action["context"].get("draft_id"):
        store.set_draft_approval(action["context"]["draft_id"], "approved")
    return {"ok": True, "action_id": action_id, "title": action["title"], "status": "approved"}


@mcp.tool(
    title="Execute an approved action (sends, publishes or spends)",
    annotations=ToolAnnotations(
        title="Execute an approved action (sends, publishes or spends)",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def revenueos_execute(action_id: int) -> dict[str, Any]:
    """Execute an approved action: send the drafted email, or run the matching SKILL.md
    (SEO fix, ads recommendation, content deliverable) against the business context. This is
    the only tool in RevenueOS Community that sends, publishes or writes a deliverable, and
    it only acts on actions already approved with revenueos_approve."""
    ws, store, ctx = _boot()
    action = store.get_action(action_id)
    if not action:
        return {"ok": False, "error": f"no action {action_id}"}
    llm = maybe_llm()
    try:
        outcome = execute_action(ws, store, ctx, llm, action)
        if refused(outcome):
            return {"ok": False, "action_id": action_id, "title": action["title"], "outcome": outcome, "error": "not executed: " + outcome}
        store.set_action_status(action_id, "executed")
        store.record_outcome(action_id, "pending", note="awaiting measurement")
        return {"ok": True, "action_id": action_id, "title": action["title"], "outcome": outcome}
    except Exception as exc:  # noqa: BLE001 — report the failure back to the caller, exactly like cli._decide
        store.set_action_status(action_id, "failed")
        return {"ok": False, "action_id": action_id, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool(
    title="Dismiss a pending action",
    annotations=ToolAnnotations(
        title="Dismiss a pending action",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def revenueos_ignore(action_id: int) -> dict[str, Any]:
    """Dismiss a pending action from revenueos_today without acting on it."""
    _ws, store, _ctx = _boot()
    action = store.get_action(action_id)
    if not action:
        return {"ok": False, "error": f"no action {action_id}"}
    store.set_action_status(action_id, "ignored")
    return {"ok": True, "action_id": action_id, "title": action["title"], "status": "ignored"}


@mcp.tool(
    title="Measure what executed actions changed",
    annotations=ToolAnnotations(
        title="Measure what executed actions changed",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
def revenueos_measure() -> dict[str, Any]:
    """RevenueOS Revenue Monitor: check every executed action for a measurable result — an
    SEO re-crawl, a reply or bounce on a sent email, the next ad export's spend delta, or a
    written content deliverable — and record it. Outcomes are honest: pending until evidence
    exists, never fabricated."""
    return _run("measure")


@mcp.tool(
    title="Search the skill catalogue",
    annotations=ToolAnnotations(
        title="Search the skill catalogue",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def revenueos_search_skills(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """RevenueOS Marketing Intelligence: search the unified skill catalogue (SEO, ads, cold
    email, content, growth — hundreds of vendored playbooks) for a topic and return matching
    skills with their source and description. Use this to see which skill a content or SEO
    action would run before approving it, or to find a skill for a task not yet queued."""
    ws, _store, _ctx = _boot()
    return search_skills(ws, query, limit=limit)


@mcp.tool(
    title="Lessons from measured results",
    annotations=ToolAnnotations(
        title="Lessons from measured results",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def revenueos_lessons(days: int = 60) -> dict[str, Any]:
    """What RevenueOS learned from this business's own measured results: one dated lesson per
    executed action that was measured (what was attempted, what the evidence showed, what to do
    differently). Read this before proposing work — the same lessons are injected into every
    worker prompt. Run `revenueos learn` (or the CLI) to derive new ones from recent outcomes;
    nothing here is written by this tool."""
    from .learning import recent_lessons

    ws, _store, _ctx = _boot()
    text = recent_lessons(ws, days=days, max_chars=20000)
    return {"days": days, "lessons": text, "count": text.count("\n## ") + (1 if text else 0)}


# ── resources ─────────────────────────────────────────────────────────────
@mcp.resource("revenueos://today")
def resource_today() -> str:
    """TODAY, rendered as text: pending opportunities, pipeline value, and how to decide on them."""
    ws, store, ctx = _boot()
    return build_brief(store, ws).render_text(ctx.company_name)


@mcp.resource("revenueos://results")
def resource_results() -> str:
    """RESULTS, rendered as text: what RevenueOS did and what measurable result occurred."""
    _ws, store, ctx = _boot()
    brief = build_brief(store)
    s = brief.summary
    line1 = (f"actions: {s.get('found', 0)} found · {s.get('executed', 0)} executed · "
             f"{s.get('measured', 0)} measured · {s.get('produced', 0)} produced (not published)")
    line2 = (
        f"{s.get('emails_sent', 0)} emails sent · {s.get('replies', 0)} replies · "
        f"{s.get('bounces', 0)} bounces · {s.get('booked', 0)} booked · ${s.get('pipeline_value', 0):,.0f} pipeline"
    )
    lines = [f"RESULTS — {ctx.company_name}", "", line1, line2, ""]
    lines += brief.result_lines(limit=100)
    if not brief.results:
        lines.append("No executed actions yet. Approve and execute something from revenueos_today.")
    return "\n".join(lines)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
