"""CONTENT — turn the skill catalogue into concrete content opportunities, and run them.

The 790 vendored skills are prose procedures for an LLM.  This worker picks the ones that
match the business's channels (registry search over the unified index), creates one
`content_opportunity` action per skill, and — on Execute — runs the skill the way a
Claude Code host would: SKILL.md as the system prompt, business context + corrections
as the user turn, output written to data/outputs/.  Content never auto-publishes.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from ..context import BusinessContext
from ..llm import LLM, Message
from ..paths import Workspace
from ..registry import load_registry, search_skills, skill_text
from ..store import Store
from . import WorkerResult

# channel keyword → registry query; preference order gives the "best known" implementation first
CHANNEL_QUERIES: dict[str, list[str]] = {
    "seo": ["seo content brief", "programmatic seo", "keyword opportunity"],
    "linkedin": ["linkedin post", "linkedin content", "founder content"],
    "email": ["newsletter", "email sequence", "lifecycle email"],
    "cold email": ["cold email", "outbound sequence"],
    "blog": ["blog post", "editorial", "content strategy"],
    "website": ["landing page", "website copy", "conversion copy"],
    "youtube": ["youtube", "video script", "short-form video"],
    "twitter": ["twitter thread", "x post"],
    "x": ["twitter thread"],
    "reddit": ["reddit", "community"],
    "google ads": ["google ads copy", "search ads"],
    "meta ads": ["meta ads creative", "facebook ads copy"],
    "ads": ["ad creative", "ad copy"],
    "case studies": ["case study"],
    "product hunt": ["product hunt launch"],
}
PREFERRED_SOURCES = ("core", "growth", "operations", "seo", "pipelines", "creative", "playbooks")


def opportunities_for(ws: Workspace, channels: list[str], per_channel: int = 2) -> list[dict[str, Any]]:
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ch in channels or ["website", "seo", "linkedin"]:
        for q in CHANNEL_QUERIES.get(ch.lower().strip(), [ch]):
            hits = search_skills(ws, q, limit=8)
            hits.sort(key=lambda s: (PREFERRED_SOURCES.index(s["source"]) if s["source"] in PREFERRED_SOURCES else 99))
            for s in hits:
                key = f"{s['source']}/{s['slug']}"
                if key in seen:
                    continue
                seen.add(key)
                picked.append({**s, "channel": ch, "query": q})
                if sum(1 for p in picked if p["channel"] == ch) >= per_channel:
                    break
            if sum(1 for p in picked if p["channel"] == ch) >= per_channel:
                break
    return picked


class ContentWorker:
    name = "content"
    description = "Match the skill catalogue to the business's channels and queue content to produce."
    upstream = "unified skill registry (Marketing-Agent-OS, marketingskills, Marketing OS, SEO Operator, Single Brain, ...)"

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult:
        if not ctx.is_onboarded():
            return WorkerResult(ok=False, summary="", error="not onboarded — run `revenueos init` first")
        load_registry(ws)
        week = datetime.now(UTC).strftime("%G-W%V")
        created = 0
        picks = opportunities_for(ws, ctx.channels)
        for s in picks:
            aid = store.create_action(
                "content_opportunity", f"{s['channel']}: {s['name']}", s["description"][:400],
                run_id=run_id, dedupe_key=f"content:{week}:{s['source']}/{s['slug']}",
                context={"executor": "run_skill", "skill": f"{s['source']}/{s['slug']}", "channel": s["channel"], "path": s["path"]},
            )
            created += 1 if aid else 0
        return WorkerResult(ok=True, summary=f"{created} content opportunity(ies) for channels {', '.join(ctx.channels) or 'defaults'} (week {week}).",
                            actions_created=created, details={"picked": [f"{p['source']}/{p['slug']}" for p in picks]})


def execute_content(ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, action: dict[str, Any]) -> str:
    """Run a SKILL.md against the business context. Used by content, seo and ads actions alike."""
    if llm is None:
        return "cannot run a skill without an LLM credential (set ANTHROPIC_API_KEY); marked executed by hand."
    c = action["context"]
    source, slug = c["skill"].split("/", 1)
    skill_md = skill_text(ws, source, slug)
    task = c.get("skill_input") or action["content"]
    system = (
        "You are a worker inside RevenueOS, an autonomous revenue department. Follow the SKILL below exactly, "
        "using only the business context provided. Never invent metrics, customers or proof (truth-rules). "
        "Return the finished deliverable in Markdown, ready for a human to approve.\n\n=== SKILL ===\n" + skill_md[:60000]
    )
    user = f"=== BUSINESS CONTEXT ===\n{ctx.prompt_summary()}\n\n=== TASK ===\n{action['title']}\n{task}"
    out = llm.complete_sync([Message("system", system), Message("user", user)], max_tokens=8000)
    safe = re.sub(r"[^a-z0-9]+", "-", f"{slug}-{action['id']}".lower()).strip("-")
    path = ws.outputs / f"{datetime.now(UTC):%Y%m%d}-{safe}.md"
    path.write_text(f"# {action['title']}\n\n_skill: {c['skill']}_\n\n{out}\n", encoding="utf-8")
    return f"deliverable written to {path.relative_to(ws.root)}"
