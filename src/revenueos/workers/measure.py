"""MEASURE — what measurable result occurred after an action was executed.

Runs after execution (on the orchestrator's schedule, or `revenueos run measure`) and records
one `outcomes` row per executed action, by kind:

  * seo_opportunity  — re-crawl the page: was the missing title/description/thin page fixed?
                       (`meta_description_fixed`, `title_fixed`, `page_body_chars` before→after)
  * follow_up (send) — replies / bounces on the send row (`replied`, from the inbox worker)
  * ad_waste / campaign_attention — the campaign's spend and conversions in the next export
                       window (`campaign_spend_delta`, `campaign_conversions_delta`)
  * content_opportunity — the deliverable exists in data/outputs (`produced`, not yet published)
  * publish_post        — the published WordPress URL is live and carries the deliverable's title
                       (`published` 0→1)
  * prospect / market_signal — unmeasurable until a downstream action exists (recorded as such)

Outcomes are honest: `pending` until evidence exists, `measured` when it does, `no_effect`
when the evidence shows nothing changed, `unmeasurable` when nothing in RevenueOS can observe
the effect (the note says what would).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlparse

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


def local_host(url: str) -> str | None:
    """The loopback/private host string `url` targets, or None for a public address. String/IP-literal
    checks only, no DNS resolution: a re-check against 127.0.0.0/8, ::1, 'localhost', a .local name, or
    an RFC1918 range (10/8, 172.16/12, 192.168/16) is not a check of the live site and must not be
    recorded as a measured result."""
    host = (urlparse(url if "//" in url else f"//{url}").hostname or "").lower()
    if not host:
        return None
    if host == "localhost" or host.endswith(".local"):
        return host
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return host
    return None


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
    host = local_host(url)
    if host:
        return "unmeasurable", {"note": f"re-checked against a local address ({host}), not the live site; "
                                        "set `website` in revenueos.yaml to the public URL to measure it"}
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


def measure_search_terms(ws: Workspace, action: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Search-term waste is measured by the next read of the same terms: excluded, or spending less, → measured; still spending → no_effect."""
    from .ads_terms import read_terms

    c = action["context"]
    before = c.get("before") or {}
    rec = read_terms(ws.exports, c.get("platform"))
    if not rec:
        return "pending", {"note": "no newer search-term read to compare against"}
    if rec["window"]["end"] <= before.get("window_end", ""):
        return "pending", {"note": "no newer search-term read yet"}
    wanted = {(t["text"], t.get("ad_group_id")) for t in before.get("terms", [])}
    now = {(t["text"], t.get("ad_group_id")): t for t in rec.get("search_terms", [])}
    excluded = sum(1 for k in wanted if k in now and now[k]["status"] in ("excluded", "added_excluded"))
    spend_after = round(sum(now[k]["cost"] for k in wanted if k in now and now[k]["status"] not in ("excluded", "added_excluded")), 2)
    spend_before = float(before.get("spend") or 0)
    after = {"window_end": rec["window"]["end"], "spend": spend_after, "excluded": excluded, "terms_seen": sum(1 for k in wanted if k in now)}
    improved = excluded > 0 or spend_after < spend_before
    note = f"{excluded} of {len(wanted)} terms now excluded; the rest spent {spend_after:.2f} in the new window (was {spend_before:.2f})"
    return ("measured" if improved else "no_effect"), {"metric": "wasted_search_term_spend", "before_value": spend_before, "after_value": spend_after,
                                                        "before": {"window_end": before.get("window_end"), "spend": spend_before, "terms": len(wanted)}, "after": after, "note": note}


