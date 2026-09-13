"""Executors that change the world, all behind the same two gates: the human approved the action, and the
connection it needs has 'allow changes' on. Each returns one sentence for RESULTS and writes what it
did into the action's outcome note; measure re-checks the effect afterwards.

    site_deploy   apply a <head> fix to a site in git (github_site) or a WordPress page → measured by re-crawl
    ads_pause     pause a wasting campaign (google | meta)                          → measured by next spend read
    ads_budget    set a campaign's daily budget (google | meta)                     → measured by next spend read
    book_call     put a call on the calendar (google) or send an .ics invite (mailbox)
    send_invoice  create and send a Stripe invoice to a lead                        → measured by Stripe paid status
"""
from __future__ import annotations

import os
import smtplib
from datetime import UTC, datetime, timedelta
from typing import Any

from ..connections import ConnectionStore, PermissionDenied
from ..context import BusinessContext
from ..llm import LLM
from ..paths import Workspace
from ..store import Store


def _ctx(action: dict[str, Any]) -> dict[str, Any]:
    return action.get("context") or {}


def execute_site_deploy(ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, action: dict[str, Any]) -> str:
    from ..connections import github_site, wordpress

    c = _ctx(action)
    cs = ConnectionStore(ws)
    fix = c["fix"]
    try:
        if cs.get("github_site"):
            res = github_site.deploy_fix(cs, file=c.get("file") or "index.html", fix=fix, message=f"RevenueOS: {action['title']}")
        elif cs.get("wordpress"):
            res = wordpress.apply_fix(cs, int(c["page_id"]), fix)
        else:
            return "not deployed: no site connection (connect the site's git repo or WordPress on the Connections page)"
    except PermissionDenied as e:
        return f"not deployed: {e}"
    if not res.get("changed"):
        return f"nothing to change: {res.get('note', 'already in place')}"
    where = res.get("commit") and f"commit {res['commit']} on {res.get('repo')}@{res.get('branch')}" or f"page {res.get('page')}"
    return f"deployed {fix['kind']} fix to {res.get('file') or res.get('link') or ''} ({where}); the host publishes it within minutes"


def execute_ads_pause(ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, action: dict[str, Any]) -> str:
    from ..connections import google, meta

    c = _ctx(action)
    cs = ConnectionStore(ws)
    try:
        if c.get("platform") == "meta":
            res = meta.pause(cs, c["campaign_id"])
        else:
            res = google.ads_pause(cs, c["campaign_id"], c.get("customer_id"))
    except PermissionDenied as e:
        return f"not paused: {e}"
    return f"paused campaign {c.get('campaign_name') or c['campaign_id']} on {c.get('platform', 'google')} ({res.get('status')})"


def execute_ads_budget(ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, action: dict[str, Any]) -> str:
    from ..connections import google, meta

    c = _ctx(action)
    cs = ConnectionStore(ws)
    amount = float(c["daily_amount"])
    try:
        if c.get("platform") == "meta":
            meta.set_budget(cs, c["campaign_id"], amount)
        else:
            google.ads_set_budget(cs, c["budget_resource"], amount, c.get("customer_id"))
    except PermissionDenied as e:
        return f"budget unchanged: {e}"
    return f"set daily budget of {c.get('campaign_name') or c['campaign_id']} to {amount:.2f} (was {float(c.get('previous', 0)):.2f})"


def execute_book_call(ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, action: dict[str, Any]) -> str:
    from ..connections import calendar_ics, google

    c = _ctx(action)
    cs = ConnectionStore(ws)
    start = datetime.fromisoformat(c["start"]) if c.get("start") else (datetime.now(UTC) + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    subject = c.get("subject") or f"{ctx.company_name} × {c.get('name') or c['to']}: 15 minutes"
    text = c.get("text") or f"Calendar invitation for a 15-minute call with {ctx.company_name}. Reply to this email to move it."
    if cs.get("google") and cs.get("google").allow_write:
        try:
            res = google.book_call(cs, attendee_email=c["to"], subject=subject, start=start, description=text)
            return f"booked {start:%Y-%m-%d %H:%M} with {c['to']} on Google Calendar ({res.get('meet') or res.get('html_link')})"
        except PermissionDenied as e:
            return f"not booked: {e}"
    sender = ctx.config.get("sender") or {}
    smtp = ctx.config.get("smtp") or {}
    password = os.environ.get("SMTP_PASSWORD")
    uid, ics = calendar_ics.build_invite(organizer_name=sender.get("name") or ctx.company_name, organizer_email=sender.get("email", ""),
                                         attendee_email=c["to"], subject=subject, start=start, description=text)
    msg = calendar_ics.invite_message(sender_name=sender.get("name") or ctx.company_name, sender_email=sender.get("email", ""),
                                      to_email=c["to"], subject=subject, text=text, ics=ics)
    if os.environ.get("REVENUEOS_DRY_RUN") == "1" or not (smtp.get("host") and password and sender.get("email")):
        (ws.outputs / f"invite-{uid[:8]}.ics").write_text(ics, encoding="utf-8")
        return f"invite written to data/outputs/invite-{uid[:8]}.ics (dry run or no mailbox); nothing sent"
    with smtplib.SMTP(smtp["host"], int(smtp.get("port") or 587), timeout=30) as s:
        s.starttls()
        s.login(smtp.get("user") or sender["email"], password)
        s.send_message(msg)
    return f"invite for {start:%Y-%m-%d %H:%M} sent to {c['to']} from {sender['email']}"


def execute_send_invoice(ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, action: dict[str, Any]) -> str:
    from ..connections import stripe_conn

    c = _ctx(action)
    cs = ConnectionStore(ws)
    try:
        res = stripe_conn.send_invoice(cs, email=c["to"], name=c.get("name") or c["to"], description=c.get("description") or action["title"],
                                       amount=float(c["amount"]), currency=c.get("currency") or "usd", send=bool(c.get("send", True)))
    except PermissionDenied as e:
        return f"no invoice: {e}"
    if c.get("lead_id"):
        store.upsert_lead_fields(c["lead_id"], deal_value=float(c["amount"]))
    return (f"invoice {res['invoice']} for {res['amount']:.2f} {res['currency'].upper()} {res['status']} to {c['to']}"
            + (f" — {res['hosted_invoice_url']}" if res.get("hosted_invoice_url") else ""))


EXECUTORS = {
    "site_deploy": execute_site_deploy,
    "ads_pause": execute_ads_pause,
    "ads_budget": execute_ads_budget,
    "book_call": execute_book_call,
    "send_invoice": execute_send_invoice,
}
