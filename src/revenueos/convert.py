"""CONVERT — the last link of the chain: prove the result, then turn it into revenue.

`billing.pay_on_result` already holds the commercial rule (free until the business has a measured
result it agreed with, then 14 more days, then continuous operation needs Pro). Nothing, however,
noticed when that clock started or ran out, nobody drafted the offer, and a payment only became a
licence if a hosted Stripe webhook existed. This module is that missing step, and it obeys the same
invariants as every worker:

  * it **reads** state and **drafts** — it never sends, never charges and never emails anybody;
    an offer or a licence delivery is a pending action a human approves and executes;
  * it is idempotent (one offer per first-measured-result date, one licence delivery per
    customer and billing period);
  * it never invents a number or a URL: the offer quotes the measured before → after that is in
    the store, and carries a payment link only when the workspace was actually given one.

    offer_state(ws, store)          none → clock_running → due → licensed
    ensure_offer_action(...)        drafts ONE "Offer Pro to <company>" follow-up
    offers_for_tenants(host_ws)     hosted mode: the host operator's offers, one per tenant
    deliver_licenses(ws, store, …)  a paying Stripe customer with no licence row → key + delivery draft
    measure_offer(store, …)         an executed offer becomes `subscribed 0 → 1` when they pay
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from . import billing
from .billing import FREE_DAYS_AFTER_FIRST_RESULT, TIER_ORDER, TIER_PRICES, Tier
from .paths import Workspace
from .store import Store

# Where the billing worker drops its last read of the paying customers, so the heartbeat can
# measure offers without a second Stripe call.
CUSTOMERS_EXPORT = "stripe-customers.json"

# A licence minted here lasts a little longer than a billing period, exactly like the webhook's.
LICENSE_VALIDITY_DAYS = 35

INSTALL_COMMAND = "revenueos license install <key>"
NO_EMAIL_NOTE = "no customer email on file: the offer is shown on the panel"
NO_SECRET_NOTE = ("no licence signing key is configured on this install (REVENUEOS_LICENSE_SIGNING_KEY / "
                  "REVENUEOS_LICENSE_SIGNING_KEY_FILE), so no key could be issued yet")


def _config(ws: Workspace, ctx: Any | None) -> dict[str, Any]:
    """`ctx.config` when a context is at hand, otherwise revenueos.yaml read straight off the
    workspace — so TODAY and the panel need not rebuild a BusinessContext just for one line."""
    cfg = getattr(ctx, "config", None)
    if isinstance(cfg, dict):
        return cfg
    try:
        import yaml

        return yaml.safe_load(ws.config.read_text(encoding="utf-8")) or {} if ws.config.exists() else {}
    except Exception:
        return {}


def _payment_link(ws: Workspace, ctx: Any | None) -> str | None:
    """The Pro payment link, or None. Never invented: a missing link stays missing."""
    cfg = _config(ws, ctx)
    link = ((cfg.get("billing") or {}) if isinstance(cfg, dict) else {}).get("payment_link")
    link = link or os.environ.get("REVENUEOS_PAYMENT_LINK_PRO")
    link = (link or "").strip()
    return link or None


def offer_state(ws: Workspace, store: Store, ctx: Any | None = None) -> dict[str, Any]:
    """Where this workspace stands on the pay-on-result clock.

        none            nothing measured yet — there is nothing to charge for
        clock_running   first measured result on <date>, N free days left
        due             the free days have run out
        licensed        a Pro (or higher) licence is installed

    The rule itself stays in `billing.pay_on_result`; this only names the state and adds the
    commercial facts a human needs to act on it.
    """
    verdict = billing.pay_on_result(ws, store)
    first = verdict.get("first_result_at") or (store.first_measured_at() if hasattr(store, "first_measured_at") else None)
    licensed = TIER_ORDER[Tier(verdict["tier"])] >= TIER_ORDER[Tier.pro]
    days_left = verdict.get("days_left")
    if licensed:
        state = "licensed"
    elif not first:
        state = "none"
    elif days_left and days_left > 0:
        state = "clock_running"
    else:
        state = "due"
        days_left = 0
    return {
        "state": state,
        "first_result_at": first,
        "days_left": days_left,
        "free_days": FREE_DAYS_AFTER_FIRST_RESULT,
        "tier": verdict["tier"],
        "price": TIER_PRICES[Tier.pro],
        "payment_link": _payment_link(ws, ctx),
        "install_command": INSTALL_COMMAND,
        "reason": verdict.get("reason"),
    }


def first_measured(store: Store) -> dict[str, Any] | None:
    """The measured result the clock started on: the earliest measured outcome on an executed action."""
    best = None
    for a in store.executed_actions(limit=1000):
        o = a.get("outcome") or {}
        if o.get("status") != "measured":
            continue
        if best is None or (o.get("measured_at") or "") < (best["measured_at"] or ""):
            best = {"action_id": a["id"], "title": a["title"], "action_type": a["action_type"],
                    "measured_at": o.get("measured_at"), "metric": o.get("metric"),
                    "before_value": o.get("before_value"), "after_value": o.get("after_value"),
                    "note": o.get("note")}
    return best


def _evidence_line(proof: dict[str, Any] | None) -> str:
    if not proof:
        return "a result you approved was measured"
    if proof.get("metric") and proof.get("after_value") is not None:
        what = f"{proof['metric']} {proof.get('before_value') or 0:g} → {proof['after_value']:g}"
    else:
        what = proof.get("note") or "measured"
    return f"[{proof['action_id']}] {proof['title']} — {what}"


def offer_email(company: str, state: dict[str, Any], proof: dict[str, Any] | None) -> str:
    """The offer, in plain words, containing only what is true in this workspace."""
    date = (state.get("first_result_at") or "")[:10]
    days = state.get("days_left") or 0
    clock = (f"RevenueOS has run free since then. {days} free day(s) remain."
             if state["state"] == "clock_running"
             else f"RevenueOS has run free for the {state['free_days']} days since then; those days have now ended.")
    link = state.get("payment_link")
    pay = (f"Pay here: {link}" if link
           else "Reply to this email and we will send the payment link (none is configured on this install).")
    return (
        f"On {date} RevenueOS measured a result for {company} on work you approved:\n"
        f"  {_evidence_line(proof)}\n\n"
        f"{clock}\n\n"
        f"RevenueOS Pro is ${state['price']}/month and keeps it running continuously — the workers on a schedule, "
        "the heartbeat, and the measurement of everything you approve.\n"
        f"{pay}\n"
        f"Then install the key on your install: {INSTALL_COMMAND}\n\n"
        "One-shot runs (`revenueos run …`) and the control panel stay free whatever you decide."
    )


def ensure_offer_action(
    ws: Workspace,
    store: Store,
    ctx: Any,
    *,
    customer_email: str | None,
    state: dict[str, Any] | None = None,
    company: str | None = None,
    evidence_store: Store | None = None,
    scope: str | None = None,
    run_id: int | None = None,
) -> int | None:
    """Draft ONE Pro offer while the clock is running or overdue. Returns the new action id, or
    None when the state does not call for an offer or the offer already exists.

    Nothing is sent: the action is `pending` and a human decides. `state` / `company` /
    `evidence_store` let the hosted operator draft a tenant's offer in the HOST workspace
    (see `offers_for_tenants`) without the tenant ever seeing another tenant's data.
    """
    st = state or offer_state(ws, store, ctx)
    if st["state"] not in ("clock_running", "due"):
        return None
    first = st.get("first_result_at")
    if not first:
        return None
    name = company or getattr(ctx, "company_name", "") or "this business"
    dedupe = f"offer:pro:{first[:10]}" + (f":{scope.strip().lower()}" if scope else "")
    proof = first_measured(evidence_store or store)
    body = offer_email(name, st, proof)
    context: dict[str, Any] = {
        "kind": "offer", "tier": Tier.pro.value, "offer_state": st["state"],
        "first_result_at": first, "days_left": st.get("days_left"),
        "payment_link": st.get("payment_link"), "offer_email": (customer_email or "").strip().lower() or None,
        "evidence_action_id": (proof or {}).get("action_id"),
        "before": {"subscribed": 0},
    }
    if customer_email:
        context.update({"executor": "send_email", "to": customer_email.strip(),
                        "subject": f"RevenueOS measured a result for {name} — what happens next"})
    else:
        context["note"] = NO_EMAIL_NOTE
        body = body + f"\n\n({NO_EMAIL_NOTE}.)"
    return store.create_action("follow_up", f"Offer Pro to {name}", body, run_id=run_id,
                               dedupe_key=dedupe, context=context)


def offers_for_tenants(host_ws: Workspace) -> list[dict[str, Any]]:
    """Hosted mode: one offer per tenant whose clock is running or due, drafted in the HOST
    workspace with the tenant's account email as the recipient. Returns [] when this install is
    not hosting accounts. A tenant's store is only ever read for its own state."""
    from .context import BusinessContext
    from .tenants import Accounts

    accounts = Accounts(host_ws.root)
    if not accounts.enabled():
        return []
    host_store = Store(host_ws.db)
    host_ctx = BusinessContext.load(host_ws)
    out: list[dict[str, Any]] = []
    for rec in accounts.list():
        email = rec["email"]
        tws = accounts.workspace_for(email)
        if tws is None:
            continue
        try:
            tstore = Store(tws.db)
            tctx = BusinessContext.load(tws)
            st = offer_state(tws, tstore, tctx)
            aid = ensure_offer_action(host_ws, host_store, host_ctx, customer_email=email, state=st,
                                      company=tctx.company_name, evidence_store=tstore, scope=email)
        except Exception as exc:  # one broken tenant workspace must not stop the others
            out.append({"email": email, "state": "unreadable", "error": f"{type(exc).__name__}: {exc}", "action_id": None})
            continue
        out.append({"email": email, "company": tctx.company_name, "state": st["state"],
                    "days_left": st.get("days_left"), "first_result_at": st.get("first_result_at"),
                    "action_id": aid})
    return out


