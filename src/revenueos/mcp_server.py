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

from .context import BusinessContext
from .llm import maybe_llm
from .paths import Workspace
from .registry import search_skills
from .store import Store
from .today import build_brief
from .workers import all_workers, execute_action, run_worker

mcp = FastMCP(
    "revenueos",
    instructions=(
        "RevenueOS Community: connect a business, run its capability workers, and see what they found. "
        "Nothing sends, publishes or spends — call revenueos_approve then revenueos_execute to act on any "
        "action; everything else is read-only analysis. Start with revenueos_today."
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
@mcp.tool()
def revenueos_today() -> dict[str, Any]:
    """The RevenueOS Community brief: pending opportunities awaiting a decision, current
    pipeline value, and the latest metrics for this connected business. Call this first to
    see what RevenueOS found and what is waiting on approve/execute/ignore."""
    _ws, store, _ctx = _boot()
    brief = build_brief(store)
    return {"counts": brief.counts, "pipeline_value": brief.pipeline_value, "actions": brief.actions, "metrics": brief.metrics}


@mcp.tool()
def revenueos_results() -> dict[str, Any]:
    """What RevenueOS Community has done and what measurable result occurred: every executed
    action with its outcome (emails sent, replies, SEO fixes confirmed by re-crawl, ad spend
    deltas, deliverables written). Outcomes are honest — pending until real evidence exists."""
    _ws, store, _ctx = _boot()
    brief = build_brief(store)
    return {"summary": brief.summary, "results": brief.results}


@mcp.tool()
def revenueos_run(worker: str) -> dict[str, Any]:
    """Run one RevenueOS Community worker now and return what it found: discover, outreach,
    inbox, seo, ads-audit, content, monitor, measure, or "all" to run every worker in order.
    Workers only analyse and queue actions — they never send, publish or spend."""
    return _run(worker)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def revenueos_draft_followups() -> dict[str, Any]:
    """RevenueOS Sales Follow-up: draft first-touch cold emails for newly discovered
    prospects from the business's own canon (offer, differentiators, pain points). Drafts
    are queued as follow_up actions and are never sent by this tool — call revenueos_approve
    then revenueos_execute to send one. Set REVENUEOS_DRY_RUN=1 (the Community default) to
    write sends to data/outputs/ instead of over real SMTP."""
    return _run("outreach")


@mcp.tool()
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


@mcp.tool()
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
        store.set_action_status(action_id, "executed")
        store.record_outcome(action_id, "pending", note="awaiting measurement")
        return {"ok": True, "action_id": action_id, "title": action["title"], "outcome": outcome}
    except Exception as exc:  # noqa: BLE001 — report the failure back to the caller, exactly like cli._decide
        store.set_action_status(action_id, "failed")
        return {"ok": False, "action_id": action_id, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def revenueos_ignore(action_id: int) -> dict[str, Any]:
    """Dismiss a pending action from revenueos_today without acting on it."""
    _ws, store, _ctx = _boot()
    action = store.get_action(action_id)
    if not action:
        return {"ok": False, "error": f"no action {action_id}"}
    store.set_action_status(action_id, "ignored")
    return {"ok": True, "action_id": action_id, "title": action["title"], "status": "ignored"}


@mcp.tool()
def revenueos_measure() -> dict[str, Any]:
    """RevenueOS Revenue Monitor: check every executed action for a measurable result — an
    SEO re-crawl, a reply or bounce on a sent email, the next ad export's spend delta, or a
    written content deliverable — and record it. Outcomes are honest: pending until evidence
    exists, never fabricated."""
    return _run("measure")


@mcp.tool()
def revenueos_search_skills(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """RevenueOS Marketing Intelligence: search the unified skill catalogue (SEO, ads, cold
    email, content, growth — hundreds of vendored playbooks) for a topic and return matching
    skills with their source and description. Use this to see which skill a content or SEO
    action would run before approving it, or to find a skill for a task not yet queued."""
    ws, _store, _ctx = _boot()
    return search_skills(ws, query, limit=limit)


# ── resources ─────────────────────────────────────────────────────────────
@mcp.resource("revenueos://today")
def resource_today() -> str:
    """TODAY, rendered as text: pending opportunities, pipeline value, and how to decide on them."""
    _ws, store, ctx = _boot()
    return build_brief(store).render_text(ctx.company_name)


@mcp.resource("revenueos://results")
def resource_results() -> str:
    """RESULTS, rendered as text: what RevenueOS did and what measurable result occurred."""
    _ws, store, ctx = _boot()
    brief = build_brief(store)
    s = brief.summary
    line1 = f"{s.get('found', 0)} opportunities found · {s.get('executed', 0)} executed · {s.get('measured', 0)} measured"
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
