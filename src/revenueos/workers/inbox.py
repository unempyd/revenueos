"""INBOX — the inbound half of the sales loop (reply / bounce / unsubscribe feedback).

ai-sales-agent ships the outbound side complete and leaves the inbound side unbuilt; the
second research pass picked mailgun/talon (Apache-2.0) to close it.  This worker:

  1. reads recent messages from the sending mailbox over IMAP (stdlib imaplib; the same
     mailbox credentials OpenOutreach uses: revenueos.yaml `imap.host`, `sender.email`,
     env IMAP_PASSWORD or SMTP_PASSWORD),
  2. matches senders to `email_sends` rows,
  3. strips quoted history with the vendored talon `extract_from_plain`,
  4. classifies: STOP/unsubscribe → suppression list; delivery failure → bounced; anything
     else → `replied_at` + a `follow_up` action carrying the clean reply text.

Messages can also be dropped as .eml files into data/exports/inbox/ for offline runs and tests.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
from email.message import Message as MimeMessage
from email.utils import parseaddr
from typing import Any

from ..context import BusinessContext
from ..llm import LLM
from ..paths import Workspace
from ..store import Store
from ..vendor.talon import quotations
from . import WorkerResult

STOP_RE = re.compile(r"^\s*(stop|unsubscribe|remove me|opt out|no thanks)\b", re.IGNORECASE)
BOUNCE_SUBJECT_RE = re.compile(r"(undeliverable|delivery (status )?notification|mail delivery failed|returned mail|delivery failure)", re.IGNORECASE)
BOUNCE_SENDER_RE = re.compile(r"^(mailer-daemon|postmaster)@", re.IGNORECASE)


def _plain_body(msg: MimeMessage) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get("Content-Disposition", "").startswith("attachment"):
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    payload = msg.get_payload(decode=True) or b""
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")


def classify(msg: MimeMessage) -> tuple[str, str, str]:
    """→ (kind, from_email, clean_text) where kind ∈ {'bounce', 'stop', 'reply'}."""
    from_email = parseaddr(msg.get("From", ""))[1].lower()
    subject = msg.get("Subject", "") or ""
    body = _plain_body(msg)
    clean = quotations.extract_from_plain(body).strip()
    if BOUNCE_SENDER_RE.match(from_email) or BOUNCE_SUBJECT_RE.search(subject):
        # the failed recipient is usually in the body / Final-Recipient header
        m = re.search(r"(?:Final-Recipient:\s*rfc822;\s*|<)([\w.+-]+@[\w.-]+)", body)
        return "bounce", (m.group(1).lower() if m else from_email), clean
    if STOP_RE.match(clean) or STOP_RE.match(subject):
        return "stop", from_email, clean
    return "reply", from_email, clean


def read_eml_drops(ws: Workspace) -> list[MimeMessage]:
    drop = ws.exports / "inbox"
    if not drop.is_dir():
        return []
    out = []
    for p in sorted(drop.glob("*.eml")):
        out.append(email.message_from_bytes(p.read_bytes()))
        p.rename(p.with_suffix(".eml.done"))
    return out


def read_imap(cfg: dict[str, Any], limit: int = 50) -> list[MimeMessage]:
    imap = cfg.get("imap") or {}
    host = imap.get("host") or os.environ.get("IMAP_HOST")
    user = imap.get("user") or (cfg.get("sender") or {}).get("email")
    password = os.environ.get("IMAP_PASSWORD") or os.environ.get("SMTP_PASSWORD")
    if not (host and user and password):
        return []
    msgs: list[MimeMessage] = []
    with imaplib.IMAP4_SSL(host, int(imap.get("port") or 993)) as m:
        m.login(user, password)
        m.select("INBOX")
        _typ, data = m.search(None, "UNSEEN")
        ids = (data[0] or b"").split()[-limit:]
        for i in ids:
            _typ, parts = m.fetch(i, "(RFC822)")
            for part in parts:
                if isinstance(part, tuple):
                    msgs.append(email.message_from_bytes(part[1]))
    return msgs


class InboxWorker:
    name = "inbox"
    description = "Detect replies, bounces and unsubscribes to sent outreach; queue reply follow-ups."
    upstream = "mailgun/talon quotations + ai-sales-agent email_sends/unsubscribes model"

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult:
        messages = read_eml_drops(ws) + read_imap(ctx.config)
        sends_by_email: dict[str, dict[str, Any]] = {}
        for row in store.recent_sends():
            sends_by_email.setdefault(row["to_email"].lower(), row)
        replies = bounces = stops = created = 0
        for msg in messages:
            kind, addr, clean = classify(msg)
            send = sends_by_email.get(addr)
            if kind == "stop":
                stops += 1
                store.add_unsubscribe(addr, via="reply_stop")
                if send:
                    store.mark_send(send["id"], unsubscribed=True)
                    store.transition_lead(send["lead_id"], "dead", {"reason": "unsubscribed"})
                continue
            if kind == "bounce":
                bounces += 1
                if send:
                    store.mark_send(send["id"], bounced=True)
                    store.transition_lead(send["lead_id"], "dead", {"reason": "bounced"})
                continue
            if not send:
                continue  # not a reply to something we sent
            replies += 1
            store.mark_send(send["id"], replied=True)
            store.transition_lead(send["lead_id"], "replied", {"snippet": clean[:200]})
            aid = store.create_action(
                "follow_up", f"Reply from {addr}: {(msg.get('Subject') or '')[:70]}", clean[:1500] or "(empty reply)",
                run_id=run_id, dedupe_key=f"reply:{send['id']}:{msg.get('Message-ID') or addr}",
                context={"lead_id": send["lead_id"], "send_id": send["id"], "to": addr, "kind": "reply"},
            )
            created += 1 if aid else 0
        return WorkerResult(ok=True, summary=f"{len(messages)} message(s): {replies} replies, {bounces} bounces, {stops} unsubscribes; {created} follow-up(s) queued.",
                            actions_created=created, details={"replies": replies, "bounces": bounces, "stops": stops})