def measure_control(ws: Workspace, action: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A failing ads control is measured by the next control audit: pass → measured, still fail → no_effect."""
    c = action["context"]
    path = ws.exports / f"ads-{c.get('platform')}.controls.json"
    if not path.exists():
        return "pending", {"note": "no control audit since the action"}
    data = json.loads(path.read_text(encoding="utf-8"))
    before_end = (c.get("before") or {}).get("window_end")
    if data.get("window", {}).get("end") and before_end and data["window"]["end"] <= before_end:
        return "pending", {"note": "no newer control audit yet"}
    f = next((x for x in data.get("findings", []) if x.get("control_id") == c.get("control_id")), None)
    if not f:
        return "pending", {"note": "control not in the latest audit"}
    passed = f.get("status") == "pass"
    return ("measured" if passed else "no_effect"), {"metric": "control_pass", "before_value": 0.0, "after_value": float(passed),
                                                    "before": c.get("before"), "after": {"status": f.get("status"), "window_end": data.get("window", {}).get("end")}}


PRODUCED_NOTE = "deliverable produced, not published; publication and audience effect unmeasured — connect the channel or record the publication URL to measure it"


def _deliverable_title(path) -> str:
    """The first markdown heading in the deliverable, used only to look for it on the published page."""
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r"^#{1,2}\s+(.+)$", line.strip())
        if m:
            return m.group(1).strip()
    return ""


def _check_published(url: str, title: str) -> tuple[bool, str]:
    """Fetch `url`; (True, '') when it's a 200 page whose body contains the deliverable's title,
    (False, reason) otherwise. Never raises — a fetch failure is just evidence it isn't measurable yet."""
    import httpx

    try:
        r = httpx.get(url, timeout=15.0, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; RevenueOS/0.1; +https://unempyd.github.io/revenueos/)"})
    except httpx.HTTPError as e:
        return False, f"publication URL unreachable ({type(e).__name__})"
    if r.status_code != 200:
        return False, f"publication URL returned HTTP {r.status_code}"
    if title and title.lower() not in r.text.lower():
        return False, "publication URL is live but does not contain the deliverable's title yet"
    return True, ""


def measure_publish(ws: Workspace, action: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A publish_post action (executors.execute_publish_post) is measured by fetching its own
    published_url: a live 200 page carrying the deliverable's title is 'published' 0→1 (measured);
    a 404/410 or a page missing the title is no_effect (published, but the evidence says it didn't
    take, or was taken down); a fetch failure is not evidence either way — pending, retried next run.

    Search Console page-level clicks for the published URL would be a second, later outcome
    (`search_clicks`, before 0 → after N) — `connections.google.search_console` already returns
    per-page rows, so the read itself is a one-call addition. It is left out here: this worker's
    per-action loop stamps an action 'final' the moment one outcome lands `measured`, so a second,
    slower-arriving metric on the same action needs its own tracking (not reusing this call), which
    is a real design change, not a small addition. Recorded here as unmeasurable-for-now so that is
    never silently implied to be tracked."""
    c = action.get("context") or {}
    published_url = c.get("published_url")
    if not published_url:
        return "pending", {"note": "not published yet"}
    title = ""
    deliverable = c.get("deliverable")
    if deliverable:
        path = ws.root / deliverable
        if path.exists():
            title = _deliverable_title(path)
    ok, reason = _check_published(published_url, title)
    after = {"published_url": published_url,
             "note": "search_clicks: needs a Search Console read filtered to this URL once the page is indexed; not tracked yet"}
    if ok:
        return "measured", {"metric": "published", "before_value": 0.0, "after_value": 1.0, "before": {"published": 0.0}, "after": after}
    if reason.startswith("publication URL unreachable"):
        return "pending", {"note": reason}
    return "no_effect", {"metric": "published", "before_value": 0.0, "after_value": 0.0, "before": {"published": 0.0},
                         "after": after, "note": reason}


def measure_content(ws: Workspace, action: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    outputs = sorted(ws.outputs.glob(f"*-{action['id']}.md"))
    if not outputs:
        return "pending", {"note": "no deliverable in data/outputs yet"}
    path = outputs[-1]
    after = {"path": str(path.relative_to(ws.root)), "chars": path.stat().st_size}
    ctx = action.get("context") or {}
    published_url = (ctx.get("published_url") or ctx.get("url") or (ctx.get("after") or {}).get("published_url"))
    if not published_url:
        return "produced", {"metric": "deliverable_produced", "before_value": 0.0, "after_value": 1.0, "after": after, "note": PRODUCED_NOTE}
    host = local_host(published_url)
    if host:
        return "produced", {"metric": "deliverable_produced", "before_value": 0.0, "after_value": 1.0, "after": after,
                            "note": f"publication URL ({host}) is a local address, not the live site; {PRODUCED_NOTE}"}
    ok, reason = _check_published(published_url, _deliverable_title(path))
    if ok:
        return "measured", {"metric": "published", "before_value": 0.0, "after_value": 1.0,
                            "after": {**after, "published_url": published_url}}
    return "produced", {"metric": "deliverable_produced", "before_value": 0.0, "after_value": 1.0,
                        "after": {**after, "published_url": published_url}, "note": f"{reason}; {PRODUCED_NOTE}"}


class MeasureWorker:
    name = "measure"
    description = "Record what measurable result occurred for every executed action."
    upstream = "pulse-cmo crawl (re-crawl), claude-ads aggregate, ai-sales-agent send rows"

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult:
        measured = pending = unchanged = produced = 0
        for action in store.executed_actions():
            last = action.get("outcome")
            # one-time correction: outcomes recorded 'measured'/'deliverable_written' by the pre-honesty
            # measure_content (file existence only, no publication evidence) are re-labelled 'produced'
            # on the next run; every run after that is a stable 'produced' outcome and skipped as final.
            stale_deliverable = bool(last) and last["status"] == "measured" and last.get("metric") == "deliverable_written"
            # one-time correction: an SEO result 'measured' against a loopback/private host before that
            # was caught is re-checked once (measure_seo now returns 'unmeasurable' for it); once it does,
            # the status is no longer 'measured' and this no longer matches, so it is stable from then on.
            stale_local_seo = (bool(last) and last["status"] == "measured" and action["action_type"] == "seo_opportunity"
                               and bool(local_host(action.get("source_url") or "")))
            if last and last["status"] in ("measured", "unmeasurable") and not (stale_deliverable or stale_local_seo):
                continue  # final
            atype = action["action_type"]
            try:
                if atype == "seo_opportunity":
                    status, data = measure_seo(store, action)
                elif atype == "follow_up" and action["context"].get("executor") == "send_email":
                    status, data = measure_send(store, action)
                elif action["context"].get("kind") == "control_fail":
                    status, data = measure_control(ws, action)
                elif action["context"].get("kind") == "search_term_waste":
                    status, data = measure_search_terms(ws, action)
                elif atype in ("ad_waste", "campaign_attention"):
                    status, data = measure_ads(ws, store, action)
                elif action["context"].get("executor") == "publish_post":
                    status, data = measure_publish(ws, action)
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
            elif status == "no_effect":
                unchanged += 1
            elif status == "produced":
                produced += 1
        return WorkerResult(ok=True, summary=f"{measured} result(s) measured, {produced} produced (not published), "
                            f"{unchanged} re-checked with no change yet, {pending} still pending.", actions_created=0,
                            details={"measured": measured, "produced": produced, "pending": pending})
