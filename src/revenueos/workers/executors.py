"""Executors that change the world, all behind the same two gates: the human approved the action, and the
connection it needs has 'allow changes' on. Each returns one sentence for RESULTS and writes what it
did into the action's outcome note; measure re-checks the effect afterwards.

    site_deploy   apply a <head> fix to a site in git (github_site) or a WordPress page → measured by re-crawl
    ads_pause     pause a wasting campaign (google | meta)                          → measured by next spend read
    ads_budget    set a campaign's daily budget (google | meta)                     → measured by next spend read
    book_call     put a call on the calendar (google) or send an .ics invite (mailbox)
    send_invoice  create and send a Stripe invoice to a lead                        → measured by Stripe paid status
    publish_post  publish a produced content deliverable to WordPress (a second, separate
                  approval from producing it)                                       → measured by fetching the URL
"""
from __future__ import annotations

import html as _html
import os
import re
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


_PROVENANCE_RE = re.compile(r"\s*\[(?:derived|hypothesis|from the website)[^\]]*\]", re.I)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_UL_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_OL_RE = re.compile(r"^\s*\d+\.\s+(.*)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _strip_provenance(text: str) -> str:
    """Remove RevenueOS's own onboarding-provenance tags ('[derived from the website]', '[hypothesis]')
    before anything is published under the customer's name (same idea as outreach._clean)."""
    return _PROVENANCE_RE.sub("", text or "").strip()


def _inline_html(text: str) -> str:
    text = _html.escape(text, quote=False)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)
    return text


def markdown_to_html(md: str) -> str:
    """A small, dependency-free markdown → HTML converter: headings, paragraphs, ordered/unordered
    lists, links and bold. Good enough for a produced deliverable going to WordPress; not a general
    markdown implementation."""
    html_parts: list[str] = []
    para: list[str] = []
    list_items: list[str] = []
    list_tag: str | None = None

    def flush_para() -> None:
        if para:
            html_parts.append("<p>" + _inline_html(" ".join(para)) + "</p>")
            para.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_items:
            html_parts.append(f"<{list_tag}>" + "".join(f"<li>{_inline_html(i)}</li>" for i in list_items) + f"</{list_tag}>")
            list_items.clear()
            list_tag = None

    for raw in (md or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush_para()
            flush_list()
            continue
        h = _HEADING_RE.match(line)
        ul = _UL_RE.match(line)
        ol = None if ul else _OL_RE.match(line)
        if h:
            flush_para()
            flush_list()
            level = len(h.group(1))
            html_parts.append(f"<h{level}>{_inline_html(h.group(2))}</h{level}>")
        elif ul:
            flush_para()
            if list_tag != "ul":
                flush_list()
                list_tag = "ul"
            list_items.append(ul.group(1))
        elif ol:
            flush_para()
            if list_tag != "ol":
                flush_list()
                list_tag = "ol"
            list_items.append(ol.group(1))
        else:
            flush_list()
            para.append(line.strip())
    flush_para()
    flush_list()
    return "\n".join(html_parts)


def _split_deliverable(text: str) -> tuple[str, str]:
    """execute_content writes '# <title>\\n\\n<body>'; split that heading back out so it isn't
    duplicated inside the WordPress post body (the post title field already carries it)."""
    lines = (text or "").splitlines()
    if lines and lines[0].startswith("# "):
        return lines[0][2:].strip(), "\n".join(lines[1:]).lstrip("\n")
    return "", text or ""


def execute_publish_post(ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, action: dict[str, Any]) -> str:
    """Publish a deliverable execute_content already wrote to data/outputs/ to WordPress. Requires an
    approved action (the caller only reaches here through execute_action) AND a wordpress connection
    with 'allow changes' on; never called from a worker, and never publishes without both."""
    from ..connections import wordpress

    c = _ctx(action)
    rel = c.get("deliverable")
    if not rel:
        return "not published: no deliverable file was recorded on this action"
    path = ws.root / rel
    if not path.exists():
        return f"not published: the deliverable file is missing ({rel})"
    text = path.read_text(encoding="utf-8")
    title, body = _split_deliverable(text)
    title = title or action["title"].removeprefix("Publish: ").strip() or action["title"]
    html_body = markdown_to_html(_strip_provenance(body))
    status = "draft" if c.get("publish_status") == "draft" else "publish"
    cs = ConnectionStore(ws)
    try:
        res = wordpress.create_post(cs, title, html_body, status=status)
    except PermissionDenied as e:
        return f"not published: {e}"
    published_at = datetime.now(UTC).isoformat(timespec="seconds")
    store.update_action_context(action["id"], published_url=res.get("link"), post_id=res.get("id"), published_at=published_at)
    return f"published {title!r} to {res.get('link')} (WordPress post {res.get('id')}, {res.get('status') or status})"


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
    "publish_post": execute_publish_post,
}
