"""MEASURE — what measurable result occurred after an action was executed.

Runs after execution (on the orchestrator's schedule, or `revenueos run measure`) and records
one `outcomes` row per executed action, by kind:

  * seo_opportunity  — re-crawl the page: was the missing title/description/thin page fixed?
                       (`meta_description_fixed`, `title_fixed`, `page_body_chars` before→after)
  * follow_up (send) — replies / bounces on the send row (`replied`, from the inbox worker)
  * ad_waste / campaign_attention — the campaign's spend and conversions in the next export
                       window (`campaign_spend_delta`, `campaign_conversions_delta`)
  * content_opportunity — the deliverable exists in data/outputs (`deliverable_written`)
  * prospect / market_signal — unmeasurable until a downstream action exists (recorded as such)

Outcomes are honest: `pending` until evidence exists, `measured` when it does, `no_effect`
when the evidence shows nothing changed, `unmeasurable` when nothing in RevenueOS can observe
the effect (the note says what would).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from ..context import BusinessContext
from ..llm import LLM
from ..paths import Workspace
from ..store import Store
from ..vendor.pulse.crawl import crawl_website
from . import WorkerResult
from .ads_audit import aggregate


def _crawl(url: str, max_pages: int = 10):
    """pulse decorates crawl_website with @tool; call the underlying coroutine (tests may monkeypatch a plain function)."""
    fn = getattr(crawl_website, "fn", crawl_website)
    return fn(url, max_pages=max_pages)


def page_snapshot(url: str) -> dict[str, Any]:
    raw = asyncio.run(_crawl(url, max_pages=1))
    data = json.loads(raw)
    page = (data.get("pages") or [{}])[0]
    meta = page.get("meta") or {}
    return {"title": (meta.get("title") or "").strip(), "description": (meta.get("description") or "").strip(),
            "body_chars": len(page.get("excerpt") or ""), "ok": bool(data.get("ok"))}


def measure_seo(store: Store, action: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    kind = action["context"].get("kind")
    url = action.get("source_url")
    if not url or kind in ("authority_gap", "indexing_surface"):
        return "unmeasurable", {"note": "needs Search Console clicks (connect google-search-console) to measure"}
    before = action["context"].get("before") or {}
    if kind == "no_sitemap":
        data = json.loads(asyncio.run(_crawl(url, max_pages=1)))
        if not data.get("ok"):
            return "pending", {"note": "site unreachable at measurement time"}
        from .seo import sitemap_under_path

        found = bool(data.get("sitemap_found")) or sitemap_under_path(url)
        return ("measured" if found else "no_effect"), {"metric": "sitemap_present", "before_value": 0.0, "after_value": float(found),
                                                        "before": before, "after": {"sitemap_found": found}}
    if kind in SIGNAL_KINDS:
        from .seo import homepage_signals

        signal, metric = SIGNAL_KINDS[kind]
        sig = homepage_signals(url)
        if not sig.get("ok"):
            return "pending", {"note": "homepage unreachable at measurement time", "after": sig}
        present = bool(sig.get(signal))
        return ("measured" if present else "no_effect"), {"metric": metric, "before_value": 0.0, "after_value": float(present),
                                                          "before": before, "after": {signal: present}}
    after = page_snapshot(url)
    if not after["ok"]:
        return "pending", {"note": "page unreachable at measurement time", "after": after}
    if kind == "missing_description":
        fixed = bool(after["description"])
        return ("measured" if fixed else "no_effect"), {"metric": "meta_description_fixed", "before_value": 0.0, "after_value": float(fixed),
                                                        "before": before, "after": after}
    if kind == "missing_title":
        fixed = bool(after["title"])
        return ("measured" if fixed else "no_effect"), {"metric": "title_fixed", "before_value": 0.0, "after_value": float(fixed),
                                                        "before": before, "after": after}
    if kind == "thin_page":
        before_chars = float(before.get("body_chars") or 0)
        grew = after["body_chars"] > max(before_chars, 300)
        return ("measured" if grew else "no_effect"), {"metric": "page_body_chars", "before_value": before_chars,
                                                       "after_value": float(after["body_chars"]), "before": before, "after": after}
    if kind == "duplicate_title":
        changed = before.get("title") and after["title"] and after["title"].lower() != str(before["title"]).lower()
        return ("measured" if changed else "no_effect"), {"metric": "title_changed", "before_value": 0.0, "after_value": float(bool(changed)),
                                                          "before": before, "after": after}
    return "unmeasurable", {"note": f"no measurement defined for {kind}"}


SIGNAL_KINDS = {"phone_not_tappable": ("tel_link", "tel_link_present"), "no_local_schema": ("local_schema", "local_schema_present"),
                "no_canonical": ("canonical", "canonical_present")}


def measure_send(store: Store, action: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    draft_id = action["context"].get("draft_id")
    send = next((s for s in store.recent_sends() if s["draft_id"] == draft_id), None)
    if not send:
        return "pending", {"note": "no send recorded yet"}
    if send["bounced"]:
        return "measured", {"metric": "bounced", "before_value": 0.0, "after_value": 1.0, "after": {"send_id": send["id"]}}
    if send["replied_at"]:
        lead = store.get_lead(send["lead_id"]) or {}
        return "measured", {"metric": "replied", "before_value": 0.0, "after_value": 1.0,
                            "after": {"replied_at": send["replied_at"], "lead_status": lead.get("status"), "deal_value": lead.get("deal_value")}}
    return "pending", {"metric": "replied", "before_value": 0.0, "after_value": 0.0, "note": f"sent {send['sent_at']}, no reply yet"}


def measure_ads(ws: Workspace, store: Store, action: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    c = action["context"]
    path = ws.exports / f"ads-{c.get('platform')}.csv"
    if not path.exists():
        return "pending", {"note": "no newer ad export to compare against"}
    camps = aggregate(path)
    camp = camps.get(c.get("campaign_id"))
    before = c.get("before") or {}
    if not camp:
        return "measured", {"metric": "campaign_present", "before_value": 1.0, "after_value": 0.0, "note": "campaign no longer in the export (paused or removed)"}
    spend_before = float(before.get("spend") or 0)
    conv_before = float(before.get("conversions") or 0)
    after = {"spend": float(camp["spend"]), "conversions": float(camp["conversions"]), "days": camp["days"]}
    if not before:
        return "pending", {"note": "no baseline recorded on the action", "after": after}
    changed = after["spend"] != spend_before or after["conversions"] != conv_before
    return ("measured" if changed else "no_effect"), {"metric": "campaign_spend_delta", "before_value": spend_before,
                                                       "after_value": after["spend"], "before": before, "after": after}


def measure_content(ws: Workspace, action: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    outputs = sorted(ws.outputs.glob(f"*-{action['id']}.md"))
    if outputs:
        return "measured", {"metric": "deliverable_written", "before_value": 0.0, "after_value": 1.0,
                            "after": {"path": str(outputs[-1].relative_to(ws.root)), "chars": outputs[-1].stat().st_size}}
    return "pending", {"note": "no deliverable in data/outputs yet"}


class MeasureWorker:
    name = "measure"
    description = "Record what measurable result occurred for every executed action."
    upstream = "pulse-cmo crawl (re-crawl), claude-ads aggregate, ai-sales-agent send rows"

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult:
        measured = pending = 0
        for action in store.executed_actions():
            last = action.get("outcome")
            if last and last["status"] in ("measured", "unmeasurable"):
                continue  # final
            atype = action["action_type"]
            try:
                if atype == "seo_opportunity":
                    status, data = measure_seo(store, action)
                elif atype == "follow_up" and action["context"].get("executor") == "send_email":
                    status, data = measure_send(store, action)
                elif atype in ("ad_waste", "campaign_attention"):
                    status, data = measure_ads(ws, store, action)
                elif atype == "content_opportunity" or action["context"].get("executor") == "run_skill":
                    status, data = measure_content(ws, action)
                else:
                    status, data = "unmeasurable", {"note": f"{atype}: measured through the actions it leads to (outreach, content)"}
            except Exception as exc:  # measurement must never break the loop
                status, data = "pending", {"note": f"measurement error: {type(exc).__name__}: {exc}"}
            if last and last["status"] == status and last.get("note") == data.get("note") and last.get("after_value") == data.get("after_value"):
                continue  # nothing new to record
            store.record_outcome(action["id"], status, metric=data.get("metric"), before=data.get("before"), after=data.get("after"),
                                 before_value=data.get("before_value"), after_value=data.get("after_value"), note=data.get("note"))
            if status == "measured":
                measured += 1
            elif status == "pending":
                pending += 1
        return WorkerResult(ok=True, summary=f"{measured} result(s) measured, {pending} still pending.", actions_created=0,
                            details={"measured": measured, "pending": pending})
