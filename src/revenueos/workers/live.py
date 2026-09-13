"""Workers that read the connected accounts (not what a stranger can read) and turn the numbers into
actions with executors that change things.

    billing    Stripe → revenue this month, MRR, customers, open invoices (metrics; RESULTS pipeline is real money)
    analytics  Search Console + GA4 → low-CTR queries (title/snippet rewrite, deployable), channel results
    ads-live   Google Ads + Meta → campaigns spending with no results → ad_waste actions with ads_pause / ads_budget

Each refuses quietly when its connection is missing and says so in one line; nothing is inferred.
"""
from __future__ import annotations

from typing import Any

from ..connections import ConnectionStore, PermissionDenied
from ..context import BusinessContext
from ..llm import LLM
from ..paths import Workspace
from ..store import Store
from . import WorkerResult, ads_controls, ads_terms


class BillingWorker:
    name = "billing"
    description = "Read Stripe: revenue, MRR, customers, open invoices — the real money behind RESULTS."
    upstream = "Stripe API (connections/stripe_conn.py)"

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult:
        from ..connections import stripe_conn

        cs = ConnectionStore(ws)
        conn = cs.get("stripe")
        if not conn:
            return WorkerResult(ok=True, summary="Stripe not connected.", details={"note": "revenueos connect stripe --key sk_…"})
        s = stripe_conn.summary(conn)
        for k in ("customers", "active_subscriptions", "mrr", "revenue_30d", "charges_30d", "open_invoices", "open_invoice_total"):
            store.record_metric(f"stripe_{k}", float(s.get(k) or 0), currency=s.get("currency"), livemode=s.get("livemode"))
        cur = (s.get("currency") or "usd").upper()
        return WorkerResult(ok=True, summary=(f"Stripe ({'live' if s.get('livemode') else 'test'}): {s['customers']} customers · "
                                              f"{s['active_subscriptions']} active subscriptions · MRR {s['mrr']:.2f} {cur} · "
                                              f"revenue 30d {s['revenue_30d']:.2f} {cur} · {s['open_invoices']} open invoices"),
                            details=s)


class AnalyticsWorker:
    name = "analytics"
    description = "Read Search Console and GA4 for the connected site: queries losing clicks, channel results."
    upstream = "Google APIs (connections/google.py)"

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult:
        from ..connections import google

        cs = ConnectionStore(ws)
        if not cs.get("google"):
            return WorkerResult(ok=True, summary="Google not connected.", details={"note": "revenueos connect google"})
        site = ctx.website or ""
        created = 0
        details: dict[str, Any] = {}
        try:
            gsc = google.search_console(cs, site)
            store.record_metric("gsc_clicks_28d", float(gsc["clicks"]), site=site)
            store.record_metric("gsc_impressions_28d", float(gsc["impressions"]), site=site)
            details["gsc"] = {"clicks": gsc["clicks"], "impressions": gsc["impressions"], "rows": len(gsc["rows"])}
            for opp in google.search_opportunities(gsc):
                aid = store.create_action(
                    "seo_opportunity", f"SEO: low click-through for '{opp['query']}' — {opp['page']}", opp["why"],
                    run_id=run_id, source_url=opp["page"], dedupe_key=f"gsc:low_ctr:{opp['page']}:{opp['query']}",
                    context={"kind": "low_ctr_query", "skill": "seo/title-meta-rewriter", "executor": "run_skill",
                             "skill_input": opp["why"] + " Rewrite the title and meta description for this page so the query is answered in the snippet.",
                             "before": {"ctr": opp["ctr"], "clicks": opp["clicks"], "impressions": opp["impressions"]}})
                created += 1 if aid else 0
        except PermissionDenied as e:
            details["gsc"] = str(e)
        prop = (ctx.config.get("google") or {}).get("ga4_property")
        if prop:
            try:
                ga = google.ga4_report(cs, str(prop))
                store.record_metric("ga4_sessions_28d", float(ga["sessions"]), site=site)
                store.record_metric("ga4_conversions_28d", float(ga["conversions"]), site=site)
                details["ga4"] = {"sessions": ga["sessions"], "conversions": ga["conversions"], "revenue": ga["revenue"]}
            except PermissionDenied as e:
                details["ga4"] = str(e)
        g = details.get("gsc") if isinstance(details.get("gsc"), dict) else None
        head = f"Search Console: {g['clicks']:.0f} clicks / {g['impressions']:.0f} impressions (28d)" if g else f"Search Console: {details.get('gsc')}"
        return WorkerResult(ok=True, summary=f"{head}; {created} query opportunit{'y' if created == 1 else 'ies'} queued.",
                            actions_created=created, details=details)


