"""After Stripe Checkout: the buyer lands on /billing/success and gets the licence the webhook issued."""
from __future__ import annotations

import json

import httpx

from revenueos import billing


def _stripe(session: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/checkout/sessions/"):
            return httpx.Response(200, json=session)
        return httpx.Response(404, json={})
    return httpx.Client(base_url="https://api.stripe.com", transport=httpx.MockTransport(handler))


def test_success_page_shows_issued_licence(workspace, vendor_keys):
    key = billing.issue_license(billing.Tier.pro, "buyer@example.com", "2027-01-01T00:00:00+00:00")
    (workspace.data / "licenses.jsonl").write_text(json.dumps({
        "issued_at": "2026-09-12T13:00:00+00:00", "tier": "pro", "email": "buyer@example.com", "key": key,
        "stripe_customer": "cus_123", "stripe_subscription": "sub_123"}) + "\n")
    session = {"id": "cs_1", "customer": "cus_123", "customer_details": {"email": "buyer@example.com"},
               "payment_status": "paid", "status": "complete", "metadata": {"tier": "pro"}}
    status, ctype, body = billing.render_success(workspace, "cs_1", secret_key="sk_test_x", client=_stripe(session))
    assert status == 200 and key in body and "revenueos license install" in body and "Pro" in body


def test_success_page_waits_for_webhook(workspace):
    session = {"id": "cs_2", "customer": "cus_999", "customer_details": {"email": "late@example.com"},
               "payment_status": "paid", "status": "complete", "metadata": {"tier": "pro"}}
    status, _ctype, body = billing.render_success(workspace, "cs_2", secret_key="sk_test_x", client=_stripe(session))
    assert status == 202 and "being issued" in body and 'refresh' in body


def test_success_page_rejects_unknown_session(workspace):
    status, _ctype, body = billing.render_success(workspace, "cs_nope", secret_key="sk_test_x", client=_stripe({}))
    assert status == 404 and "could not find" in body
    status, _ctype, _body = billing.render_success(workspace, "", secret_key=None)
    assert status == 400


def test_default_urls_follow_the_request_host(monkeypatch):
    monkeypatch.delenv("REVENUEOS_BILLING_CANCEL_URL", raising=False)
    success, cancel = billing._default_urls({"Host": "revenueos-billing.fly.dev", "X-Forwarded-Proto": "https"})
    assert success == "https://revenueos-billing.fly.dev/billing/success?session_id={CHECKOUT_SESSION_ID}"
    assert cancel == "https://revenueos-billing.fly.dev/site/pricing.html"
    success, _ = billing._default_urls({"Host": "127.0.0.1:8791"})
    assert success.startswith("http://127.0.0.1:8791/")


def test_billing_http_routes_success(workspace, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(billing, "render_success", lambda ws, sid, secret_key=None: (200, "text/html; charset=utf-8", f"ok {sid}"))
    assert billing.billing_http("/billing/success?session_id=cs_9", "GET", b"", {}, workspace) == (200, "text/html; charset=utf-8", "ok cs_9")