# ── payment → licence, without a webhook ─────────────────────────────────────
def read_customers_export(ws: Workspace) -> list[dict[str, Any]]:
    """The billing worker's last read of the paying customers; [] when it has never run."""
    path = ws.exports / CUSTOMERS_EXPORT
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("customers") if isinstance(data, dict) else data
    return [r for r in (rows or []) if isinstance(r, dict)]


def write_customers_export(ws: Workspace, customers: list[dict[str, Any]]) -> None:
    (ws.exports / CUSTOMERS_EXPORT).write_text(
        json.dumps({"read_at": datetime.now(UTC).isoformat(timespec="seconds"), "customers": customers}, indent=2),
        encoding="utf-8")


def _license_body(email: str, key: str | None, expires_at: str) -> str:
    if not key:
        return (f"{email} is paying for RevenueOS Pro and has no licence key on file.\n\n"
                f"No key could be issued: {NO_SECRET_NOTE}.\n"
                "Set it, then run `revenueos run billing` again to issue and deliver the key.")
    return (f"Thank you for subscribing to RevenueOS Pro.\n\n"
            f"Your licence key (valid until {expires_at[:10]}, renewed automatically while the subscription is active):\n\n"
            f"{key}\n\n"
            f"Install it on your RevenueOS:\n\n    revenueos license install {key}\n\n"
            "Then `revenueos orchestrator` runs continuously.")