def google_term_evidence(cs: ConnectionStore) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None, str]:
    """The keyword, search-term and negative reads; each may fail on its own (scope, API version) without losing the campaign audit."""
    from ..connections import google

    kws: list[dict[str, Any]] = []
    terms: list[dict[str, Any]] = []
    negs: dict[str, Any] | None = None
    failed = []
    for name, call in (("keywords", google.ads_keywords), ("search terms", google.ads_search_terms), ("negatives", google.ads_negative_keywords)):
        try:
            got = call(cs)
        except Exception as exc:  # noqa: BLE001 — one evidence read must not sink the others
            failed.append(f"{name}: {type(exc).__name__}: {str(exc)[:80]}")
            continue
        if name == "keywords":
            kws = got
        elif name == "search terms":
            terms = got
        else:
            negs = got
    return kws, terms, negs, (" (not read — " + "; ".join(failed) + ")" if failed else "")


def queue_search_term_waste(store: Store, run_id: int, platform: str, terms: list[dict[str, Any]], window_end: str, currency: str, min_spend: float) -> int:
    """One action per platform and window: the search terms that spent, converted nothing and are not excluded. Deterministic; no model."""
    waste = ads_terms.search_term_waste(terms, min_spend)
    if not waste:
        return 0
    top = waste[:25]
    total = round(sum(t["cost"] for t in waste), 2)
    listing = "\n".join(f"- '{t['text']}' ({t['match_type']}, {t['campaign_name'] or t['campaign_id']}): {t['cost']:.2f} {currency}, {t['clicks']} clicks, 0 conversions" for t in top)
    aid = store.create_action(
        "campaign_attention", f"{platform.title()} ads: {len(waste)} search terms spent {total:.2f} {currency} with no conversions and no exclusion",
        f"These search terms triggered ads, cost money and converted nothing in the window; none is excluded yet. Adding them as negatives stops that spend.\n\n{listing}",
        run_id=run_id, dedupe_key=f"ads:{platform}:search_term_waste:{window_end}",
        context={"platform": platform, "kind": "search_term_waste", "executor": "run_skill", "skill": ads_controls.PLATFORM_SKILL.get(platform, "ads/ads-audit"),
                 "before": {"window_end": window_end, "spend": total, "terms": [{"text": t["text"], "ad_group_id": t["ad_group_id"], "campaign_id": t["campaign_id"], "cost": t["cost"]} for t in waste]},
                 "skill_input": (f"These search terms spent {total:.2f} {currency} with zero conversions and are not excluded:\n{listing}\n"
                                 "Draft the negative keywords to add (exact match type per term, campaign or list level, reversible) for the owner to apply; "
                                 "do not invent terms that are not listed.")})
    return 1 if aid else 0


