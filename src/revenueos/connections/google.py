"""Google: Search Console (queries and pages), GA4 (sessions, conversions), Calendar (book the call) and
Google Ads (campaigns, pause, budget). One OAuth connection; each API is a scope the owner granted.

Live use needs an OAuth client (GOOGLE_OAUTH_CLIENT_ID / _SECRET) that the owner registers once in the
Google Cloud console, and for Google Ads a developer token (GOOGLE_ADS_DEVELOPER_TOKEN) plus the customer
id. Every request shape here is the documented REST form so a MockTransport test exercises the real code.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from . import Connection, ConnectionStore, PermissionDenied, require
from .oauth import access_token, loopback_flow

NAME = "google"
GSC = "https://searchconsole.googleapis.com/webmasters/v3"
GA4 = "https://analyticsdata.googleapis.com/v1beta"
CAL = "https://www.googleapis.com/calendar/v3"
ADS = "https://googleads.googleapis.com/v18"


def describe() -> dict[str, Any]:
    return {
        "label": "Google (Search Console, GA4, Calendar, Google Ads)",
        "reads": "search queries and pages (Search Console), sessions and conversions (GA4), campaigns and spend (Google Ads)",
        "writes": "book a call on your calendar; pause a campaign or change its daily budget (needs 'allow changes')",
        "needs": "an OAuth client (GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET); Google Ads also needs GOOGLE_ADS_DEVELOPER_TOKEN and GOOGLE_ADS_CUSTOMER_ID",
        "how": "revenueos connect google  (opens the consent page in your browser)",
    }


def ready() -> bool:
    return bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID") and os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET"))


def missing() -> list[str]:
    out = [] if ready() else ["GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET (OAuth client from console.cloud.google.com)"]
    if not os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN"):
        out.append("GOOGLE_ADS_DEVELOPER_TOKEN (Google Ads API centre) — only for Google Ads")
    return out


DEFAULT_SCOPES = ["identity", "searchconsole.read", "analytics.read", "calendar.write"]


def connect(store: ConnectionStore, wanted: list[str] | None = None, *, tokens: dict[str, Any] | None = None,
            client: httpx.Client | None = None, open_browser: bool = True) -> Connection:
    wanted = wanted or DEFAULT_SCOPES
    toks = tokens or loopback_flow(NAME, wanted, open_browser=open_browser, client=client)
    c = client or httpx.Client(timeout=30)
    who = c.get("https://openidconnect.googleapis.com/v1/userinfo", headers={"Authorization": f"Bearer {toks['access_token']}"})
    email = who.json().get("email", "") if who.status_code == 200 else ""
    granted = (toks.get("scope") or "").split()
    conn = Connection(provider=NAME, account=email or "google", scopes=granted or wanted, secrets={"tokens": toks},
                      meta={"wanted": wanted, "ads_customer_id": os.environ.get("GOOGLE_ADS_CUSTOMER_ID")})
    return store.put(conn)


def _bearer(store: ConnectionStore, conn: Connection, client: httpx.Client | None) -> dict[str, str]:
    tok, toks = access_token(NAME, conn.secrets["tokens"], client)
    if toks is not conn.secrets["tokens"]:
        conn.secrets["tokens"] = toks
        store.put(conn)
    return {"Authorization": f"Bearer {tok}"}


def _has(conn: Connection, scope_url: str) -> bool:
    return any(scope_url in s for s in conn.scopes)


# ── Search Console ────────────────────────────────────────────────────────────
def search_console(store: ConnectionStore, site_url: str, days: int = 28, client: httpx.Client | None = None) -> dict[str, Any]:
    conn = require(store, NAME)
    if not _has(conn, "webmasters"):
        raise PermissionDenied("Search Console was not granted on this Google connection")
    c = client or httpx.Client(timeout=30)
    h = _bearer(store, conn, client)
    end = datetime.now(UTC).date() - timedelta(days=2)
    start = end - timedelta(days=days)
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["query", "page"], "rowLimit": 250}
    r = c.post(f"{GSC}/sites/{httpx.URL(site_url).host and site_url.replace('/', '%2F').replace(':', '%3A')}/searchAnalytics/query", json=body, headers=h)
    if r.status_code != 200:
        raise RuntimeError(f"Search Console: {r.status_code} {r.text[:200]}")
    rows = r.json().get("rows", [])
    out = [{"query": rw["keys"][0], "page": rw["keys"][1], "clicks": rw.get("clicks", 0), "impressions": rw.get("impressions", 0),
            "ctr": rw.get("ctr", 0.0), "position": rw.get("position", 0.0)} for rw in rows]
    return {"site": site_url, "days": days, "rows": out,
            "clicks": sum(x["clicks"] for x in out), "impressions": sum(x["impressions"] for x in out)}


def search_opportunities(gsc: dict[str, Any]) -> list[dict[str, Any]]:
    """Queries with real impressions and almost no clicks on page one/two: a title or snippet problem, measurable by CTR."""
    out = []
    for r in gsc.get("rows", []):
        if r["impressions"] >= 100 and r["position"] <= 20 and r["ctr"] < 0.02:
            out.append({"kind": "low_ctr_query", "query": r["query"], "page": r["page"], "impressions": r["impressions"],
                        "clicks": r["clicks"], "ctr": r["ctr"], "position": r["position"],
                        "why": f"'{r['query']}' shows {r['impressions']} times at position {r['position']:.0f} and gets {r['clicks']} clicks (CTR {r['ctr']*100:.1f}%)."})
    return sorted(out, key=lambda x: -x["impressions"])[:10]


# ── GA4 ───────────────────────────────────────────────────────────────────────
def ga4_report(store: ConnectionStore, property_id: str, days: int = 28, client: httpx.Client | None = None) -> dict[str, Any]:
    conn = require(store, NAME)
    if not _has(conn, "analytics"):
        raise PermissionDenied("GA4 was not granted on this Google connection")
    c = client or httpx.Client(timeout=30)
    h = _bearer(store, conn, client)
    body = {"dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "yesterday"}],
            "dimensions": [{"name": "sessionDefaultChannelGroup"}],
            "metrics": [{"name": "sessions"}, {"name": "conversions"}, {"name": "totalRevenue"}]}
    r = c.post(f"{GA4}/properties/{property_id}:runReport", json=body, headers=h)
    if r.status_code != 200:
        raise RuntimeError(f"GA4: {r.status_code} {r.text[:200]}")
    rows = []
    for rw in r.json().get("rows", []):
        vals = [float(v.get("value") or 0) for v in rw.get("metricValues", [])]
        rows.append({"channel": rw["dimensionValues"][0]["value"], "sessions": vals[0], "conversions": vals[1] if len(vals) > 1 else 0.0,
                     "revenue": vals[2] if len(vals) > 2 else 0.0})
    return {"property": property_id, "days": days, "rows": rows, "sessions": sum(x["sessions"] for x in rows),
            "conversions": sum(x["conversions"] for x in rows), "revenue": sum(x["revenue"] for x in rows)}


# ── Calendar: book the call ───────────────────────────────────────────────────
def book_call(store: ConnectionStore, *, attendee_email: str, subject: str, start: datetime, minutes: int = 15,
              description: str = "", client: httpx.Client | None = None) -> dict[str, Any]:
    conn = require(store, NAME, "write")
    if not _has(conn, "calendar"):
        raise PermissionDenied("Calendar was not granted on this Google connection")
    c = client or httpx.Client(timeout=30)
    h = _bearer(store, conn, client)
    end = start + timedelta(minutes=minutes)
    body = {"summary": subject, "description": description, "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()},
            "attendees": [{"email": attendee_email}], "conferenceData": {"createRequest": {"requestId": f"revenueos-{int(start.timestamp())}"}}}
    r = c.post(f"{CAL}/calendars/primary/events", params={"sendUpdates": "all", "conferenceDataVersion": 1}, json=body, headers=h)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Calendar: {r.status_code} {r.text[:200]}")
    ev = r.json()
    return {"event_id": ev.get("id"), "html_link": ev.get("htmlLink"), "meet": (ev.get("hangoutLink") or ""), "start": start.isoformat()}


# ── Google Ads ────────────────────────────────────────────────────────────────
def _ads_headers(store: ConnectionStore, conn: Connection, client: httpx.Client | None) -> dict[str, str]:
    dev = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN")
    if not dev:
        raise PermissionDenied("Google Ads needs GOOGLE_ADS_DEVELOPER_TOKEN")
    h = _bearer(store, conn, client)
    h["developer-token"] = dev
    login = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
    if login:
        h["login-customer-id"] = login
    return h


def ads_campaigns(store: ConnectionStore, customer_id: str | None = None, days: int = 28, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    conn = require(store, NAME)
    cid = (customer_id or conn.meta.get("ads_customer_id") or "").replace("-", "")
    if not cid:
        raise PermissionDenied("no Google Ads customer id (GOOGLE_ADS_CUSTOMER_ID)")
    c = client or httpx.Client(timeout=60)
    q = (f"SELECT campaign.id, campaign.name, campaign.status, campaign_budget.amount_micros, metrics.cost_micros, "
         f"metrics.conversions, metrics.clicks, metrics.impressions FROM campaign WHERE segments.date DURING LAST_{days}_DAYS "
         "AND campaign.status != 'REMOVED'")
    r = c.post(f"{ADS}/customers/{cid}/googleAds:searchStream", json={"query": q}, headers=_ads_headers(store, conn, client))
    if r.status_code != 200:
        raise RuntimeError(f"Google Ads: {r.status_code} {r.text[:200]}")
    out: dict[str, dict[str, Any]] = {}
    for batch in r.json() if isinstance(r.json(), list) else [r.json()]:
        for row in batch.get("results", []):
            camp, m, b = row.get("campaign", {}), row.get("metrics", {}), row.get("campaignBudget", {})
            rec = out.setdefault(str(camp.get("id")), {"id": str(camp.get("id")), "name": camp.get("name"), "status": camp.get("status"),
                                                        "daily_budget": float(b.get("amountMicros") or 0) / 1e6, "cost": 0.0, "conversions": 0.0, "clicks": 0, "impressions": 0})
            rec["cost"] += float(m.get("costMicros") or 0) / 1e6
            rec["conversions"] += float(m.get("conversions") or 0)
            rec["clicks"] += int(m.get("clicks") or 0)
            rec["impressions"] += int(m.get("impressions") or 0)
    return list(out.values())


def ads_waste(campaigns: list[dict[str, Any]], min_cost: float = 50.0) -> list[dict[str, Any]]:
    """Enabled campaigns that spent real money over the window with zero conversions."""
    return [c for c in campaigns if c.get("status") == "ENABLED" and c["cost"] >= min_cost and c["conversions"] == 0]


def ads_pause(store: ConnectionStore, campaign_id: str, customer_id: str | None = None, client: httpx.Client | None = None) -> dict[str, Any]:
    conn = require(store, NAME, "write")
    cid = (customer_id or conn.meta.get("ads_customer_id") or "").replace("-", "")
    c = client or httpx.Client(timeout=60)
    body = {"operations": [{"updateMask": "status", "update": {"resourceName": f"customers/{cid}/campaigns/{campaign_id}", "status": "PAUSED"}}]}
    r = c.post(f"{ADS}/customers/{cid}/campaigns:mutate", json=body, headers=_ads_headers(store, conn, client))
    if r.status_code != 200:
        raise RuntimeError(f"Google Ads pause: {r.status_code} {r.text[:200]}")
    return {"campaign": campaign_id, "status": "PAUSED", "results": r.json().get("results", [])}


def ads_set_budget(store: ConnectionStore, budget_resource: str, daily_amount: float, customer_id: str | None = None,
                   client: httpx.Client | None = None) -> dict[str, Any]:
    conn = require(store, NAME, "write")
    cid = (customer_id or conn.meta.get("ads_customer_id") or "").replace("-", "")
    c = client or httpx.Client(timeout=60)
    body = {"operations": [{"updateMask": "amountMicros", "update": {"resourceName": budget_resource, "amountMicros": str(int(daily_amount * 1e6))}}]}
    r = c.post(f"{ADS}/customers/{cid}/campaignBudgets:mutate", json=body, headers=_ads_headers(store, conn, client))
    if r.status_code != 200:
        raise RuntimeError(f"Google Ads budget: {r.status_code} {r.text[:200]}")
    return {"budget": budget_resource, "daily_amount": daily_amount}
