"""Commercial plumbing: subscription tiers, signed licence keys, and Stripe billing.

RevenueOS itself is self-hosted and free to run; RevenueOS Inc. (the vendor) sells
subscriptions that unlock tiers via a signed licence key. This module has two audiences:

  * the **customer install** — `revenueos license show|install` and `require_tier()`,
    which gate a feature behind a tier the install has (or hasn't) unlocked.
  * the **vendor** — `revenueos license issue` (mint a key), `revenueos billing checkout`
    (start a Stripe Checkout session), and `handle_webhook` (process Stripe events into
    licence keys). These need `REVENUEOS_LICENSE_SECRET` / `STRIPE_SECRET_KEY`, which only
    the vendor holds.

Licence keys are `<base64url json payload>.<hmac-sha256 hex>`, verified with a single
shared secret (`REVENUEOS_LICENSE_SECRET`). This is a symmetric scheme, not a real
public/private signature: the same secret that issues a key also verifies it. For a
self-hosted product that is a deliberate, documented simplification — the customer's
install is shipped with that secret as a config value (baked into their deployment, not
committed to source) so it can verify keys offline. A customer who extracts the secret
from their own install could mint their own keys; the mitigation is that doing so gains
them nothing they don't already have (the product is self-hosted and open), and the
"real" enforcement point is the vendor's Stripe subscription — a licence key just mirrors
what Stripe already believes. If that threat model ever matters, swap the HMAC for
Ed25519 (private key at the vendor, public key shipped to customers) without changing
this module's public functions.

Never log or print a licence key except where explicitly asked for one (`license issue`,
`license show`) — `handle_webhook`'s return value carries a key back to its caller, which
must not log it either.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from .paths import Workspace

STRIPE_API_BASE = "https://api.stripe.com/v1"

# How long a licence minted from a webhook stays valid before the next renewal webhook
# (subscription cycle) refreshes it. Comfortably longer than a month so a few days of
# Stripe/webhook flakiness never lands a paying customer back on Community.
_WEBHOOK_LICENSE_VALIDITY = timedelta(days=35)


class Tier(str, Enum):
    community = "community"
    pro = "pro"
    business = "business"
    agency = "agency"


# Ordered cheapest → most expensive; used to compare "is this install entitled to at
# least tier X".
TIER_ORDER: dict[Tier, int] = {Tier.community: 0, Tier.pro: 1, Tier.business: 2, Tier.agency: 3}

TIER_PRICES: dict[Tier, int] = {  # USD / month
    Tier.community: 0,
    Tier.pro: 99,
    Tier.business: 299,
    Tier.agency: 999,
}

TIER_LABELS: dict[Tier, str] = {
    Tier.community: "Community",
    Tier.pro: "Pro",
    Tier.business: "Business",
    Tier.agency: "Agency",
}

# Plain data table — one tuple of feature strings per tier. Higher tiers also include
# every feature of the tiers below them (see `features_for`); this table lists only what
# each tier *adds*.
TIER_FEATURES: dict[Tier, tuple[str, ...]] = {
    Tier.community: (
        "manual runs (`revenueos run <worker>`, `revenueos today`)",
    ),
    Tier.pro: (
        "continuous operation via the orchestrator (cron scheduling, always-on)",
        "advanced audits",
        "historical reporting",
        "automated monitoring",
        "persistent business memory (learning-loop corrections beyond 30 days)",
    ),
    Tier.business: (
        "multi-brand workspaces",
        "multiple users",
        "CRM sync",
        "campaign fleets",
        "approval controls / team workflows",
        "centralized analytics",
    ),
    Tier.agency: (
        "multi-client management",
        "white-label reports",
        "account fleets",
        "centralized controls",
        "API access",
        "client workspaces",
    ),
}


def features_for(tier: Tier) -> list[str]:
    """Every feature a tier unlocks, including everything the tiers below it unlock."""
    tier = Tier(tier)
    out: list[str] = []
    for t in sorted(TIER_ORDER, key=TIER_ORDER.__getitem__):
        out.extend(TIER_FEATURES[t])
        if t is tier:
            break
    return out


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


# ── licence keys ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class License:
    tier: Tier
    customer_email: str
    expires_at: str  # ISO-8601
    issued_at: str = ""

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return _parse_iso(self.expires_at) < (now or datetime.now(UTC))


def _community_license() -> License:
    return License(tier=Tier.community, customer_email="", expires_at="9999-12-31T00:00:00+00:00", issued_at="")


def issue_license(tier: Tier, customer_email: str, expires_at: str | datetime, secret: str) -> str:
    """Mint `<base64url payload>.<hmac-sha256 hex>`. Vendor-side; needs the vendor secret."""
    tier = Tier(tier)
    expires_str = expires_at.isoformat(timespec="seconds") if isinstance(expires_at, datetime) else str(expires_at)
    payload = {
        "tier": tier.value,
        "customer_email": customer_email,
        "expires_at": expires_str,
        "issued_at": now_iso(),
    }
    payload_b64 = _b64url_encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_license(key: str, secret: str) -> License | None:
    """Checks the HMAC signature and expiry. Returns None on any malformed/tampered/expired key."""
    if not key or "." not in key:
        return None
    payload_b64, _, sig = key.partition(".")
    expected = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64))
        tier = Tier(payload["tier"])
        customer_email = str(payload["customer_email"])
        expires_at = str(payload["expires_at"])
        issued_at = str(payload.get("issued_at", ""))
        expired = _parse_iso(expires_at) < datetime.now(UTC)
    except (KeyError, TypeError, ValueError, binascii.Error, UnicodeDecodeError):
        return None
    if expired:
        return None
    return License(tier=tier, customer_email=customer_email, expires_at=expires_at, issued_at=issued_at)


def load_license(ws: Workspace) -> License:
    """Reads data/license.json (`{"key": ...}`) and verifies it against
    $REVENUEOS_LICENSE_SECRET. Community tier when there is no file, no secret configured,
    or the key fails verification (bad signature, tampered, expired) — always fails closed
    to the free tier rather than failing open."""
    path = ws.data / "license.json"
    if not path.exists():
        return _community_license()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _community_license()
    key = raw.get("key") if isinstance(raw, dict) else None
    secret = os.environ.get("REVENUEOS_LICENSE_SECRET")
    if not key or not secret:
        return _community_license()
    return verify_license(key, secret) or _community_license()


def require_tier(ws: Workspace, minimum: Tier) -> str | None:
    """None when the installed licence meets `minimum`; otherwise an explanatory message."""
    minimum = Tier(minimum)
    lic = load_license(ws)
    if TIER_ORDER[lic.tier] >= TIER_ORDER[minimum]:
        return None
    return (
        f"This feature needs RevenueOS {TIER_LABELS[minimum]} (${TIER_PRICES[minimum]}/mo) or higher; "
        f"this install is on {TIER_LABELS[lic.tier]}. Run "
        f"`revenueos billing checkout --tier {minimum.value} --success-url <url> --cancel-url <url>` "
        "to upgrade, then `revenueos license install <key>` with the key from your confirmation email."
    )


# ── Stripe: vendor side ──────────────────────────────────────────────────────
def create_checkout_session(
    tier: Tier,
    success_url: str,
    cancel_url: str,
    *,
    secret_key: str,
    price_ids: dict[Tier, str],
    client: httpx.Client | None = None,
) -> str:
    """POSTs a subscription Checkout Session to Stripe and returns its hosted URL."""
    tier = Tier(tier)
    price_id = price_ids.get(tier)
    if price_id is None:
        # tolerate a dict keyed by tier value strings too
        price_id = price_ids.get(tier.value)  # type: ignore[call-overload]
    if not price_id:
        raise ValueError(f"no Stripe price id configured for tier {tier.value!r}")
    form = {
        "mode": "subscription",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        # Metadata round-trips onto the Checkout Session *and* the Subscription it
        # creates, so `handle_webhook` can map an event straight back to a tier without
        # a second Stripe API call.
        "metadata[tier]": tier.value,
        "subscription_data[metadata][tier]": tier.value,
    }
    owns_client = client is None
    http_client = client or httpx.Client()
    try:
        resp = http_client.post(
            f"{STRIPE_API_BASE}/checkout/sessions",
            data=form,
            headers={"Authorization": f"Bearer {secret_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
    finally:
        if owns_client:
            http_client.close()
    url = data.get("url")
    if not url:
        raise RuntimeError(f"Stripe did not return a checkout url: {data}")
    return url


def verify_webhook_signature(payload: bytes, sig_header: str, webhook_secret: str, tolerance: int = 300) -> bool:
    """Implements Stripe's `Stripe-Signature: t=...,v1=...` HMAC-SHA256 scheme."""
    if not sig_header or not webhook_secret:
        return False
    parts: dict[str, list[str]] = {}
    for item in sig_header.split(","):
        k, _, v = item.partition("=")
        if k:
            parts.setdefault(k.strip(), []).append(v.strip())
    timestamps, v1_sigs = parts.get("t"), parts.get("v1")
    if not timestamps or not v1_sigs:
        return False
    try:
        t = int(timestamps[0])
    except ValueError:
        return False
    if tolerance is not None and abs(time.time() - t) > tolerance:
        return False
    signed_payload = f"{timestamps[0]}.".encode("ascii") + payload
    expected = hmac.new(webhook_secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in v1_sigs)