class AdsLiveWorker:
    name = "ads-live"
    description = "Read the connected ad accounts: campaigns spending with no results become pause/budget actions."
    upstream = "Google Ads API, Meta Marketing API (connections/google.py, meta.py)"

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult:
        from ..connections import google, meta

        cs = ConnectionStore(ws)
        created = 0
        parts = []
        min_cost = float((ctx.config.get("ads") or {}).get("waste_min_spend", 50))
        if cs.get("google"):
            try:
                camps = google.ads_campaigns(cs)
                spend = sum(c["cost"] for c in camps)
                store.record_metric("google_ads_spend_28d", spend)
                for c in google.ads_waste(camps, min_cost):
                    aid = store.create_action(
                        "ad_waste", f"Google Ads: '{c['name']}' spent {c['cost']:.2f} with 0 conversions (28d)",
                        f"Campaign {c['name']} is enabled, spent {c['cost']:.2f} over 28 days for {c['clicks']} clicks and no conversions. Pausing stops the spend today.",
                        run_id=run_id, dedupe_key=f"ads:google:waste:{c['id']}",
                        context={"executor": "ads_pause", "platform": "google", "campaign_id": c["id"], "campaign_name": c["name"],
                                 "before": {"cost_28d": c["cost"], "conversions": c["conversions"], "status": c["status"]}})
                    created += 1 if aid else 0
                snap = ads_controls.snapshot_from_campaigns("google", cs.get("google").meta.get("ads_customer_id") or "google", (ctx.config.get("ads") or {}).get("currency", "USD"), camps)
                kws, terms, negs, ev_note = google_term_evidence(cs)
                ads_terms.attach(snap, keywords=kws, search_terms=terms, negatives=negs)
                if kws or terms or negs:
                    ads_terms.write_terms(ws.exports, ads_terms.terms_record("google", snap["window"], kws, terms, negs, "google-ads-api"))
                created += queue_search_term_waste(store, run_id, "google", terms, snap["window"]["end"], snap["currency"],
                                                   float((ctx.config.get("ads") or {}).get("search_term_min_spend", 20)))
                ctl = ads_controls.audit(ws, store, ctx, llm, run_id, "google", snap, "google-ads-api")
                created += ctl.get("created", 0)
                parts.append(f"Google Ads {spend:.2f} spend, {len(camps)} campaigns, {len(kws)} keywords, {len(terms)} search terms"
                             + (f", {ctl['checked']} controls: {ctl['fail']} fail" if "checked" in ctl else f" ({ctl.get('note')})") + ev_note)
            except PermissionDenied as e:
                parts.append(f"Google Ads: {e}")
        if cs.get("meta"):
            try:
                rows = meta.campaigns(cs)
                spend = sum(c["spend"] for c in rows)
                store.record_metric("meta_ads_spend_28d", spend)
                for c in meta.waste(rows, min_cost):
                    aid = store.create_action(
                        "ad_waste", f"Meta Ads: '{c['name']}' spent {c['spend']:.2f} with 0 results (28d)",
                        f"Campaign {c['name']} is active, spent {c['spend']:.2f} over 28 days for {c['clicks']} clicks and no results. Pausing stops the spend today.",
                        run_id=run_id, dedupe_key=f"ads:meta:waste:{c['id']}",
                        context={"executor": "ads_pause", "platform": "meta", "campaign_id": c["id"], "campaign_name": c["name"],
                                 "before": {"spend_28d": c["spend"], "results": c["results"], "status": c["status"]}})
                    created += 1 if aid else 0
                snap = ads_controls.snapshot_from_campaigns("meta", cs.get("meta").meta.get("ad_account") or "meta", (ctx.config.get("ads") or {}).get("currency", "USD"), rows)
                ctl = ads_controls.audit(ws, store, ctx, llm, run_id, "meta", snap, "meta-marketing-api")
                created += ctl.get("created", 0)
                parts.append(f"Meta {spend:.2f} spend, {len(rows)} campaigns" + (f", {ctl['checked']} controls: {ctl['fail']} fail" if "checked" in ctl else f" ({ctl.get('note')})"))
            except PermissionDenied as e:
                parts.append(f"Meta: {e}")
        if not parts:
            return WorkerResult(ok=True, summary="No ad account connected.", details={"note": "revenueos connect google / meta"})
        return WorkerResult(ok=True, summary="; ".join(parts) + f"; {created} wasting campaign(s) queued to pause.", actions_created=created)
