"""Meta (Facebook/Instagram) Ads: campaign spend and results by campaign; pause a campaign; set its daily
budget. One OAuth connection with ads_read (and ads_management when the owner allows changes).

Live use needs a Meta app (META_APP_ID / META_APP_SECRET) the owner registers once at developers.facebook.com
and the ad account id (META_AD_ACCOUNT_ID, 'act_…'). Request shapes are the Marketing API's documented forms.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from . import Connection, ConnectionStore, PermissionDenied, require
from .oauth import access_token, loopback_flow

NAME = "meta"
GRAPH = "https://graph.facebook.com/v21.0"


def describe() -> dict[str, Any]:
    return {
        "label": "Meta Ads (Facebook / Instagram)",
        "reads": "campaigns, spend, results and cost per result",
        "writes": "pause a campaign or change its daily budget (needs 'allow changes')",
        "needs": "a Meta app (META_APP_ID / META_APP_SECRET) and the ad account id (META_AD_ACCOUNT_ID)",
        "how": "revenueos connect meta  (opens the consent page in your browser)",
    }


def ready() -> bool:
    return bool(os.environ.get("META_APP_ID") and os.environ.get("META_APP_SECRET"))


def missing() -> list[str]:
    out = [] if ready() else ["META_APP_ID + META_APP_SECRET (app at developers.facebook.com)"]
    if not os.environ.get("META_AD_ACCOUNT_ID"):
        out.append("META_AD_ACCOUNT_ID (act_…)")
    return out


def connect(store: ConnectionStore, wanted: list[str] | None = None, *, tokens: dict[str, Any] | None = None,
            client: httpx.Client | None = None, open_browser: bool = True) -> Connection:
    wanted = wanted or ["identity", "ads.read"]
    toks = tokens or loopback_flow(NAME, wanted, open_browser=open_browser, client=client)
    c = client or httpx.Client(timeout=30)
    me = c.get(f"{GRAPH}/me", params={"access_token": toks["access_token"], "fields": "id,name"})
    ident = me.json().get("name") or me.json().get("id", "meta") if me.status_code == 200 else "meta"
    granted = ["ads_read"] + (["ads_management"] if "ads.write" in wanted else [])
    conn = Connection(provider=NAME, account=str(ident), scopes=granted, secrets={"tokens": toks},
                      meta={"ad_account": os.environ.get("META_AD_ACCOUNT_ID")})
    return store.put(conn)


def _token(store: ConnectionStore, conn: Connection, client: httpx.Client | None) -> str:
    tok, toks = access_token(NAME, conn.secrets["tokens"], client)
    if toks is not conn.secrets["tokens"]:
        conn.secrets["tokens"] = toks
        store.put(conn)
    return tok


def campaigns(store: ConnectionStore, ad_account: str | None = None, days: int = 28, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    conn = require(store, NAME)
    acct = ad_account or conn.meta.get("ad_account")
    if not acct:
        raise PermissionDenied("no Meta ad account id (META_AD_ACCOUNT_ID)")
    c = client or httpx.Client(timeout=60)
    tok = _token(store, conn, client)
    r = c.get(f"{GRAPH}/{acct}/insights", params={"access_token": tok, "level": "campaign", "date_preset": f"last_{days}d" if days in (7, 14, 28, 30, 90) else "last_28d",
                                                     "fields": "campaign_id,campaign_name,spend,actions,cost_per_action_type,impressions,clicks"})
    if r.status_code != 200:
        raise RuntimeError(f"Meta insights: {r.status_code} {r.text[:200]}")
    status = c.get(f"{GRAPH}/{acct}/campaigns", params={"access_token": tok, "fields": "id,status,daily_budget", "limit": 200})
    st = {x["id"]: x for x in status.json().get("data", [])} if status.status_code == 200 else {}
    out = []
    for row in r.json().get("data", []):
        results = sum(float(a.get("value") or 0) for a in row.get("actions", []) if a.get("action_type") in ("lead", "purchase", "onsite_conversion.messaging_conversation_started_7d", "schedule", "contact"))
        s = st.get(row.get("campaign_id"), {})
        out.append({"id": row.get("campaign_id"), "name": row.get("campaign_name"), "status": s.get("status"),
                    "daily_budget": float(s.get("daily_budget") or 0) / 100.0, "spend": float(row.get("spend") or 0),
                    "results": results, "impressions": int(row.get("impressions") or 0), "clicks": int(row.get("clicks") or 0)})
    return out


def waste(rows: list[dict[str, Any]], min_spend: float = 50.0) -> list[dict[str, Any]]:
    return [c for c in rows if c.get("status") == "ACTIVE" and c["spend"] >= min_spend and c["results"] == 0]


def pause(store: ConnectionStore, campaign_id: str, client: httpx.Client | None = None) -> dict[str, Any]:
    conn = require(store, NAME, "write")
    if "ads_management" not in conn.scopes:
        raise PermissionDenied("Meta connection has ads_read only; reconnect with 'ads.write'")
    c = client or httpx.Client(timeout=60)
    r = c.post(f"{GRAPH}/{campaign_id}", data={"access_token": _token(store, conn, client), "status": "PAUSED"})
    if r.status_code != 200:
        raise RuntimeError(f"Meta pause: {r.status_code} {r.text[:200]}")
    return {"campaign": campaign_id, "status": "PAUSED", "ok": bool(r.json().get("success"))}


def set_budget(store: ConnectionStore, campaign_id: str, daily_amount: float, client: httpx.Client | None = None) -> dict[str, Any]:
    conn = require(store, NAME, "write")
    if "ads_management" not in conn.scopes:
        raise PermissionDenied("Meta connection has ads_read only; reconnect with 'ads.write'")
    c = client or httpx.Client(timeout=60)
    r = c.post(f"{GRAPH}/{campaign_id}", data={"access_token": _token(store, conn, client), "daily_budget": int(round(daily_amount * 100))})
    if r.status_code != 200:
        raise RuntimeError(f"Meta budget: {r.status_code} {r.text[:200]}")
    return {"campaign": campaign_id, "daily_amount": daily_amount, "ok": bool(r.json().get("success"))}