def _price_id_from_event_object(obj: dict[str, Any]) -> str | None:
    for key in ("items", "line_items"):
        container = obj.get(key)
        if isinstance(container, dict):
            data = container.get("data") or []
            if data and isinstance(data[0], dict):
                price = data[0].get("price") or data[0].get("plan") or {}
                if isinstance(price, dict) and price.get("id"):
                    return str(price["id"])
    price = obj.get("price")
    if isinstance(price, str):
        return price
    return None


def _tier_from_event_object(obj: dict[str, Any], price_ids: dict[Tier, str]) -> Tier | None:
    meta = obj.get("metadata") or {}
    meta_tier = meta.get("tier") if isinstance(meta, dict) else None
    if meta_tier:
        try:
            return Tier(meta_tier)
        except ValueError:
            pass
    price_id = _price_id_from_event_object(obj)
    if price_id:
        for tier, pid in price_ids.items():
            if pid == price_id:
                return Tier(tier)
    return None


def _email_from_event_object(obj: dict[str, Any]) -> str | None:
    details = obj.get("customer_details")
    if isinstance(details, dict) and details.get("email"):
        return str(details["email"])
    return obj.get("customer_email") or obj.get("email")


def _append_license_row(ws: Workspace, row: dict[str, Any]) -> None:
    path = ws.data / "licenses.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def handle_webhook(
    payload: bytes,
    sig_header: str,
    *,
    webhook_secret: str,
    license_secret: str,
    price_ids: dict[Tier, str],
    ws: Workspace,
) -> dict[str, Any]:
    """Verifies the signature, then turns a Stripe subscription event into a licence.

    `checkout.session.completed` / `customer.subscription.created|updated` issue (or
    reissue) a licence for the mapped tier and append a row to data/licenses.jsonl.
    `customer.subscription.deleted` appends a revocation row. Returns
    `{"ok": True, "tier":..., "email":..., "key":...}` or `{"ok": False, "error":...}`.
    Never logs the key — it is only ever returned to the caller.
    """
    if not verify_webhook_signature(payload, sig_header, webhook_secret):
        return {"ok": False, "error": "invalid webhook signature"}
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        return {"ok": False, "error": "payload is not valid JSON"}
    event_type = event.get("type", "")
    obj: dict[str, Any] = ((event.get("data") or {}).get("object")) or {}

    if event_type in (
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
    ):
        email = _email_from_event_object(obj)
        if not email:
            return {"ok": False, "error": "missing customer email in event"}
        tier = _tier_from_event_object(obj, price_ids)
        if tier is None:
            return {"ok": False, "error": "could not map event to a tier (unknown price id / metadata)"}
        expires_at = (datetime.now(UTC) + _WEBHOOK_LICENSE_VALIDITY).isoformat(timespec="seconds")
        key = issue_license(tier, email, expires_at, license_secret)
        stripe_subscription = obj.get("subscription") if event_type == "checkout.session.completed" else obj.get("id")
        _append_license_row(
            ws,
            {
                "issued_at": now_iso(),
                "tier": tier.value,
                "email": email,
                "key": key,
                "stripe_customer": obj.get("customer"),
                "stripe_subscription": stripe_subscription,
            },
        )
        return {"ok": True, "tier": tier.value, "email": email, "key": key}

    if event_type == "customer.subscription.deleted":
        row = {
            "issued_at": now_iso(),
            "tier": None,
            "email": _email_from_event_object(obj),
            "key": None,
            "stripe_customer": obj.get("customer"),
            "stripe_subscription": obj.get("id"),
            "revoked": True,
        }
        _append_license_row(ws, row)
        return {"ok": True, "revoked": True, "stripe_customer": row["stripe_customer"], "stripe_subscription": row["stripe_subscription"]}

    return {"ok": False, "error": f"unhandled event type: {event_type!r}"}


