"""Stripe: the money. Reads customers, subscriptions, invoices and charges into real revenue numbers;
the write executor creates and (with permission) sends an invoice to a lead.

Connect with a restricted or secret key (`revenueos connect stripe --key sk_...`), or from
STRIPE_SECRET_KEY. Scopes are what the key can do; a restricted read-only key makes every write refuse.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from . import Connection, ConnectionStore, PermissionDenied, require

NAME = "stripe"
API = "https://api.stripe.com/v1"


def describe() -> dict[str, Any]:
    return {
        "label": "Stripe",
        "reads": "customers, active subscriptions, invoices, charges → revenue this month, MRR, open invoices",
        "writes": "create an invoice for a lead and send it (needs 'allow changes')",
        "needs": "a Stripe secret or restricted key",
        "how": "revenueos connect stripe --key sk_live_… (or set STRIPE_SECRET_KEY)",
    }


def ready() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY"))


def missing() -> list[str]:
    return [] if ready() else ["STRIPE_SECRET_KEY (or pass --key)"]


def _client(conn: Connection, client: httpx.Client | None = None) -> httpx.Client:
    if client is not None:
        return client
    return httpx.Client(base_url=API, auth=(conn.secrets["secret_key"], ""), timeout=30)


def connect(store: ConnectionStore, key: str | None = None, client: httpx.Client | None = None) -> Connection:
    key = (key or os.environ.get("STRIPE_SECRET_KEY") or "").strip()
    if not key.startswith(("sk_", "rk_")):
        raise ValueError("a Stripe secret (sk_) or restricted (rk_) key is required")
    c = client or httpx.Client(base_url=API, auth=(key, ""), timeout=30)
    r = c.get("/account")
    if r.status_code != 200:
        raise RuntimeError(f"Stripe refused the key: {r.status_code} {r.text[:120]}")
    acct = r.json()
    write_capable = key.startswith("sk_")  # a restricted key's writes are whatever the owner granted at Stripe
    conn = Connection(provider=NAME, account=acct.get("id", ""), scopes=["read", "write"] if write_capable else ["read"],
                      secrets={"secret_key": key},
                      meta={"business": (acct.get("business_profile") or {}).get("name") or acct.get("settings", {}).get("dashboard", {}).get("display_name"),
                            "country": acct.get("country"), "currency": acct.get("default_currency"), "livemode": not key.startswith(("sk_test", "rk_test"))})
    return store.put(conn)


def _cents(items: list[dict[str, Any]], field: str = "amount") -> float:
    return sum(float(i.get(field) or 0) for i in items) / 100.0


def summary(conn: Connection, client: httpx.Client | None = None, days: int = 30) -> dict[str, Any]:
    """Real numbers from the account: what a customer's Spend page shows. Amounts in the account currency."""
    c = _client(conn, client)
    since = int((datetime.now(UTC) - timedelta(days=days)).timestamp())
    customers = c.get("/customers", params={"limit": 100}).json().get("data", [])
    subs = c.get("/subscriptions", params={"status": "active", "limit": 100}).json().get("data", [])
    charges = c.get("/charges", params={"limit": 100, "created[gte]": since}).json().get("data", [])
    invoices = c.get("/invoices", params={"limit": 100}).json().get("data", [])
    paid = [ch for ch in charges if ch.get("paid") and not ch.get("refunded")]
    mrr = 0.0
    for s in subs:
        for it in (s.get("items") or {}).get("data", []):
            price = it.get("price") or {}
            amt = float(price.get("unit_amount") or 0) / 100.0 * int(it.get("quantity") or 1)
            interval = (price.get("recurring") or {}).get("interval")
            mrr += amt / 12 if interval == "year" else amt
    open_inv = [i for i in invoices if i.get("status") == "open"]
    return {
        "currency": conn.meta.get("currency") or (paid[0]["currency"] if paid else "usd"),
        "customers": len(customers), "active_subscriptions": len(subs), "mrr": round(mrr, 2),
        f"revenue_{days}d": round(_cents(paid), 2), f"charges_{days}d": len(paid),
        "open_invoices": len(open_inv), "open_invoice_total": round(_cents(open_inv, "amount_due"), 2),
        "livemode": bool(conn.meta.get("livemode")),
    }


def send_invoice(store: ConnectionStore, *, email: str, name: str, description: str, amount: float, currency: str = "usd",
                 days_until_due: int = 14, send: bool = True, client: httpx.Client | None = None) -> dict[str, Any]:
    """The 'send the invoice' executor. Creates the customer if needed, a draft invoice with one line, then
    finalises and emails it — or leaves it as a draft when `send` is False (the approval said 'prepare')."""
    conn = require(store, NAME, "write")
    if "write" not in conn.scopes:
        raise PermissionDenied("the Stripe key on file is read-only; connect a key that can create invoices")
    c = _client(conn, client)
    found = c.get("/customers", params={"email": email, "limit": 1}).json().get("data", [])
    customer = found[0] if found else c.post("/customers", data={"email": email, "name": name}).json()
    inv = c.post("/invoices", data={"customer": customer["id"], "collection_method": "send_invoice",
                                    "days_until_due": days_until_due, "description": description[:500]}).json()
    if "id" not in inv:
        raise RuntimeError(f"Stripe would not create the invoice: {inv}")
    c.post("/invoiceitems", data={"customer": customer["id"], "invoice": inv["id"], "amount": int(round(amount * 100)),
                                  "currency": currency, "description": description[:500]})
    out = {"customer": customer["id"], "invoice": inv["id"], "amount": amount, "currency": currency, "status": "draft"}
    if send:
        fin = c.post(f"/invoices/{inv['id']}/finalize").json()
        sent = c.post(f"/invoices/{inv['id']}/send").json()
        out.update({"status": sent.get("status") or fin.get("status"), "hosted_invoice_url": sent.get("hosted_invoice_url") or fin.get("hosted_invoice_url")})
    return out


def void_invoice(store: ConnectionStore, invoice_id: str, client: httpx.Client | None = None) -> dict[str, Any]:
    conn = require(store, NAME, "write")
    c = _client(conn, client)
    inv = c.get(f"/invoices/{invoice_id}").json()
    if inv.get("status") == "draft":
        return c.delete(f"/invoices/{invoice_id}").json()
    return c.post(f"/invoices/{invoice_id}/void").json()
