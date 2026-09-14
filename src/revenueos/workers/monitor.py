"""MONITOR — continuous market monitoring (pulse-cmo's discovery loop).

Uses the vendored pulse-cmo Hacker News discovery (Algolia, no key) with the Product
Brain built from company-context, and the vendored relevance gate (default-REJECT) when an
LLM is available.  Each surviving thread becomes a `market_signal` action with the
suggested angle.  Reddit discovery is not included: pulse's Reddit path is vendor-locked to
OpenAdapter (see research/ADDITIONAL_REPOS.md for the replacement).
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from ..context import BusinessContext
from ..llm import LLM
from ..paths import Workspace
from ..store import Store
from ..vendor.pulse.discovery import make_discovery_tools
from . import WorkerResult

HN_ITEM = "https://hacker-news.firebaseio.com/v0/item/{id}.json"


class _BrainStore:
    """Adapter: pulse's tools read the Product Brain through store.get_product_brain(project_id)."""

    def __init__(self, brain: dict[str, Any]) -> None:
        self._brain = brain

    def get_product_brain(self, project_id: int) -> dict[str, Any]:
        return self._brain


async def _find(ctx: BusinessContext, llm: LLM | None, days_back: int) -> dict[str, Any]:
    brain = ctx.brain()
    tool = make_discovery_tools(store=_BrainStore(brain), project_id=1, llm=llm)[0]
    keywords = [brain["category"]] if brain.get("category") else []
    raw = await tool.call({"keywords": keywords, "days_back": days_back})
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": raw[:300]}


def _hn_id(url: str) -> str:
    m = re.search(r"[?&]id=(\d+)", url or "")
    return m.group(1) if m else ""


def live_on_hn(url: str, client: Any = None) -> tuple[bool, str]:
    """Is this thread still alive on Hacker News?

    The search index the discovery tool uses does not carry moderation state — it simply drops
    what has been killed, which means a thread can be indexed, surfaced to a customer, and dead by
    the time they open it. Firebase does carry it: a flagged item returns `dead: true` with its
    text replaced by `[flagged]`. Verified against a real flagged comment on 2026-09-14. One HTTP
    call per candidate, no model, and it fails open: if the check itself cannot run we keep the
    thread rather than silently dropping the worker's output.
    """
    import httpx

    hid = _hn_id(url)
    if not hid:
        return True, ""
    try:
        c = client or httpx.Client(timeout=10)
        r = c.get(HN_ITEM.format(id=hid))
        if r.status_code != 200:
            return True, ""
        item = r.json()
    except Exception:  # noqa: BLE001 — a monitoring check must never sink the run
        return True, ""
    if not isinstance(item, dict):
        return True, ""
    if item.get("deleted"):
        return False, "deleted on Hacker News"
    if item.get("dead"):
        return False, "flagged or killed on Hacker News"
    return True, ""


class MonitorWorker:
    name = "monitor"
    description = "Find conversations (Hacker News) where the business genuinely belongs; propose an angle."
    upstream = "aruntemme/pulse-cmo discovery + relevance gate"

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult:
        if not ctx.is_onboarded():
            return WorkerResult(ok=False, summary="", error="not onboarded — run `revenueos init` first")
        days = int((ctx.config.get("monitor") or {}).get("days_back", 14))
        result = asyncio.run(_find(ctx, llm, days))
        if not result.get("ok"):
            return WorkerResult(ok=False, summary="", error=str(result.get("error") or "discovery failed"))
        created = 0
        if llm is None:
            # recency alone is not relevance: without a model to judge the thread, nothing is put in front of the customer
            return WorkerResult(ok=True, summary=f"0 conversation(s) worth joining; {result.get('found', 0)} candidate thread(s) found, "
                                                 "none judged (no LLM credential — connect one to gate them).",
                                actions_created=0, details={"found": result.get("found", 0), "gated": False, "candidates": len(result.get("items", []))})
        skipped = []
        for item in result.get("items", []):
            url = item.get("hn_url")
            alive, why = live_on_hn(url)
            if not alive:
                skipped.append(why)
                continue
            title = item.get("title") or "(untitled thread)"
            body = item.get("why_relevant") or item.get("snippet") or ""
            angle = item.get("suggested_angle")
            content = body + (f"\n\nSuggested angle: {angle}" if angle else "")
            aid = store.create_action(
                "market_signal", f"Join the conversation: {title[:90]}", content.strip() or "Relevant thread.",
                run_id=run_id, source_url=url, dedupe_key=f"hn:{url}",
                context={"gated": result.get("gated", False), "score": item.get("score")},
            )
            created += 1 if aid else 0
        if result.get("gated"):
            gate = "after the Claude relevance gate (default-reject)"
        elif llm is not None:
            gate = "no candidate threads matched the search seeds"
        else:
            gate = "ungated (no LLM credential — recency only)"
        note = f" {len(skipped)} dropped as flagged or deleted." if skipped else ""
        return WorkerResult(ok=True, summary=f"{created} conversation(s) worth joining; {result.get('found', 0)} relevant thread(s) found.{note}",
                            actions_created=created, details={"found": result.get("found", 0), "gated": result.get("gated"), "gate": gate,
                                                              "skipped_dead": len(skipped)})