def deliver_licenses(ws: Workspace, store: Store, customers: list[dict[str, Any]], *,
                     run_id: int | None = None, ctx: Any = None) -> dict[str, Any]:
    """For every paying customer with no licence row yet: mint the key (when the vendor's Ed25519
    signing key is present), append the row to data/licenses.jsonl, and deliver it.

    THE ONE EXCEPTION TO "WORKERS NEVER SEND", AND WHY IT IS NARROW. Everything else in RevenueOS
    waits for a human, because everything else is the system's idea. This is not: the customer
    asked for it by paying, and a key sitting in a queue while they wait is a broken purchase, not
    a safeguard. The gate is Stripe itself — this runs only for an email the connected account
    currently reports as an ACTIVE subscriber, so nothing but a real payment can trigger it, and the
    only thing it can ever send is the licence template. Without a configured mailbox the action is
    left pending exactly as before: nothing is lost, delivery is just slower."""
    can_sign = billing.signing_key() is not None
    issued, created, skipped, delivered = 0, 0, 0, 0
    for cust in customers:
        email = (cust.get("email") or "").strip()
        if not email:
            skipped += 1
            continue
        if billing._latest_license_row(ws, customer=cust.get("customer"), email=email):
            skipped += 1
            continue
        period = str(cust.get("current_period_start") or cust.get("subscription") or "")
        expires_at = (datetime.now(UTC) + timedelta(days=LICENSE_VALIDITY_DAYS)).isoformat(timespec="seconds")
        key = None
        if can_sign:
            key = billing.issue_license(Tier.pro, email, expires_at)
            billing._append_license_row(ws, {
                "issued_at": billing.now_iso(), "tier": Tier.pro.value, "email": email, "key": key,
                "stripe_customer": cust.get("customer"), "stripe_subscription": cust.get("subscription"),
                "via": "billing-worker",
            })
            issued += 1
        context: dict[str, Any] = {"kind": "license_delivery", "tier": Tier.pro.value,
                                   "stripe_customer": cust.get("customer"), "stripe_subscription": cust.get("subscription"),
                                   "expires_at": expires_at if key else None}
        if key:
            context.update({"executor": "send_email", "to": email, "subject": "Your RevenueOS Pro licence key"})
        else:
            context["note"] = NO_SECRET_NOTE
        aid = store.create_action("follow_up", f"Deliver Pro licence to {email}", _license_body(email, key, expires_at),
                                  run_id=run_id, dedupe_key=f"license:deliver:{email.lower()}:{period}", context=context)
        created += 1 if aid else 0
        if aid and key and _mailbox_ready():
            delivered += 1 if _deliver_now(ws, store, aid, ctx) else 0
    return {"issued": issued, "actions_created": created, "skipped": skipped, "secret": can_sign,
            "delivered": delivered, "mailbox": _mailbox_ready()}