# ── HTTP router (for the panel to delegate to) ──────────────────────────────
def _price_ids_from_env() -> dict[Tier, str]:
    mapping = {
        Tier.pro: os.environ.get("STRIPE_PRICE_PRO"),
        Tier.business: os.environ.get("STRIPE_PRICE_BUSINESS"),
        Tier.agency: os.environ.get("STRIPE_PRICE_AGENCY"),
    }
    return {tier: price_id for tier, price_id in mapping.items() if price_id}


def _header(headers: dict[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return v
    return None


def billing_http(path: str, method: str, body: bytes, headers: dict[str, str], ws: Workspace) -> tuple[int, str, str] | None:
    """A tiny router for the panel's HTTP handler to delegate billing routes to.

        POST /billing/webhook          — verify + handle a Stripe event
        GET  /billing/checkout?tier=.. — 302 to Stripe Checkout, or 400 with a reason

    Returns `(status, content_type, body)`, or `None` when `path` is not a billing route
    (the caller should fall through to its own routing in that case). For a 302, `body`
    is the redirect target — the caller is responsible for writing it as a `Location`
    header, the way `panel.py`'s POST handlers already do for their own redirects.
    """
    parsed = urlsplit(path)
    route = parsed.path
    verb = method.upper()

    if route == "/billing/webhook" and verb == "POST":
        webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
        license_secret = os.environ.get("REVENUEOS_LICENSE_SECRET")
        if not webhook_secret or not license_secret:
            return (500, "application/json", json.dumps({"ok": False, "error": "billing is not configured on this server"}))
        sig_header = _header(headers, "Stripe-Signature") or ""
        result = handle_webhook(
            body,
            sig_header,
            webhook_secret=webhook_secret,
            license_secret=license_secret,
            price_ids=_price_ids_from_env(),
            ws=ws,
        )
        return (200 if result.get("ok") else 400, "application/json", json.dumps(result))

    if route == "/billing/checkout" and verb == "GET":
        tier_str = (parse_qs(parsed.query).get("tier") or [""])[0]
        try:
            tier = Tier(tier_str)
        except ValueError:
            return (400, "text/plain; charset=utf-8", f"unknown tier {tier_str!r}")
        if tier is Tier.community:
            return (400, "text/plain; charset=utf-8", "the community tier is free; there is no checkout for it")
        secret_key = os.environ.get("STRIPE_SECRET_KEY")
        if not secret_key:
            return (400, "text/plain; charset=utf-8", "STRIPE_SECRET_KEY is not set")
        price_ids = _price_ids_from_env()
        if tier not in price_ids:
            return (400, "text/plain; charset=utf-8", f"STRIPE_PRICE_{tier.value.upper()} is not set")
        success_url = os.environ.get("REVENUEOS_BILLING_SUCCESS_URL")
        cancel_url = os.environ.get("REVENUEOS_BILLING_CANCEL_URL")
        if not success_url or not cancel_url:
            return (400, "text/plain; charset=utf-8", "REVENUEOS_BILLING_SUCCESS_URL / REVENUEOS_BILLING_CANCEL_URL are not set")
        try:
            url = create_checkout_session(tier, success_url, cancel_url, secret_key=secret_key, price_ids=price_ids)
        except Exception as exc:  # noqa: BLE001 — surfaced to the caller as a 400 reason
            return (400, "text/plain; charset=utf-8", str(exc))
        return (302, "text/plain; charset=utf-8", url)

    return None


# ── CLI ───────────────────────────────────────────────────────────────────
def _workspace_from_args(args: argparse.Namespace) -> Workspace:
    return Workspace.locate(Path(args.root) if getattr(args, "root", None) else None)


def _cmd_license_show(args: argparse.Namespace) -> int:
    ws = _workspace_from_args(args)
    lic = load_license(ws)
    print(f"tier:    {lic.tier.value}")
    print(f"email:   {lic.customer_email or '(none — community)'}")
    print(f"expires: {lic.expires_at}")
    print("features:")
    for f in features_for(lic.tier):
        print(f"  - {f}")
    return 0


def _cmd_license_install(args: argparse.Namespace) -> int:
    ws = _workspace_from_args(args)
    ws.data.mkdir(parents=True, exist_ok=True)
    (ws.data / "license.json").write_text(json.dumps({"key": args.key}), encoding="utf-8")
    lic = load_license(ws)
    if lic.tier is Tier.community:
        print(
            "licence installed, but it did not verify as a paid tier — check "
            "REVENUEOS_LICENSE_SECRET is configured on this install and the key is current. "
            "Running as Community."
        )
    else:
        print(f"licence installed: {TIER_LABELS[lic.tier]} for {lic.customer_email}, expires {lic.expires_at}")
    return 0


def _cmd_license_issue(args: argparse.Namespace) -> int:
    secret = os.environ.get("REVENUEOS_LICENSE_SECRET")
    if not secret:
        print("REVENUEOS_LICENSE_SECRET is not set (the vendor secret is required to issue licences)", file=sys.stderr)
        return 2
    tier = Tier(args.tier)
    expires_at = (datetime.now(UTC) + timedelta(days=args.days)).isoformat(timespec="seconds")
    key = issue_license(tier, args.email, expires_at, secret)
    print(key)
    return 0


def _cmd_billing_checkout(args: argparse.Namespace) -> int:
    secret_key = os.environ.get("STRIPE_SECRET_KEY")
    if not secret_key:
        print("STRIPE_SECRET_KEY is not set", file=sys.stderr)
        return 2
    tier = Tier(args.tier)
    price_ids = _price_ids_from_env()
    if tier not in price_ids:
        print(f"STRIPE_PRICE_{tier.value.upper()} is not set", file=sys.stderr)
        return 2
    try:
        url = create_checkout_session(tier, args.success_url, args.cancel_url, secret_key=secret_key, price_ids=price_ids)
    except Exception as exc:  # noqa: BLE001 — surfaced to the operator as a CLI error
        print(f"checkout failed: {exc}", file=sys.stderr)
        return 1
    print(url)
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    """Adds `license show|install|issue` and `billing checkout` to revenueos's argparse
    subparsers. Call from `cli.py`'s `build_parser()` as `billing.register(sub)`, where
    `sub` is the object `parser.add_subparsers()` returned."""
    lic = subparsers.add_parser("license", help="licence key management")
    lic_sub = lic.add_subparsers(dest="license_cmd", required=True)

    s = lic_sub.add_parser("show", help="show the installed licence and its tier/features")
    s.set_defaults(fn=_cmd_license_show)

    s = lic_sub.add_parser("install", help="install a licence key issued for this install")
    s.add_argument("key")
    s.set_defaults(fn=_cmd_license_install)

    s = lic_sub.add_parser("issue", help="issue a licence key (vendor use; needs REVENUEOS_LICENSE_SECRET)")
    s.add_argument("--tier", required=True, choices=[t.value for t in Tier])
    s.add_argument("--email", required=True)
    s.add_argument("--days", type=int, default=30)
    s.set_defaults(fn=_cmd_license_issue)

    billing = subparsers.add_parser("billing", help="Stripe billing")
    billing_sub = billing.add_subparsers(dest="billing_cmd", required=True)

    s = billing_sub.add_parser("checkout", help="print a Stripe Checkout URL (needs STRIPE_SECRET_KEY / STRIPE_PRICE_<TIER>)")
    s.add_argument("--tier", required=True, choices=[t.value for t in Tier if t is not Tier.community])
    s.add_argument("--success-url", required=True)
    s.add_argument("--cancel-url", required=True)
    s.set_defaults(fn=_cmd_billing_checkout)
