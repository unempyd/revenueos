"""Shared relevance layer.

Two jobs, both conditioned on the Product Brain:
  1. Turn the Brain's intent-grouped search_seeds + competitor entities into a
     ready-to-run query list (so discovery searches for people ALREADY
     expressing the pain, using the ICP's words, never our product name).
  2. Gate raw candidates (HN threads, web results, …) through a strict,
     chain-of-thought LLM judge that DEFAULTS TO REJECT and requires a quotable
     overlap with our wedge — surfacing nothing beats surfacing noise.

This is the discipline that stops "topically related but our product is
irrelevant" matches (the off-product HN/Reddit picks).
"""

from __future__ import annotations

from typing import Any

import structlog

from revenueos.llm import LLM, Message
from .text import parse_json_lenient

log = structlog.get_logger()


def brain_queries(
    brain: dict[str, Any] | None,
    *,
    intents: tuple[str, ...] = ("switching", "comparison", "question", "pain", "shopping"),
    limit: int = 14,
) -> list[str]:
    """Flatten the Brain's intent search-seeds + competitor expansions into a
    deduped query list. Returns [] if there's no brain (caller falls back)."""
    if not brain:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(q: str) -> None:
        q = (q or "").strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)

    seeds = brain.get("search_seeds") or {}
    for intent in intents:
        for q in (seeds.get(intent) or [])[:5]:
            add(q)
    ents = brain.get("entities") or {}
    for c in (ents.get("competitors") or [])[:3]:
        add(f"{c} alternative")
        add(f"{c} vs")
    return out[:limit]


# ---------------------------------------------------------------------------
# The relevance gate (default REJECT, ProductFit floor, quotable overlap).
# ---------------------------------------------------------------------------

_GATE_SYSTEM = """\
You filter raw {source} candidates for a marketing team and decide which are
GENUINE opportunities for THIS specific product. Be strict — surfacing nothing is
better than surfacing noise, and most candidates are NOT a fit.

For each candidate, reason briefly, then score. Output STRICT JSON only — an array
in the SAME ORDER as the input, no preface, no fences:
[
  {
    "id": "<the id from input>",
    "product_fit": 0.0-1.0,    // does our WEDGE actually solve the problem in THIS item?
    "icp_match": 0.0-1.0,      // is the person/community our ICP (not a disqualified audience)?
    "intent": 0.0-1.0,         // pain/switching/comparison/shopping beats generic curiosity
    "overlap": "<the exact phrase in the item our wedge addresses, or 'none'>",
    "verdict": "SURFACE" | "REJECT",
    "mention_product": true | false,
    "angle": "<one sentence: how to contribute genuinely / the reply or comment angle>",
    "reason": "<one sentence: why this score>"
  }
]

Rules:
- DEFAULT TO REJECT. Only SURFACE if our wedge clearly addresses the item's ACTUAL
  topic AND the person is in our ICP.
- If "overlap" is "none", product_fit MUST be < 0.5 and verdict MUST be REJECT.
- Topical keyword overlap is NOT product fit. "a thread about AI tools" is NOT the
  same as "someone wants to rank/compare their AI stack."
- A DISQUALIFIER audience (listed in the brain) is an instant REJECT.
- Output ONLY the JSON array. The first character is '['.\
"""


async def gate_candidates(
    llm: LLM,
    *,
    brain: dict[str, Any] | None,
    items: list[dict[str, Any]],
    source: str = "Hacker News",
    min_product_fit: float = 0.6,
    min_total: float = 0.7,
    keep: int = 6,
) -> list[dict[str, Any]]:
    """Score every candidate and return only genuine fits, best-first.

    `items` each need: id, title, body (snippet), and optionally extra/url. The
    returned items are the originals annotated with product_fit, total, angle,
    reason, mention_product. Empty list is a valid (good) answer.
    """
    if not items:
        return []
    if not brain:
        # no brain → we can't judge product-fit; return the inputs unjudged
        return items[:keep]

    from .product_brain import brain_context_block

    payload = [
        {
            "id": str(it.get("id") or i),
            "title": (it.get("title") or "")[:200],
            "body": (it.get("body") or it.get("snippet") or "")[:500],
            "extra": it.get("extra") or "",
        }
        for i, it in enumerate(items)
    ]
    user = (
        brain_context_block(brain)
        + f"\n\n{source.upper()} CANDIDATES (judge each):\n"
        + _dump(payload)
        + "\n\nScore them. Output only the JSON array."
    )
    try:
        raw = await llm.complete(
            [
                Message(role="system", content=_GATE_SYSTEM.replace("{source}", source)),
                Message(role="user", content=user),
            ],
            temperature=0.3,
            max_tokens=2200,
        )
    except Exception as e:
        log.warning("relevance_gate_failed", source=source, error=repr(e))
        return items[:keep]

    verdicts = parse_json_lenient(raw)
    if not isinstance(verdicts, list):
        log.warning("relevance_gate_unparsed", source=source)
        return items[:keep]

    by_id = {str(it.get("id") or i): it for i, it in enumerate(items)}
    survivors: list[dict[str, Any]] = []
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        it = by_id.get(str(v.get("id")))
        if it is None:
            continue
        pf = _f(v.get("product_fit"))
        icp = _f(v.get("icp_match"))
        intent = _f(v.get("intent"))
        total = 0.45 * pf + 0.3 * icp + 0.25 * intent
        if str(v.get("verdict", "")).upper() != "SURFACE":
            continue
        if pf < min_product_fit or total < min_total:
            continue
        out = dict(it)
        out.update(
            product_fit=round(pf, 2),
            total=round(total, 2),
            angle=str(v.get("angle") or ""),
            reason=str(v.get("reason") or ""),
            overlap=str(v.get("overlap") or ""),
            mention_product=bool(v.get("mention_product")),
        )
        survivors.append(out)

    survivors.sort(key=lambda x: -x.get("total", 0))
    log.info("relevance_gate", source=source, candidates=len(items), surfaced=len(survivors))
    return survivors[:keep]


def _f(v: Any) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def _dump(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)[:6000]
