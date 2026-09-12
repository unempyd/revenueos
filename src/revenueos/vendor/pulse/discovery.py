"""Discovery tools — find places where the user's product is genuinely relevant.

`find_hn_opportunities` is now Brain-aware and relevance-gated (it used to be a
naive keyword + recency search, which is how off-product threads like "Uber's
$1,500/mo AI limit" slipped through). It searches with the Product Brain's
intent queries + competitor names, then runs every candidate through the
default-REJECT relevance gate, so only genuine fits come back.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import structlog

from .registry import Tool, tool

log = structlog.get_logger()

HN_ALGOLIA = "https://hn.algolia.com/api/v1/search_by_date"


async def _hn_search(client: httpx.AsyncClient, query: str, since: int) -> list[dict]:
    params = {
        "query": query,
        "tags": "(story,comment)",
        "numericFilters": f"created_at_i>{since}",
        "hitsPerPage": "8",
    }
    try:
        r = await client.get(HN_ALGOLIA, params=params)
        if r.status_code >= 400:
            return []
        return r.json().get("hits", [])
    except Exception as e:
        log.warning("hn_search_failed", query=query, error=repr(e))
        return []


def _clean(text: str) -> str:
    return (
        (text or "")
        .replace("<p>", "\n").replace("</p>", "").replace("<i>", "").replace("</i>", "")
        .strip()[:400]
    )


def make_discovery_tools(
    store: Any = None, project_id: int | None = None, llm: Any = None
) -> list[Tool]:
    """HN discovery, bound to (store, project_id, llm) so it can read the
    Product Brain and run the relevance gate."""

    @tool
    async def find_hn_opportunities(keywords: list[str] = [], days_back: int = 14) -> str:
        """Find recent Hacker News threads where THIS product genuinely belongs.

        Searches with the Product Brain's intent queries + competitor names (plus
        any keywords you pass), then runs every candidate through a strict
        relevance gate that rejects topical-but-off-product matches. Returns only
        genuine fits, each with a why + a suggested angle. May return zero — that
        means there's nothing worth commenting on right now, which is fine.

        Args:
            keywords: Optional extra search terms (the Brain supplies most).
            days_back: How far back to search (default 14, capped 30).
        """
        brain = store.get_product_brain(project_id) if (store is not None and project_id) else None
        days_back = max(1, min(int(days_back), 30))
        since = int((datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp())

        # build the query set: Brain intent queries + competitor names + caller keywords
        queries: list[str] = []
        seen_q: set[str] = set()

        def add_q(q: str) -> None:
            q = (q or "").strip()
            if q and q.lower() not in seen_q:
                seen_q.add(q.lower())
                queries.append(q)

        if brain:
            from .relevance import brain_queries

            for q in brain_queries(brain, intents=("question", "comparison", "switching", "pain")):
                add_q(q)
            for c in ((brain.get("entities") or {}).get("competitors") or [])[:3]:
                add_q(c)
            cat = brain.get("category") or ""
            if cat:
                add_q(cat)
        for kw in (keywords or [])[:6]:
            add_q(kw)
        if not queries:
            return json.dumps({"ok": False, "error": "no Product Brain and no keywords to search with"})

        # fan out
        hits: dict[str, dict] = {}
        async with httpx.AsyncClient(timeout=15.0) as client:
            for q in queries[:10]:
                for hit in await _hn_search(client, q, since):
                    oid = hit.get("objectID") or ""
                    if oid and oid not in hits:
                        hits[oid] = hit

        candidates = []
        for oid, hit in hits.items():
            title = hit.get("title") or hit.get("story_title") or "(comment)"
            candidates.append({
                "id": oid,
                "title": title,
                "body": _clean(hit.get("story_text") or hit.get("comment_text") or ""),
                "extra": f"{hit.get('points') or 0} pts · {hit.get('created_at') or ''}",
                "hn_url": f"https://news.ycombinator.com/item?id={oid}",
                "created_at": hit.get("created_at") or "",
            })

        if not candidates:
            return json.dumps({"ok": True, "found": 0, "items": [], "note": "no recent threads matched the queries"})

        # gate (default REJECT, ProductFit floor) — only if we have the brain + llm
        if llm is not None and brain:
            from .relevance import gate_candidates

            # HN bar is a touch lower than a direct pitch: you comment helpfully on
            # adjacent threads too. The ProductFit floor still blocks pure noise.
            survivors = await gate_candidates(
                llm, brain=brain, items=candidates, source="Hacker News",
                min_product_fit=0.5, min_total=0.6, keep=5,
            )
            items = [
                {
                    "title": s["title"],
                    "hn_url": s["hn_url"],
                    "score": s.get("total"),
                    "why_relevant": (s.get("reason") or "") + (f" (overlap: {s['overlap']})" if s.get("overlap") and s["overlap"] != "none" else ""),
                    "suggested_angle": s.get("angle") or "",
                }
                for s in survivors
            ]
            return json.dumps({"ok": True, "found": len(items), "gated": True, "items": items}, ensure_ascii=False)

        # fallback: no brain yet — recency sort, ungated (legacy behavior)
        candidates.sort(key=lambda c: c.get("created_at") or "", reverse=True)
        items = [{"title": c["title"], "hn_url": c["hn_url"], "snippet": c["body"]} for c in candidates[:10]]
        return json.dumps({"ok": True, "found": len(items), "gated": False, "items": items}, ensure_ascii=False)

    return [find_hn_opportunities]
