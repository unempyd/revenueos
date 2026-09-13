"""Connections: tokens at rest, the permission model, every adapter against the real request shapes, and the
executors refusing without approval-plus-permission."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from revenueos.connections import (
    Connection,
    ConnectionStore,
    PermissionDenied,
    calendar_ics,
    describe_all,
    github_site,
    google,
    meta,
    oauth,
    require,
    stripe_conn,
    wordpress,
)
from revenueos.workers import execute_action


def _mock(routes):
    """routes: {(method, path_prefix): (status, json)}"""
    def handler(req: httpx.Request) -> httpx.Response:
        for (m, pfx), (status, payload) in routes.items():
            if req.method == m and req.url.path.startswith(pfx):
                return httpx.Response(status, json=payload if not callable(payload) else payload(req))
        return httpx.Response(404, json={"error": f"no route {req.method} {req.url.path}"})
    return httpx.MockTransport(handler)


# ── store + permissions ───────────────────────────────────────────────────────
def test_connection_store_seals_secrets_when_a_key_is_set(workspace, monkeypatch):
    monkeypatch.setenv("REVENUEOS_TOKEN_KEY", "test-key")
    cs = ConnectionStore(workspace)
    cs.put(Connection(provider="stripe", account="acct_1", scopes=["read"], secrets={"secret_key": "sk_test_abc"}))
    raw = json.loads(cs.path.read_text())
    assert "sk_test_abc" not in json.dumps(raw) and "__sealed__" in raw["stripe"]["secrets"]
    assert cs.get("stripe").secrets["secret_key"] == "sk_test_abc"
    assert oct(cs.path.stat().st_mode)[-3:] == "600"
    assert cs.get("stripe").public()["secrets"] == {"secret_key": "•••"}
    monkeypatch.delenv("REVENUEOS_TOKEN_KEY")
    with pytest.raises(RuntimeError):
        cs.get("stripe")


def test_permission_model_read_only_until_the_owner_allows_changes(workspace):
    cs = ConnectionStore(workspace)
    with pytest.raises(PermissionDenied, match="not connected"):
        require(cs, "stripe", "read")
    cs.put(Connection(provider="stripe", account="acct_1", scopes=["read", "write"], secrets={"secret_key": "sk_test_x"}))
    assert require(cs, "stripe", "read").account == "acct_1"
    with pytest.raises(PermissionDenied, match="read-only"):
        require(cs, "stripe", "write")
    cs.set_allow_write("stripe", True)
    assert require(cs, "stripe", "write").allow_write
    rows = {r["name"]: r for r in describe_all(cs)}
    assert rows["stripe"]["connected"] and rows["stripe"]["allow_write"] and not rows["google"]["connected"]
    assert cs.remove("stripe") and cs.get("stripe") is None


# ── Stripe ────────────────────────────────────────────────────────────────────
def _stripe_client(routes):
    return httpx.Client(base_url=stripe_conn.API, transport=_mock(routes))


def test_stripe_connect_summary_and_invoice(workspace):
    cs = ConnectionStore(workspace)
    c = _stripe_client({
        ("GET", "/v1/account"): (200, {"id": "acct_9", "country": "AU", "default_currency": "aud", "business_profile": {"name": "X190"}}),
        ("GET", "/v1/customers"): (200, {"data": [{"id": "cus_1", "email": "a@b.example"}]}),
        ("GET", "/v1/subscriptions"): (200, {"data": [{"items": {"data": [{"quantity": 1, "price": {"unit_amount": 9900, "recurring": {"interval": "month"}}}]}},
                                                      {"items": {"data": [{"quantity": 1, "price": {"unit_amount": 120000, "recurring": {"interval": "year"}}}]}}]}),
        ("GET", "/v1/charges"): (200, {"data": [{"amount": 9900, "paid": True, "refunded": False, "currency": "aud"}, {"amount": 5000, "paid": False}]}),
        ("GET", "/v1/invoices"): (200, {"data": [{"status": "open", "amount_due": 25000}, {"status": "paid", "amount_due": 9900}]}),
        ("POST", "/v1/customers"): (200, {"id": "cus_new"}),
        ("POST", "/v1/invoices/in_1/finalize"): (200, {"id": "in_1", "status": "open", "hosted_invoice_url": "https://inv.example/1"}),
        ("POST", "/v1/invoices/in_1/send"): (200, {"id": "in_1", "status": "open", "hosted_invoice_url": "https://inv.example/1"}),
        ("POST", "/v1/invoices"): (200, {"id": "in_1", "status": "draft"}),
        ("POST", "/v1/invoiceitems"): (200, {"id": "ii_1"}),
    })
    conn = stripe_conn.connect(cs, "sk_live_123", client=c)
    assert conn.account == "acct_9" and conn.meta["livemode"] and conn.scopes == ["read", "write"]
    s = stripe_conn.summary(conn, client=c)
    assert s["customers"] == 1 and s["active_subscriptions"] == 2 and s["mrr"] == 199.0 and s["revenue_30d"] == 99.0
    assert s["open_invoices"] == 1 and s["open_invoice_total"] == 250.0
    with pytest.raises(PermissionDenied):
        stripe_conn.send_invoice(cs, email="new@b.example", name="New", description="Site fixes", amount=250, client=c)
    cs.set_allow_write("stripe", True)
    inv = stripe_conn.send_invoice(cs, email="new@b.example", name="New", description="Site fixes", amount=250, currency="aud", client=c)
    assert inv["invoice"] == "in_1" and inv["status"] == "open" and inv["hosted_invoice_url"].endswith("/1")


# ── OAuth ─────────────────────────────────────────────────────────────────────
def test_oauth_authorize_exchange_refresh(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "csec")
    verifier, challenge = oauth._pkce()
    url = oauth.authorize_url("google", "http://127.0.0.1:1/callback", ["identity", "searchconsole.read"], "st", challenge)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?") and "code_challenge_method=S256" in url and "webmasters.readonly" in url
    client = httpx.Client(transport=_mock({("POST", "/token"): (200, {"access_token": "at1", "refresh_token": "rt", "expires_in": 3600, "scope": "openid email"})}))
    toks = oauth.exchange_code("google", "code123", "http://127.0.0.1:1/callback", verifier, client)
    assert toks["access_token"] == "at1" and toks["obtained_at"] > 0
    stale = {**toks, "obtained_at": 0}
    client2 = httpx.Client(transport=_mock({("POST", "/token"): (200, {"access_token": "at2", "expires_in": 3600})}))
    tok, new = oauth.access_token("google", stale, client2)
    assert tok == "at2" and new["refresh_token"] == "rt"


# ── Google: Search Console, GA4, Calendar, Ads ────────────────────────────────
def _google_conn(cs, scopes):
    cs.put(Connection(provider="google", account="owner@biz.example", scopes=scopes,
                      secrets={"tokens": {"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "obtained_at": 10**12}},
                      meta={"ads_customer_id": "123-456-7890"}))


def test_google_search_console_ga4_calendar_and_ads(workspace, monkeypatch):
    cs = ConnectionStore(workspace)
    _google_conn(cs, ["https://www.googleapis.com/auth/webmasters.readonly", "https://www.googleapis.com/auth/analytics.readonly",
                      "https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/adwords"])
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "dev")
    client = httpx.Client(transport=_mock({
        ("POST", "/webmasters/v3/sites/"): (200, {"rows": [
            {"keys": ["hair salon adelaide", "https://s.example/"], "clicks": 2, "impressions": 900, "ctr": 0.0022, "position": 8.1},
            {"keys": ["balayage", "https://s.example/colour"], "clicks": 40, "impressions": 500, "ctr": 0.08, "position": 3.0}]}),
        ("POST", "/v1beta/properties/"): (200, {"rows": [{"dimensionValues": [{"value": "Paid Social"}], "metricValues": [{"value": "120"}, {"value": "4"}, {"value": "0"}]}]}),
        ("POST", "/calendar/v3/calendars/primary/events"): (200, {"id": "ev1", "htmlLink": "https://cal.example/ev1", "hangoutLink": "https://meet.example/x"}),
        ("POST", "/v18/customers/1234567890/googleAds:searchStream"): (200, [{"results": [
            {"campaign": {"id": "11", "name": "Brand", "status": "ENABLED"}, "campaignBudget": {"amountMicros": "20000000"}, "metrics": {"costMicros": "150000000", "conversions": 0, "clicks": 300, "impressions": 9000}},
            {"campaign": {"id": "12", "name": "Balayage", "status": "ENABLED"}, "campaignBudget": {"amountMicros": "10000000"}, "metrics": {"costMicros": "90000000", "conversions": 6, "clicks": 200, "impressions": 4000}}]}]),
        ("POST", "/v18/customers/1234567890/campaigns:mutate"): (200, {"results": [{"resourceName": "customers/1234567890/campaigns/11"}]}),
        ("POST", "/v18/customers/1234567890/campaignBudgets:mutate"): (200, {"results": [{"resourceName": "customers/1234567890/campaignBudgets/5"}]}),
    }))
    gsc = google.search_console(cs, "https://s.example/", client=client)
    assert gsc["clicks"] == 42 and gsc["impressions"] == 1400
    opps = google.search_opportunities(gsc)
    assert [o["query"] for o in opps] == ["hair salon adelaide"] and "900 times" in opps[0]["why"]
    ga = google.ga4_report(cs, "987", client=client)
    assert ga["sessions"] == 120 and ga["conversions"] == 4
    with pytest.raises(PermissionDenied):
        google.book_call(cs, attendee_email="x@y.example", subject="Call", start=datetime(2026, 9, 20, 10, tzinfo=UTC), client=client)
    cs.set_allow_write("google", True)
    ev = google.book_call(cs, attendee_email="x@y.example", subject="Call", start=datetime(2026, 9, 20, 10, tzinfo=UTC), client=client)
    assert ev["event_id"] == "ev1" and ev["meet"].startswith("https://meet")
    camps = google.ads_campaigns(cs, client=client)
    assert {c["name"]: c["cost"] for c in camps} == {"Brand": 150.0, "Balayage": 90.0}
    waste = google.ads_waste(camps)
    assert [c["name"] for c in waste] == ["Brand"]
    assert google.ads_pause(cs, "11", client=client)["status"] == "PAUSED"
    assert google.ads_set_budget(cs, "customers/1234567890/campaignBudgets/5", 10.0, client=client)["daily_amount"] == 10.0


def _gaql_router(req: httpx.Request):
    """searchStream answers by the FROM clause, in the REST (camelCase) row shape the API returns."""
    q = json.loads(req.content)["query"]
    if "FROM keyword_view" in q:
        assert "ad_group_criterion.quality_info.quality_score" in q and "segments.date DURING LAST_28_DAYS" in q
        return [{"results": [
            {"campaign": {"id": "11", "name": "Brand"}, "adGroup": {"id": "5", "name": "Core"}, "adGroupCriterion": {"criterionId": "901", "status": "ENABLED",
             "keyword": {"text": "hair salon", "matchType": "BROAD"}, "qualityInfo": {"qualityScore": 3}}, "metrics": {"costMicros": "120000000", "clicks": 240, "impressions": 8000, "conversions": 0}},
            {"campaign": {"id": "12", "name": "Balayage"}, "adGroup": {"id": "6", "name": "Colour"}, "adGroupCriterion": {"criterionId": "902", "status": "ENABLED",
             "keyword": {"text": "balayage adelaide", "matchType": "EXACT"}, "qualityInfo": {"qualityScore": 8}}, "metrics": {"costMicros": "90000000", "clicks": 200, "impressions": 4000, "conversions": 6}},
            {"campaign": {"id": "12", "name": "Balayage"}, "adGroup": {"id": "6", "name": "Colour"}, "adGroupCriterion": {"criterionId": "903", "status": "ENABLED",
             "keyword": {"text": "ombre hair", "matchType": "PHRASE"}}, "metrics": {"costMicros": "0", "clicks": 0, "impressions": 0, "conversions": 0}}]}]
    if "FROM search_term_view" in q:
        assert "search_term_view.status" in q and "segments.search_term_match_type" in q
        return [{"results": [
            {"campaign": {"id": "11", "name": "Brand"}, "adGroup": {"id": "5", "name": "Core"}, "searchTermView": {"searchTerm": "hair salon jobs", "status": "NONE"},
             "segments": {"searchTermMatchType": "BROAD"}, "metrics": {"costMicros": "40000000", "clicks": 90, "impressions": 3000, "conversions": 0}},
            {"campaign": {"id": "11", "name": "Brand"}, "adGroup": {"id": "5", "name": "Core"}, "searchTermView": {"searchTerm": "hair salon jobs", "status": "NONE"},
             "segments": {"searchTermMatchType": "BROAD"}, "metrics": {"costMicros": "5000000", "clicks": 10, "impressions": 300, "conversions": 0}},
            {"campaign": {"id": "12", "name": "Balayage"}, "adGroup": {"id": "6", "name": "Colour"}, "searchTermView": {"searchTerm": "balayage adelaide", "status": "ADDED"},
             "segments": {"searchTermMatchType": "EXACT"}, "metrics": {"costMicros": "60000000", "clicks": 150, "impressions": 2500, "conversions": 5}},
            {"campaign": {"id": "11", "name": "Brand"}, "adGroup": {"id": "5", "name": "Core"}, "searchTermView": {"searchTerm": "free haircut", "status": "EXCLUDED"},
             "segments": {"searchTermMatchType": "BROAD"}, "metrics": {"costMicros": "30000000", "clicks": 70, "impressions": 1000, "conversions": 0}}]}]
    if "FROM campaign_criterion" in q:
        assert "campaign_criterion.negative = TRUE" in q
        return [{"results": [{"campaign": {"id": "11", "name": "Brand"}, "campaignCriterion": {"criterionId": "77", "keyword": {"text": "free", "matchType": "BROAD"}}}]}]
    if "FROM shared_criterion" in q:
        assert "shared_set.type = 'NEGATIVE_KEYWORDS'" in q
        return [{"results": [{"sharedSet": {"id": "500", "name": "Universal negatives"}, "sharedCriterion": {"keyword": {"text": "jobs", "matchType": "PHRASE"}}},
                             {"sharedSet": {"id": "500", "name": "Universal negatives"}, "sharedCriterion": {"keyword": {"text": "course", "matchType": "BROAD"}}}]}]
    if "FROM campaign_shared_set" in q:
        return [{"results": [{"campaign": {"id": "11"}, "sharedSet": {"id": "500", "name": "Universal negatives"}},
                             {"campaign": {"id": "12"}, "sharedSet": {"id": "500", "name": "Universal negatives"}}]}]
    return {"error": f"unexpected query {q[:60]}"}


def test_google_ads_keyword_search_term_and_negative_reads(workspace, monkeypatch):
    cs = ConnectionStore(workspace)
    _google_conn(cs, ["https://www.googleapis.com/auth/adwords"])
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "dev")
    client = httpx.Client(transport=_mock({("POST", "/v18/customers/1234567890/googleAds:searchStream"): (200, _gaql_router)}))
    kws = google.ads_keywords(cs, client=client)
    assert {k["text"]: (k["match_type"], k["quality_score"], k["cost"], k["impressions"]) for k in kws} == {
        "hair salon": ("broad", 3, 120.0, 8000), "balayage adelaide": ("exact", 8, 90.0, 4000), "ombre hair": ("phrase", None, 0.0, 0)}
    terms = google.ads_search_terms(cs, client=client)
    by = {t["text"]: t for t in terms}
    assert by["hair salon jobs"]["cost"] == 45.0 and by["hair salon jobs"]["clicks"] == 100 and by["hair salon jobs"]["status"] == "none"  # two rows aggregated
    assert by["balayage adelaide"]["status"] == "added" and by["free haircut"]["status"] == "excluded" and by["hair salon jobs"]["match_type"] == "broad"
    negs = google.ads_negative_keywords(cs, client=client)
    assert negs["campaign"] == [{"text": "free", "match_type": "broad", "campaign_id": "11", "campaign_name": "Brand"}]
    assert negs["lists"][0]["name"] == "Universal negatives" and [k["text"] for k in negs["lists"][0]["keywords"]] == ["jobs", "course"]
    assert {a["campaign_id"] for a in negs["list_campaigns"]} == {"11", "12"}


# ── Meta ──────────────────────────────────────────────────────────────────────
def test_meta_campaigns_waste_and_pause(workspace):
    cs = ConnectionStore(workspace)
    cs.put(Connection(provider="meta", account="Biz", scopes=["ads_read", "ads_management"],
                      secrets={"tokens": {"access_token": "at", "expires_in": 5184000, "obtained_at": 10**12}}, meta={"ad_account": "act_1"}))
    client = httpx.Client(transport=_mock({
        ("GET", "/v21.0/act_1/insights"): (200, {"data": [
            {"campaign_id": "c1", "campaign_name": "Reels", "spend": "220.5", "actions": [], "impressions": "5000", "clicks": "80"},
            {"campaign_id": "c2", "campaign_name": "Leads", "spend": "300", "actions": [{"action_type": "lead", "value": "12"}], "impressions": "9000", "clicks": "150"}]}),
        ("GET", "/v21.0/act_1/campaigns"): (200, {"data": [{"id": "c1", "status": "ACTIVE", "daily_budget": "1500"}, {"id": "c2", "status": "ACTIVE", "daily_budget": "2000"}]}),
        ("POST", "/v21.0/c1"): (200, {"success": True}),
    }))
    rows = meta.campaigns(cs, client=client)
    assert {r["name"]: (r["spend"], r["results"], r["daily_budget"]) for r in rows} == {"Reels": (220.5, 0.0, 15.0), "Leads": (300.0, 12.0, 20.0)}
    assert [c["name"] for c in meta.waste(rows)] == ["Reels"]
    with pytest.raises(PermissionDenied):
        meta.pause(cs, "c1", client=client)
    cs.set_allow_write("meta", True)
    assert meta.pause(cs, "c1", client=client)["ok"] and meta.set_budget(cs, "c1", 12.5, client=client)["ok"]


# ── site executors ────────────────────────────────────────────────────────────
def test_apply_head_fix_is_idempotent():
    html = "<html><head><title>Old</title></head><body>x</body></html>"
    out, changed = github_site.apply_head_fix(html, {"kind": "canonical", "url": "https://s.example/"})
    assert changed and '<link rel="canonical" href="https://s.example/">' in out
    assert github_site.apply_head_fix(out, {"kind": "canonical", "url": "https://s.example/"}) == (out, False)
    out2, changed = github_site.apply_head_fix(out, {"kind": "jsonld", "data": {"@context": "https://schema.org", "@type": "HairSalon", "name": "S"}})
    assert changed and '"@type": "HairSalon"' in out2
    assert github_site.apply_head_fix(out2, {"kind": "jsonld", "data": {"@type": "HairSalon"}}) == (out2, False)
    out3, changed = github_site.apply_head_fix(out2, {"kind": "title", "text": "New"})
    assert changed and "<title>New</title>" in out3


def test_wordpress_connect_and_fix(workspace):
    cs = ConnectionStore(workspace)
    client = httpx.Client(base_url="https://wp.example/wp-json/wp/v2", transport=_mock({
        ("GET", "/wp-json/wp/v2/users/me"): (200, {"id": 1, "slug": "admin", "capabilities": {"edit_pages": True}}),
        ("GET", "/wp-json/wp/v2/pages/7"): (200, {"id": 7, "content": {"raw": "<p>Hello</p>"}, "title": {"raw": "Home"}}),
        ("POST", "/wp-json/wp/v2/pages/7"): (200, {"id": 7, "link": "https://wp.example/"}),
    }))
    conn = wordpress.connect(cs, "https://wp.example", "admin", "abcd efgh", client=client)
    assert conn.scopes == ["read", "write"]
    with pytest.raises(PermissionDenied):
        wordpress.apply_fix(cs, 7, {"kind": "canonical", "url": "https://wp.example/"}, client=client)
    cs.set_allow_write("wordpress", True)
    assert wordpress.apply_fix(cs, 7, {"kind": "canonical", "url": "https://wp.example/"}, client=client)["changed"]


# ── calendar invite ───────────────────────────────────────────────────────────
def test_ics_invite_is_rfc5545():
    uid, ics = calendar_ics.build_invite(organizer_name="RevenueOS", organizer_email="hello@r.example", attendee_email="x@y.example",
                                         subject="15 minutes", start=datetime(2026, 9, 20, 10, tzinfo=UTC))
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and "METHOD:REQUEST" in ics and "DTSTART:20260920T100000Z" in ics and "DTEND:20260920T101500Z" in ics
    msg = calendar_ics.invite_message(sender_name="RevenueOS", sender_email="hello@r.example", to_email="x@y.example", subject="15 minutes", text="hi", ics=ics)
    assert msg["To"] == "x@y.example" and any(p.get_content_type() == "text/calendar" for p in msg.iter_attachments())


# ── executors through execute_action ──────────────────────────────────────────
def test_executors_refuse_without_connection_or_permission(workspace, store, onboarded, monkeypatch):
    monkeypatch.setenv("REVENUEOS_DRY_RUN", "1")
    a = store.create_action("seo_opportunity", "SEO: no canonical — https://acme-scheduling.example/", "x",
                            context={"executor": "site_deploy", "fix": {"kind": "canonical", "url": "https://acme-scheduling.example/"}, "file": "index.html"})
    assert "no site connection" in execute_action(workspace, store, onboarded, None, store.get_action(a))
    b = store.create_action("ad_waste", "Google Ads: 'Brand' spent 150.00 with 0 conversions (28d)", "x",
                            context={"executor": "ads_pause", "platform": "google", "campaign_id": "11"})
    assert "not paused: google is not connected" in execute_action(workspace, store, onboarded, None, store.get_action(b))
    c = store.create_action("follow_up", "Book a call with x@y.example", "x", context={"executor": "book_call", "to": "x@y.example"})
    out = execute_action(workspace, store, onboarded, None, store.get_action(c))
    assert "invite written to data/outputs/invite-" in out and list(workspace.outputs.glob("invite-*.ics"))
    d = store.create_action("follow_up", "Invoice x@y.example", "x", context={"executor": "send_invoice", "to": "x@y.example", "amount": 250})
    assert "no invoice: stripe is not connected" in execute_action(workspace, store, onboarded, None, store.get_action(d))
