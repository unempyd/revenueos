"""CONVERT — prove the result, then turn it into revenue.

The offer clock, the drafted offer (which is never sent), the hosted operator's per-tenant offers,
a real Stripe payment becoming a licence key without a webhook, and the measurement that only says
"subscribed" when somebody actually pays.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from revenueos import billing, convert
from revenueos.connections import Connection, ConnectionStore, stripe_conn
from revenueos.store import Store


def _measured(store: Store, *, days_ago: int = 0, title: str = "SEO: missing description — /pricing",
              metric: str = "meta_description_fixed", before: float = 0, after: float = 1) -> int:
    """An action the business approved, executed, and whose result was measured `days_ago` days ago.

    Its own MonkeyPatch context so the clock is rewound only for the one INSERT (an undo on the
    test's own monkeypatch would also roll back the environment the test set up)."""
    aid = store.create_action("seo_opportunity", title, "the page has no meta description", dedupe_key=f"seo:{title}")
    store.set_action_status(aid, "executed")
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    with pytest.MonkeyPatch.context() as m:
        m.setattr("revenueos.store.now", lambda: ts)
        store.record_outcome(aid, "measured", metric=metric, before_value=before, after_value=after)
    return aid


# ── the clock ────────────────────────────────────────────────────────────────
def test_offer_state_none_then_clock_running_then_due(workspace, store, onboarded, monkeypatch):
    monkeypatch.delenv("REVENUEOS_PAYMENT_LINK_PRO", raising=False)
    st = convert.offer_state(workspace, store, onboarded)
    assert st["state"] == "none" and st["first_result_at"] is None and st["payment_link"] is None

    _measured(store, days_ago=2)
    st = convert.offer_state(workspace, store, onboarded)
    assert st["state"] == "clock_running"
    assert st["days_left"] == billing.FREE_DAYS_AFTER_FIRST_RESULT - 2
    assert st["price"] == 99

    # the same workspace, 20 days after the first measured result
    aid2 = store.create_action("content_opportunity", "Publish the rebooking guide", "…", dedupe_key="c:1")
    store.set_action_status(aid2, "executed")
    old = (datetime.now(UTC) - timedelta(days=20)).isoformat(timespec="seconds")
    monkeypatch.setattr("revenueos.store.now", lambda: old)
    store.record_outcome(aid2, "measured", metric="deliverable", before_value=0, after_value=1)
    monkeypatch.undo()
    st = convert.offer_state(workspace, store, onboarded)
    assert st["state"] == "due" and st["days_left"] == 0


def test_offer_state_is_licensed_when_a_pro_key_is_installed(workspace, store, onboarded, vendor_keys):
    _measured(store, days_ago=1)
    key = billing.issue_license(billing.Tier.pro, "owner@acme.example", "2999-01-01T00:00:00+00:00")
    (workspace.data / "license.json").write_text(json.dumps({"key": key}))
    assert convert.offer_state(workspace, store, onboarded)["state"] == "licensed"
    assert convert.ensure_offer_action(workspace, store, onboarded, customer_email=None) is None


# ── the drafted offer ────────────────────────────────────────────────────────
def test_one_offer_action_is_drafted_and_is_idempotent(workspace, store, onboarded, monkeypatch):
    monkeypatch.delenv("REVENUEOS_PAYMENT_LINK_PRO", raising=False)
    assert convert.ensure_offer_action(workspace, store, onboarded, customer_email=None) is None  # nothing measured

    _measured(store, days_ago=3)
    aid = convert.ensure_offer_action(workspace, store, onboarded, customer_email=None)
    assert aid is not None
    assert convert.ensure_offer_action(workspace, store, onboarded, customer_email=None) is None
    assert convert.ensure_offer_action(workspace, store, onboarded, customer_email=None) is None
    offers = [a for a in store.list_actions("pending") if (a["context"] or {}).get("kind") == "offer"]
    assert len(offers) == 1

    a = offers[0]
    assert a["title"] == "Offer Pro to Acme Scheduling"
    assert a["action_type"] == "follow_up"
    assert a["status"] == "pending"                      # nothing was sent; a human decides
    assert "meta_description_fixed 0 → 1" in a["content"]  # the measured before → after, from the store
    assert "SEO: missing description — /pricing" in a["content"]
    assert "$99/month" in a["content"]
    assert "revenueos license install <key>" in a["content"]
    assert "11 free day(s) remain" in a["content"]
    assert "http" not in a["content"]                     # no payment link was configured: none is invented
    assert a["context"].get("executor") is None           # no address on file → nothing to send
    assert a["context"]["note"] == convert.NO_EMAIL_NOTE


def test_offer_carries_the_payment_link_and_recipient_when_configured(workspace, store, onboarded, monkeypatch):
    monkeypatch.setenv("REVENUEOS_PAYMENT_LINK_PRO", "https://buy.stripe.example/pro")
    _measured(store, days_ago=1)
    aid = convert.ensure_offer_action(workspace, store, onboarded, customer_email="Owner@Acme.example")
    a = store.get_action(aid)
    assert "https://buy.stripe.example/pro" in a["content"]
    assert a["context"]["executor"] == "send_email" and a["context"]["to"] == "Owner@Acme.example"
    assert a["context"]["offer_email"] == "owner@acme.example"
    assert a["context"]["before"] == {"subscribed": 0}


def test_offer_wording_when_the_free_days_have_ended(workspace, store, onboarded, monkeypatch):
    _measured(store, days_ago=30)
    aid = convert.ensure_offer_action(workspace, store, onboarded, customer_email=None)
    body = store.get_action(aid)["content"]
    assert "those days have now ended" in body


# ── hosted mode ──────────────────────────────────────────────────────────────
def _tenant(workspace, tmp_path, email: str, company: str):
    from revenueos.context import BusinessContext
    from revenueos.paths import Workspace, new_workspace

    from .conftest import ANSWERS

    d = tmp_path / "tenants" / email.split("@")[0]
    new_workspace(d, workspace.root)
    tws = Workspace(d)
    tctx = BusinessContext.load(tws)
    tctx.onboard({**ANSWERS, "company_name": company})
    return tws


def test_hosted_offers_are_drafted_in_the_host_store_with_the_tenant_email(workspace, store, onboarded, tmp_path, monkeypatch):
    monkeypatch.delenv("REVENUEOS_PAYMENT_LINK_PRO", raising=False)
    tws = _tenant(workspace, tmp_path, "owner@clinic.example", "Bright Smile Dental")
    tstore = Store(tws.db)
    _measured(tstore, days_ago=4, title="SEO: missing title — /book")
    (workspace.root / "accounts.json").write_text(json.dumps({
        "owner@clinic.example": {"password_hash": "x", "salt": "00" * 16, "workspace": str(tws.root),
                                 "role": "owner", "created_at": "2026-09-01T00:00:00+00:00"}}))

    rows = convert.offers_for_tenants(workspace)
    assert len(rows) == 1
    assert rows[0]["email"] == "owner@clinic.example"
    assert rows[0]["state"] == "clock_running"
    assert rows[0]["action_id"] is not None

    # the offer lives in the HOST workspace (the host operator sends it), not the tenant's
    host_offers = [a for a in store.list_actions("pending") if (a["context"] or {}).get("kind") == "offer"]
    assert len(host_offers) == 1
    assert host_offers[0]["title"] == "Offer Pro to Bright Smile Dental"
    assert host_offers[0]["context"]["to"] == "owner@clinic.example"
    assert "SEO: missing title — /book" in host_offers[0]["content"]
    assert not [a for a in tstore.list_actions("pending") if (a["context"] or {}).get("kind") == "offer"]

    convert.offers_for_tenants(workspace)  # idempotent
    assert len([a for a in store.list_actions("pending") if (a["context"] or {}).get("kind") == "offer"]) == 1


def test_offers_for_tenants_is_empty_without_accounts(workspace):
    assert convert.offers_for_tenants(workspace) == []


# ── payment → licence, without a webhook ─────────────────────────────────────
def _stripe_connection(workspace, subs):
    cs = ConnectionStore(workspace)
    cs.put(Connection(provider="stripe", account="acct_1", scopes=["read"], secrets={"secret_key": "sk_test_abc"},
                      meta={"currency": "usd", "livemode": False}))

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p.startswith("/v1/subscriptions"):
            return httpx.Response(200, json={"data": subs})
        if p.startswith("/v1/customers"):
            return httpx.Response(200, json={"data": [{"id": "cus_1"}]})
        return httpx.Response(200, json={"data": []})

    return cs, httpx.Client(base_url=stripe_conn.API, transport=httpx.MockTransport(handler))


SUBS = [{
    "id": "sub_1", "status": "active", "livemode": False, "current_period_start": 1789000000,
    "customer": {"id": "cus_1", "email": "Buyer@Example.com"},
    "items": {"data": [{"quantity": 1, "price": {"unit_amount": 9900, "recurring": {"interval": "month"}}}]},
}]


def test_paying_customers_reads_active_subscriptions_with_emails(workspace):
    cs, client = _stripe_connection(workspace, SUBS)
    rows = stripe_conn.paying_customers(cs.get("stripe"), client=client)
    assert rows == [{"email": "buyer@example.com", "customer": "cus_1", "subscription": "sub_1",
                     "status": "active", "current_period_start": 1789000000, "livemode": False}]


def test_billing_worker_issues_a_licence_once_and_never_sends(workspace, store, onboarded, vendor_keys, monkeypatch):
    cs, client = _stripe_connection(workspace, SUBS)
    monkeypatch.setattr(stripe_conn, "_client", lambda conn, c=None: client)

    from revenueos.workers import run_worker

    r1 = run_worker("billing", workspace, store, onboarded, None, "test")
    assert r1.ok, r1.error
    assert r1.details["paying_customers"] == 1
    assert store.latest_metrics()["paying_customers"] == 1.0

    rows = [json.loads(x) for x in (workspace.data / "licenses.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["email"] == "buyer@example.com" and rows[0]["tier"] == "pro"
    lic = billing.verify_license(rows[0]["key"])
    assert lic is not None and lic.alg == "ed25519" and lic.kid == vendor_keys["kid"]
    assert lic is not None and lic.tier is billing.Tier.pro

    drafts = [a for a in store.list_actions("pending") if (a["context"] or {}).get("kind") == "license_delivery"]
    assert len(drafts) == 1
    d = drafts[0]
    assert d["title"] == "Deliver Pro licence to buyer@example.com"
    assert d["status"] == "pending"                                  # never sent
    assert rows[0]["key"] in d["content"] and "revenueos license install" in d["content"]
    assert d["context"]["executor"] == "send_email" and d["context"]["to"] == "buyer@example.com"

    r2 = run_worker("billing", workspace, store, onboarded, None, "test")  # idempotent: no second key, no second draft
    assert r2.ok
    assert len((workspace.data / "licenses.jsonl").read_text().splitlines()) == 1
    assert len([a for a in store.list_actions("pending") if (a["context"] or {}).get("kind") == "license_delivery"]) == 1

    export = json.loads((workspace.exports / convert.CUSTOMERS_EXPORT).read_text())
    assert export["customers"][0]["email"] == "buyer@example.com"


def test_licence_delivery_without_a_signing_key_says_what_is_missing(workspace, store, onboarded):
    """No signing key on this install (the conftest fixture clears every licence env var)."""
    out = convert.deliver_licenses(workspace, store, [{"email": "buyer@example.com", "customer": "cus_1",
                                                       "subscription": "sub_1", "current_period_start": 1789000000}])
    # `delivered` and `mailbox` were added when a paid licence started sending itself; without a
    # signing key there is no key to send, so nothing is delivered no matter what the mailbox says.
    assert out["issued"] == 0 and out["actions_created"] == 1 and out["skipped"] == 0
    assert out["secret"] is False and out["delivered"] == 0
    assert not (workspace.data / "licenses.jsonl").exists()
    d = [a for a in store.list_actions("pending") if (a["context"] or {}).get("kind") == "license_delivery"][0]
    assert convert.NO_SECRET_NOTE in d["content"]
    assert d["context"].get("executor") is None  # an email with no key in it is not worth sending


# ── did the offer convert? ───────────────────────────────────────────────────
def test_measure_offer_marks_subscribed_only_when_that_address_pays(workspace, store, onboarded, monkeypatch):
    _measured(store, days_ago=5)
    aid = convert.ensure_offer_action(workspace, store, onboarded, customer_email="buyer@example.com")
    store.set_action_status(aid, "executed")

    assert convert.measure_offer(store, [{"email": "someone-else@example.com"}]) == 0
    assert store.latest_outcome(aid)["status"] == "pending"

    assert convert.measure_offer(store, [{"email": "Buyer@Example.com"}]) == 1
    o = store.latest_outcome(aid)
    assert o["status"] == "measured" and o["metric"] == "subscribed"
    assert (o["before_value"], o["after_value"]) == (0.0, 1.0)

    assert convert.measure_offer(store, [{"email": "buyer@example.com"}]) == 0  # already measured


def test_measure_offer_does_nothing_without_a_customers_export(workspace):
    assert convert.read_customers_export(workspace) == []


# ── TODAY ────────────────────────────────────────────────────────────────────
def test_today_shows_the_offer_line_only_in_the_right_states(workspace, store, onboarded, vendor_keys, monkeypatch):
    from revenueos.today import build_brief

    monkeypatch.delenv("REVENUEOS_PAYMENT_LINK_PRO", raising=False)
    assert build_brief(store, workspace).offer_line() is None            # state none
    assert build_brief(store).offer_line() is None                       # no workspace → no clock

    _measured(store, days_ago=4)
    line = build_brief(store, workspace).offer_line()
    assert line is not None
    assert line.startswith("First measured result on ")
    assert "free for 10 more day(s)" in line
    assert "Pro $99/month: ask for the link" in line
    assert "`revenueos license install <key>`" in line
    assert "\n" not in line
    text = build_brief(store, workspace).render_text("Acme Scheduling")
    assert line in text

    key = billing.issue_license(billing.Tier.pro, "o@a.example", "2999-01-01T00:00:00+00:00")
    (workspace.data / "license.json").write_text(json.dumps({"key": key}))
    assert build_brief(store, workspace).offer_line() is None            # licensed → never shown


def test_offer_line_when_the_free_days_have_ended(workspace, store, onboarded, monkeypatch):
    monkeypatch.setenv("REVENUEOS_PAYMENT_LINK_PRO", "https://buy.stripe.example/pro")
    _measured(store, days_ago=40)
    from revenueos.today import build_brief

    line = build_brief(store, workspace).offer_line()
    assert "the free days have ended" in line and "https://buy.stripe.example/pro" in line


def test_executing_an_offer_draft_writes_the_email_and_sends_nothing(workspace, store, onboarded, monkeypatch):
    """The offer is a real, executable email — but only in dry run here; nothing reaches a network."""
    monkeypatch.setenv("REVENUEOS_DRY_RUN", "1")
    _measured(store, days_ago=2)
    aid = convert.ensure_offer_action(workspace, store, onboarded, customer_email="owner@acme.example")
    from revenueos.workers import execute_action

    out = execute_action(workspace, store, onboarded, None, store.get_action(aid))
    assert "dry run" in out
    written = (workspace.outputs / f"email-action-{aid}.txt").read_text()
    assert "To: owner@acme.example" in written and "$99/month" in written


@pytest.mark.parametrize("state", ["none", "licensed"])
def test_no_offer_is_ever_drafted_in_these_states(workspace, store, onboarded, vendor_keys, state):
    if state == "licensed":
        _measured(store, days_ago=2)
        key = billing.issue_license(billing.Tier.pro, "o@a.example", "2999-01-01T00:00:00+00:00")
        (workspace.data / "license.json").write_text(json.dumps({"key": key}))
    assert convert.ensure_offer_action(workspace, store, onboarded, customer_email="o@a.example") is None
    assert store.list_actions("pending", action_type="follow_up") == []
