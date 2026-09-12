"""Tests for the commercial plumbing: licence keys, Stripe webhooks/checkout, and the
tiny billing HTTP router. Uses the `workspace` fixture from conftest.py — a throwaway
workspace with its own `data/` directory, nothing touches the repo's own state."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest

from revenueos.billing import (
    Tier,
    billing_http,
    create_checkout_session,
    handle_webhook,
    issue_license,
    load_license,
    require_tier,
    verify_license,
    verify_webhook_signature,
)

SECRET = "vendor-hmac-secret"
WEBHOOK_SECRET = "whsec_test"
FAR_FUTURE = "2999-01-01T00:00:00+00:00"
PAST = "2000-01-01T00:00:00+00:00"


def _stripe_sig_header(secret: str, payload: bytes, t: int | None = None) -> str:
    t = int(time.time()) if t is None else t
    signed = f"{t}.".encode("ascii") + payload
    sig = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


# ── licence keys ─────────────────────────────────────────────────────────────
def test_issue_and_verify_roundtrip():
    key = issue_license(Tier.pro, "buyer@example.com", FAR_FUTURE, SECRET)
    assert key.count(".") == 1
    lic = verify_license(key, SECRET)
    assert lic is not None
    assert lic.tier is Tier.pro
    assert lic.customer_email == "buyer@example.com"
    assert lic.expires_at == FAR_FUTURE


def test_verify_rejects_wrong_secret():
    key = issue_license(Tier.business, "buyer@example.com", FAR_FUTURE, SECRET)
    assert verify_license(key, "not-the-secret") is None


def test_verify_rejects_tampered_payload():
    key = issue_license(Tier.agency, "buyer@example.com", FAR_FUTURE, SECRET)
    payload_b64, sig = key.split(".", 1)
    # flip a character in the payload without recomputing the signature
    tampered_char = "A" if payload_b64[0] != "A" else "B"
    tampered = tampered_char + payload_b64[1:]
    assert verify_license(f"{tampered}.{sig}", SECRET) is None


def test_verify_rejects_expired():
    key = issue_license(Tier.pro, "buyer@example.com", PAST, SECRET)
    assert verify_license(key, SECRET) is None


def test_verify_rejects_malformed_key():
    assert verify_license("not-a-real-key", SECRET) is None
    assert verify_license("", SECRET) is None
    assert verify_license("###.###", SECRET) is None


# ── load_license / require_tier (customer install side) ─────────────────────
def test_load_license_defaults_to_community_when_no_file(workspace):
    lic = load_license(workspace)
    assert lic.tier is Tier.community


def test_load_license_verifies_installed_key(workspace, monkeypatch):
    monkeypatch.setenv("REVENUEOS_LICENSE_SECRET", SECRET)
    key = issue_license(Tier.business, "owner@acme.example", FAR_FUTURE, SECRET)
    (workspace.data / "license.json").write_text(json.dumps({"key": key}))
    lic = load_license(workspace)
    assert lic.tier is Tier.business
    assert lic.customer_email == "owner@acme.example"


def test_load_license_falls_back_to_community_without_secret_configured(workspace, monkeypatch):
    monkeypatch.delenv("REVENUEOS_LICENSE_SECRET", raising=False)
    key = issue_license(Tier.agency, "owner@acme.example", FAR_FUTURE, SECRET)
    (workspace.data / "license.json").write_text(json.dumps({"key": key}))
    lic = load_license(workspace)
    assert lic.tier is Tier.community


def test_load_license_falls_back_to_community_on_tampered_file(workspace, monkeypatch):
    monkeypatch.setenv("REVENUEOS_LICENSE_SECRET", SECRET)
    (workspace.data / "license.json").write_text(json.dumps({"key": "garbage.notasig"}))
    assert load_license(workspace).tier is Tier.community


def test_require_tier_blocks_below_minimum(workspace, monkeypatch):
    monkeypatch.delenv("REVENUEOS_LICENSE_SECRET", raising=False)
    msg = require_tier(workspace, Tier.pro)
    assert msg is not None
    assert "Pro" in msg
    assert "99" in msg


def test_require_tier_allows_community_minimum(workspace, monkeypatch):
    monkeypatch.delenv("REVENUEOS_LICENSE_SECRET", raising=False)
    assert require_tier(workspace, Tier.community) is None


def test_require_tier_allows_when_licence_meets_minimum(workspace, monkeypatch):
    monkeypatch.setenv("REVENUEOS_LICENSE_SECRET", SECRET)
    key = issue_license(Tier.business, "owner@acme.example", FAR_FUTURE, SECRET)
    (workspace.data / "license.json").write_text(json.dumps({"key": key}))
    assert require_tier(workspace, Tier.pro) is None
    assert require_tier(workspace, Tier.business) is None
    assert require_tier(workspace, Tier.agency) is not None


# ── Stripe webhook signature ─────────────────────────────────────────────────
def test_verify_webhook_signature_good():
    payload = b'{"type": "ping"}'
    header = _stripe_sig_header(WEBHOOK_SECRET, payload)
    assert verify_webhook_signature(payload, header, WEBHOOK_SECRET) is True


def test_verify_webhook_signature_bad_secret():
    payload = b'{"type": "ping"}'
    header = _stripe_sig_header(WEBHOOK_SECRET, payload)
    assert verify_webhook_signature(payload, header, "some-other-secret") is False


def test_verify_webhook_signature_tampered_payload():
    payload = b'{"type": "ping"}'
    header = _stripe_sig_header(WEBHOOK_SECRET, payload)
    assert verify_webhook_signature(b'{"type": "pong"}', header, WEBHOOK_SECRET) is False


def test_verify_webhook_signature_stale_timestamp():
    payload = b'{"type": "ping"}'
    old = int(time.time()) - 10_000
    header = _stripe_sig_header(WEBHOOK_SECRET, payload, t=old)
    assert verify_webhook_signature(payload, header, WEBHOOK_SECRET, tolerance=300) is False


def test_verify_webhook_signature_missing_header():
    assert verify_webhook_signature(b"{}", "", WEBHOOK_SECRET) is False
    assert verify_webhook_signature(b"{}", "garbage", WEBHOOK_SECRET) is False


# ── handle_webhook ────────────────────────────────────────────────────────────
def _checkout_completed_event(*, tier: str = "pro", email: str = "buyer@example.com",
                               price_id: str | None = None) -> dict:
    obj = {
        "id": "cs_test_123",
        "customer": "cus_test_123",
        "subscription": "sub_test_123",
        "customer_details": {"email": email},
        "metadata": {"tier": tier} if tier else {},
    }
    if price_id:
        obj["items"] = {"data": [{"price": {"id": price_id}}]}
    return {"id": "evt_test_1", "type": "checkout.session.completed", "data": {"object": obj}}


def test_handle_webhook_checkout_completed_issues_license_and_appends_row(workspace):
    event = _checkout_completed_event()
    payload = json.dumps(event).encode("utf-8")
    header = _stripe_sig_header(WEBHOOK_SECRET, payload)

    result = handle_webhook(
        payload, header,
        webhook_secret=WEBHOOK_SECRET, license_secret=SECRET,
        price_ids={Tier.pro: "price_pro_123"}, ws=workspace,
    )

    assert result["ok"] is True
    assert result["tier"] == "pro"
    assert result["email"] == "buyer@example.com"
    assert "." in result["key"]
    lic = verify_license(result["key"], SECRET)
    assert lic is not None and lic.tier is Tier.pro and lic.customer_email == "buyer@example.com"

    rows_path = workspace.data / "licenses.jsonl"
    assert rows_path.exists()
    rows = [json.loads(line) for line in rows_path.read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["tier"] == "pro"
    assert row["email"] == "buyer@example.com"
    assert row["key"] == result["key"]
    assert row["stripe_customer"] == "cus_test_123"
    assert row["stripe_subscription"] == "sub_test_123"
    assert "issued_at" in row


def test_handle_webhook_maps_price_id_when_no_metadata(workspace):
    event = _checkout_completed_event(tier="", price_id="price_business_456")
    payload = json.dumps(event).encode("utf-8")
    header = _stripe_sig_header(WEBHOOK_SECRET, payload)

    result = handle_webhook(
        payload, header,
        webhook_secret=WEBHOOK_SECRET, license_secret=SECRET,
        price_ids={Tier.business: "price_business_456"}, ws=workspace,
    )
    assert result["ok"] is True
    assert result["tier"] == "business"


def test_handle_webhook_bad_signature_rejected(workspace):
    event = _checkout_completed_event()
    payload = json.dumps(event).encode("utf-8")
    result = handle_webhook(
        payload, "t=1,v1=deadbeef",
        webhook_secret=WEBHOOK_SECRET, license_secret=SECRET,
        price_ids={Tier.pro: "price_pro_123"}, ws=workspace,
    )
    assert result == {"ok": False, "error": "invalid webhook signature"}
    assert not (workspace.data / "licenses.jsonl").exists()


def test_handle_webhook_unknown_price_id(workspace):
    event = _checkout_completed_event(tier="", price_id="price_unknown")
    payload = json.dumps(event).encode("utf-8")
    header = _stripe_sig_header(WEBHOOK_SECRET, payload)
    result = handle_webhook(
        payload, header,
        webhook_secret=WEBHOOK_SECRET, license_secret=SECRET,
        price_ids={Tier.pro: "price_pro_123"}, ws=workspace,
    )
    assert result["ok"] is False
    assert "tier" in result["error"]


def test_handle_webhook_missing_email(workspace):
    obj = {"id": "cs_test_2", "customer": "cus_2", "subscription": "sub_2", "metadata": {"tier": "pro"}}
    event = {"id": "evt_2", "type": "checkout.session.completed", "data": {"object": obj}}
    payload = json.dumps(event).encode("utf-8")
    header = _stripe_sig_header(WEBHOOK_SECRET, payload)
    result = handle_webhook(
        payload, header,
        webhook_secret=WEBHOOK_SECRET, license_secret=SECRET,
        price_ids={Tier.pro: "price_pro_123"}, ws=workspace,
    )
    assert result == {"ok": False, "error": "missing customer email in event"}


def test_handle_webhook_subscription_deleted_revokes(workspace):
    obj = {"id": "sub_test_999", "customer": "cus_test_999"}
    event = {"id": "evt_3", "type": "customer.subscription.deleted", "data": {"object": obj}}
    payload = json.dumps(event).encode("utf-8")
    header = _stripe_sig_header(WEBHOOK_SECRET, payload)

    result = handle_webhook(
        payload, header,
        webhook_secret=WEBHOOK_SECRET, license_secret=SECRET,
        price_ids={Tier.pro: "price_pro_123"}, ws=workspace,
    )
    assert result["ok"] is True
    assert result["revoked"] is True
    assert result["stripe_subscription"] == "sub_test_999"

    rows = [json.loads(line) for line in (workspace.data / "licenses.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["revoked"] is True
    assert rows[0]["key"] is None
    assert rows[0]["stripe_customer"] == "cus_test_999"


def test_handle_webhook_unhandled_event_type(workspace):
    event = {"id": "evt_4", "type": "invoice.paid", "data": {"object": {}}}
    payload = json.dumps(event).encode("utf-8")
    header = _stripe_sig_header(WEBHOOK_SECRET, payload)
    result = handle_webhook(
        payload, header,
        webhook_secret=WEBHOOK_SECRET, license_secret=SECRET,
        price_ids={}, ws=workspace,
    )
    assert result["ok"] is False
    assert "invoice.paid" in result["error"]


# ── Stripe checkout session (fake transport) ─────────────────────────────────
def test_create_checkout_session_posts_expected_form_and_returns_url():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["form"] = dict(httpx.QueryParams(request.content.decode("utf-8")))
        return httpx.Response(200, json={"url": "https://checkout.stripe.com/c/pay/x"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    url = create_checkout_session(
        Tier.pro, "https://example.com/ok", "https://example.com/cancel",
        secret_key="sk_test_123", price_ids={Tier.pro: "price_pro_123"}, client=client,
    )

    assert url == "https://checkout.stripe.com/c/pay/x"
    assert captured["url"] == "https://api.stripe.com/v1/checkout/sessions"
    assert captured["headers"]["authorization"] == "Bearer sk_test_123"
    form = captured["form"]
    assert form["mode"] == "subscription"
    assert form["success_url"] == "https://example.com/ok"
    assert form["cancel_url"] == "https://example.com/cancel"
    assert form["line_items[0][price]"] == "price_pro_123"
    assert form["line_items[0][quantity]"] == "1"
    assert form["metadata[tier]"] == "pro"


def test_create_checkout_session_missing_price_raises():
    with pytest.raises(ValueError):
        create_checkout_session(
            Tier.agency, "https://example.com/ok", "https://example.com/cancel",
            secret_key="sk_test_123", price_ids={Tier.pro: "price_pro_123"},
            client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"url": "x"}))),
        )


def test_create_checkout_session_error_response_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"message": "card declined"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        create_checkout_session(
            Tier.pro, "https://example.com/ok", "https://example.com/cancel",
            secret_key="sk_test_123", price_ids={Tier.pro: "price_pro_123"}, client=client,
        )


# ── billing_http router ──────────────────────────────────────────────────────
def test_billing_http_returns_none_for_unrelated_path(workspace):
    assert billing_http("/api/today", "GET", b"", {}, workspace) is None


def test_billing_http_checkout_missing_config_returns_400(workspace, monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    status, _ctype, body = billing_http("/billing/checkout?tier=pro", "GET", b"", {}, workspace)
    assert status == 400
    assert "STRIPE_SECRET_KEY" in body


def test_billing_http_checkout_unknown_tier_returns_400(workspace):
    status, _ctype, body = billing_http("/billing/checkout?tier=nonsense", "GET", b"", {}, workspace)
    assert status == 400
    assert "nonsense" in body


def test_billing_http_checkout_community_returns_400(workspace):
    status, _ctype, body = billing_http("/billing/checkout?tier=community", "GET", b"", {}, workspace)
    assert status == 400
    assert "community" in body.lower()


def test_billing_http_checkout_success_redirect(workspace, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro_123")
    monkeypatch.setenv("REVENUEOS_BILLING_SUCCESS_URL", "https://example.com/ok")
    monkeypatch.setenv("REVENUEOS_BILLING_CANCEL_URL", "https://example.com/cancel")

    import revenueos.billing as billing_module

    def fake_create_checkout_session(tier, success_url, cancel_url, *, secret_key, price_ids, client=None):
        return "https://checkout.stripe.com/c/pay/fake"

    monkeypatch.setattr(billing_module, "create_checkout_session", fake_create_checkout_session)

    status, _ctype, body = billing_http("/billing/checkout?tier=pro", "GET", b"", {}, workspace)
    assert status == 302
    assert body == "https://checkout.stripe.com/c/pay/fake"


def test_billing_http_webhook_not_configured_returns_500(workspace, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("REVENUEOS_LICENSE_SECRET", raising=False)
    status, ctype, _body = billing_http("/billing/webhook", "POST", b"{}", {}, workspace)
    assert status == 500
    assert ctype == "application/json"


def test_billing_http_webhook_dispatches_to_handle_webhook(workspace, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("REVENUEOS_LICENSE_SECRET", SECRET)
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro_123")

    event = _checkout_completed_event()
    payload = json.dumps(event).encode("utf-8")
    header = _stripe_sig_header(WEBHOOK_SECRET, payload)

    status, ctype, body = billing_http(
        "/billing/webhook", "POST", payload, {"Stripe-Signature": header}, workspace,
    )
    assert status == 200
    assert ctype == "application/json"
    result = json.loads(body)
    assert result["ok"] is True
    assert result["tier"] == "pro"
