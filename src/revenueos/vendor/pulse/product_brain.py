"""The Product Brain — the shared, persisted product intelligence.

This is the single highest-leverage piece of the engine. Generic, off-product
marketing happens when each tool re-derives a thin understanding of the product
from a one-line description. Instead, we build ONE rich, evidence-grounded
profile at dive time and make every downstream tool (discovery, relevance
gating, positioning, drafting) condition on it.

Structure follows the relevance research: April Dunford positioning (incl. the
WEDGE), ICP segments, Jobs-to-be-Done with the Four Forces (push/pull/anxiety/
habit), extracted entities, the ICP's literal VOCABULARY, the communities where
the ICP actually hangs out, and intent-grouped SEARCH SEEDS used to find people
already expressing the pain. The two highest-leverage fields are `wedge` and
`icp_vocabulary` — they are what make later output specific instead of generic.

Built from evidence only (crawl + brief + competitor reads); fields the evidence
can't support are left empty rather than invented.
"""

from __future__ import annotations

from typing import Any

import structlog

from revenueos.llm import LLM, Message  # RevenueOS: store/strategy_core imports removed; only brain_context_block is used
ActionStore = gather_evidence = render_evidence = None  # noqa: N816
from .text import parse_json_lenient, strip_stray_cjk

log = structlog.get_logger()


_BRAIN_SYSTEM = """\
You are a product strategist building the SHARED intelligence profile that every
downstream marketing tool will condition on. Work ONLY from the evidence provided
(the crawl, the founder's brief, the competitor reads). Be concrete and specific
— generic profiles produce generic marketing. Where the evidence does not support
a field, leave it empty; never invent.

Output STRICT JSON only — no preface, no fences — in this exact shape:
{
  "one_liner": "<what it is, one sentence>",
  "category": "<the market category an outsider would file it under>",
  "competitive_alternatives": [
    {"name": "<include non-software: spreadsheets, doing it by hand, doing nothing>",
     "type": "software|manual|status_quo"}
  ],
  "differentiated_capabilities": ["<what only THIS product does well>"],
  "wedge": {
    "capability": "<the single sharpest differentiated capability>",
    "best_fit_segment": "<the narrow segment that cares most about it>",
    "why_they_care": "<the specific reason it matters to them>"
  },
  "icp": [
    {"segment": "<short name>", "who": "<one sentence>",
     "descriptors": ["<job titles / self-descriptions / where they work>"],
     "pains": ["<a real frustration they have TODAY, in their own words>"],
     "channels": ["<where they actually hang out online>"]}
  ],
  "jtbd": [
    {"job": "<the progress they are trying to make>",
     "functional": "<the practical task>",
     "emotional": "<how they want to feel>",
     "social": "<how they want to be seen>",
     "push": "<frustration with the current way>",
     "pull": "<what attracts them to this kind of solution>",
     "anxiety": "<fear about switching or trying it>",
     "habit": "<inertia keeping them on the status quo>"}
  ],
  "entities": {
    "product_names": [], "feature_names": [], "competitors": [], "integrations": []
  },
  "icp_vocabulary": ["<exact phrases / jargon the ICP uses — lifted from testimonials, FAQ, hero copy>"],
  "communities": {
    "subreddits": ["<real subreddit name without the r/, where this ICP is active>"],
    "hn_topics": ["<Hacker News keywords/topics this ICP discusses>"],
    "other": ["<discords, forums, directories, hashtags>"]
  },
  "search_seeds": {
    "pain":       ["<2-6 word query: someone venting the pain>"],
    "switching":  ["<'<competitor> alternative', 'moving off <competitor>'>"],
    "shopping":   ["<'best <category> for <use case>'>"],
    "comparison": ["<'<competitor A> vs <competitor B>'>"],
    "question":   ["<'how do i <job>', 'what do you use for <job>'>"]
  },
  "disqualifiers": ["<audience types or contexts we explicitly do NOT target>"]
}

Rules:
- `wedge` and `icp_vocabulary` are the most important fields. The wedge is the ONE
  thing to be remembered for versus the alternatives — not a feature list. Get it
  exactly right from the evidence.
- Use REAL competitor names and REAL feature names from the evidence, never generic
  placeholders like "Competitor A".
- `search_seeds` must use the ICP's words and the competitors' names, and must NOT
  contain our own product's name — they exist to find people ALREADY expressing the
  pain or shopping. Each query 2-7 words, lowercase, no quotes.
- `pains` and `icp_vocabulary` must sound like real humans (lift them from the copy),
  not marketing speak.
- Output ONLY the JSON object. The first character is '{'.\
"""


