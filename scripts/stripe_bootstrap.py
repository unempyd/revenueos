#!/usr/bin/env python3
"""Create (idempotently) the RevenueOS products and recurring prices in a Stripe account and
print the environment variables the panel needs.

    STRIPE_SECRET_KEY=sk_test_... python3 scripts/stripe_bootstrap.py            # test mode
    STRIPE_SECRET_KEY=sk_live_... python3 scripts/stripe_bootstrap.py --live     # production
    python3 scripts/stripe_bootstrap.py --dry-run                                # print the plan only

Idempotency: products are looked up by `metadata[revenueos_tier]`; prices by product +
unit_amount + interval. Re-running never duplicates. Uses the Stripe REST API over httpx
(form-encoded), the same way src/revenueos/billing.py does; no Stripe SDK.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from revenueos.billing import TIER_FEATURES, TIER_LABELS, TIER_PRICES, Tier  # noqa: E402

API = "https://api.stripe.com/v1"
PAID = [Tier.pro, Tier.business, Tier.agency]


def _client(secret_key: str, client: httpx.Client | None = None) -> httpx.Client:
    return client or httpx.Client(base_url=API, auth=(secret_key, ""), timeout=30.0)


def _list(client: httpx.Client, path: str, **params: Any) -> list[dict[str, Any]]:
    r = client.get(path, params={"limit": 100, **params})
    r.raise_for_status()
    return r.json().get("data", [])


def ensure_product(client: httpx.Client, tier: Tier) -> dict[str, Any]:
    for p in _list(client, "/products", active="true"):
        if (p.get("metadata") or {}).get("revenueos_tier") == tier.value:
            return p
    features = "; ".join(TIER_FEATURES.get(tier, [])[:4])
    r = client.post("/products", data={
        "name": f"RevenueOS {TIER_LABELS[tier]}",
        "description": f"RevenueOS {TIER_LABELS[tier]}: {features}"[:500],
        "metadata[revenueos_tier]": tier.value,
    })
    r.raise_for_status()
    return r.json()


def ensure_price(client: httpx.Client, product: dict[str, Any], tier: Tier) -> dict[str, Any]:
    amount = int(TIER_PRICES[tier]) * 100
    for pr in _list(client, "/prices", product=product["id"], active="true"):
        rec = pr.get("recurring") or {}
        if pr.get("unit_amount") == amount and pr.get("currency") == "usd" and rec.get("interval") == "month":
            return pr
    r = client.post("/prices", data={
        "product": product["id"], "unit_amount": amount, "currency": "usd",
        "recurring[interval]": "month", "metadata[revenueos_tier]": tier.value,
        "lookup_key": f"revenueos_{tier.value}_monthly",
    })
    r.raise_for_status()
    return r.json()


def bootstrap(secret_key: str, client: httpx.Client | None = None) -> dict[str, str]:
    c = _client(secret_key, client)
    env: dict[str, str] = {}
    for tier in PAID:
        product = ensure_product(c, tier)
        price = ensure_price(c, product, tier)
        env[f"STRIPE_PRICE_{tier.value.upper()}"] = price["id"]
    return env


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="require a live key (sk_live_)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, call nothing")
    args = ap.parse_args(argv)
    if args.dry_run:
        for tier in PAID:
            print(f"product 'RevenueOS {TIER_LABELS[tier]}' → price ${TIER_PRICES[tier]}/month (metadata revenueos_tier={tier.value})")
        return 0
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key.startswith("sk_"):
        print("STRIPE_SECRET_KEY is not set (expected sk_test_… or sk_live_…)", file=sys.stderr)
        return 2
    if args.live and not key.startswith("sk_live_"):
        print("--live requires a live secret key", file=sys.stderr)
        return 2
    env = bootstrap(key)
    print("# add to the panel's environment (docker-compose / deploy secrets):")
    for k, v in env.items():
        print(f"{k}={v}")
    print("# then create a webhook endpoint for https://<your-host>/billing/webhook with events:")
    print("#   checkout.session.completed customer.subscription.created customer.subscription.updated customer.subscription.deleted")
    print("# and set STRIPE_WEBHOOK_SECRET=whsec_…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