def _mailbox_ready() -> bool:
    """A real send needs a password; a dry run counts, because it proves the path without posting."""
    return bool(os.environ.get("SMTP_PASSWORD")) or os.environ.get("REVENUEOS_DRY_RUN") == "1"


def _deliver_now(ws: Workspace, store: Store, action_id: int, ctx: Any = None) -> bool:
    """Send the licence immediately and record it, or leave the action pending for a human.

    A failure here must never lose the key: the licence row is already written, the action still
    holds the body, and the next run finds it unsent. A buyer waiting is recoverable; a buyer with
    no key and no record is not."""
    from .context import BusinessContext
    from .workers import execute_action

    if not store.get_action(action_id):
        return False
    # BusinessContext(ws) is an EMPTY context: its config defaults to {}, so smtp.host is absent and
    # every send fails with "SMTP not configured" even on a correctly configured workspace. Use the
    # caller's loaded context, and load one only if we were given none.
    ctx = ctx if ctx is not None else BusinessContext.load(ws)
    try:
        store.set_action_status(action_id, "approved")
        outcome = execute_action(ws, store, ctx, None, store.get_action(action_id))
        if str(outcome).strip().lower().startswith("not "):
            store.set_action_status(action_id, "pending")
            store.record_outcome(action_id, "pending", note=f"licence not delivered: {str(outcome)[:180]}")
            return False
        store.set_action_status(action_id, "executed")
        store.record_outcome(action_id, "measured", metric="licence_delivered", before=0, after=1,
                             note=f"paid subscription, key delivered automatically: {str(outcome)[:150]}")
        return True
    except Exception as exc:  # noqa: BLE001 — a delivery failure must leave the action recoverable
        store.set_action_status(action_id, "pending")
        store.record_outcome(action_id, "pending",
                             note=f"licence delivery failed, still queued: {type(exc).__name__}: {exc}"[:220])
        return False


# ── measurement: did the offer convert? ──────────────────────────────────────
def measure_offer(store: Store, stripe_customers: list[dict[str, Any]]) -> int:
    """An executed offer is `measured` (`subscribed 0 → 1`) once a paying customer with the same
    email appears in the billing worker's read; until then it stays `pending`. Nothing else is
    inferred — no payment, no metric. Returns the number newly measured."""
    paying = {(c.get("email") or "").strip().lower() for c in stripe_customers if c.get("email")}
    measured = 0
    for a in store.executed_actions(limit=1000):
        c = a.get("context") or {}
        if c.get("kind") != "offer":
            continue
        o = store.latest_outcome(a["id"]) or {}
        if o.get("status") == "measured":
            continue
        email = (c.get("offer_email") or c.get("to") or "").strip().lower()
        if email and email in paying:
            store.record_outcome(a["id"], "measured", metric="subscribed", before_value=0, after_value=1,
                                 before={"subscribed": 0}, after={"subscribed": 1, "email": email},
                                 note=f"{email} is a paying RevenueOS customer")
            measured += 1
        elif not o:
            store.record_outcome(a["id"], "pending", metric="subscribed", before_value=0,
                                 note="offer sent; no subscription for this address yet")
    return measured


def offer_line(state: dict[str, Any] | None) -> str | None:
    """The single line TODAY and the panel show while the clock is running or overdue."""
    if not state or state.get("state") not in ("clock_running", "due"):
        return None
    date = (state.get("first_result_at") or "")[:10]
    days = state.get("days_left") or 0
    clock = f"free for {days} more day(s)" if state["state"] == "clock_running" else "the free days have ended"
    link = state.get("payment_link") or "ask for the link"
    return f"First measured result on {date} · {clock} · Pro ${state['price']}/month: {link} · `{INSTALL_COMMAND}`"