def _ok(brain: Any) -> bool:
    """A brain is usable only if the two load-bearing fields came through."""
    return (
        isinstance(brain, dict)
        and isinstance(brain.get("wedge"), dict)
        and bool(brain["wedge"].get("capability"))
        and bool(brain.get("icp"))
    )


async def generate_product_brain(llm: LLM, store: ActionStore, project_id: int) -> dict[str, Any]:
    """Build the Product Brain from all persisted evidence and save it on the
    project. Returns the brain dict ({} on failure — never persists garbage)."""
    ev = gather_evidence(store, project_id)
    evidence_block = render_evidence(
        ev, include=("product", "brief", "crawl", "competitors", "seo", "traction")
    )
    user = evidence_block + "\n\nBuild the product brain. Output only the JSON object."
    try:
        raw = await llm.complete(
            [Message(role="system", content=_BRAIN_SYSTEM), Message(role="user", content=user)],
            temperature=0.4,
            max_tokens=2600,
        )
    except Exception as e:
        log.warning("product_brain_failed", project_id=project_id, error=repr(e))
        return {}
    brain = parse_json_lenient(raw)
    if not _ok(brain):
        log.warning("product_brain_thin", project_id=project_id)
        return {}
    store.set_product_brain(project_id, brain)
    log.info("product_brain_built", project_id=project_id,
             wedge=(brain.get("wedge") or {}).get("capability", "")[:60])
    return brain


def _join(xs: Any, n: int = 8) -> str:
    if not isinstance(xs, list):
        return ""
    return ", ".join(str(x) for x in xs[:n] if x)


def brain_context_block(brain: dict[str, Any] | None) -> str:
    """Compact, high-signal render of the Brain for injection into downstream
    prompts (discovery, relevance gate, positioning, drafting). Empty string if
    there's no brain yet."""
    if not brain or not isinstance(brain, dict):
        return ""
    wedge = brain.get("wedge") or {}
    rows: list[str] = ["PRODUCT BRAIN (condition every output on this — be specific to it):"]
    if brain.get("one_liner"):
        rows.append(f"  what it is: {brain['one_liner']}")
    if wedge.get("capability"):
        rows.append(
            f"  WEDGE: {wedge.get('capability')} — for {wedge.get('best_fit_segment', '')} "
            f"because {wedge.get('why_they_care', '')}"
        )
    icp = brain.get("icp") or []
    if icp:
        seg = icp[0]
        rows.append(f"  ICP: {seg.get('who') or seg.get('segment', '')}")
        pains = _join(seg.get("pains"), 4)
        if pains:
            rows.append(f"  their pains (their words): {pains}")
    ents = brain.get("entities") or {}
    if ents.get("competitors"):
        rows.append(f"  competitors: {_join(ents.get('competitors'))}")
    if ents.get("feature_names"):
        rows.append(f"  real features: {_join(ents.get('feature_names'))}")
    if brain.get("icp_vocabulary"):
        rows.append(f"  ICP vocabulary (write in these words): {_join(brain.get('icp_vocabulary'), 10)}")
    voice = brain.get("voice") or {}
    if voice.get("pains"):
        rows.append(f"  VOICE — real pains (verbatim from forums): {_join(voice.get('pains'), 5)}")
    if voice.get("alternative_gripes"):
        gripes = "; ".join(
            f"{g.get('alternative', '')}: {g.get('gripe', '')}"
            for g in voice["alternative_gripes"][:4]
            if isinstance(g, dict) and g.get("gripe")
        )
        if gripes:
            rows.append(f"  VOICE — what they dislike about alternatives: {gripes}")
    if voice.get("vocabulary"):
        rows.append(f"  VOICE — their words: {_join(voice.get('vocabulary'), 10)}")
    if brain.get("disqualifiers"):
        rows.append(f"  NOT for / disqualifiers: {_join(brain.get('disqualifiers'))}")
    return strip_stray_cjk("\n".join(rows))
