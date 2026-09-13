"""'Book the call' without a calendar API: a standards-compliant iCalendar invite (RFC 5545) sent from the
business's own mailbox. Every mail client turns it into a calendar entry with Accept / Decline; the reply
lands in the inbox worker. When Google Calendar is connected, google.book_call is used instead.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

NAME = "calendar_ics"


def describe() -> dict[str, Any]:
    return {
        "label": "Calendar invites by email (.ics)",
        "reads": "nothing",
        "writes": "sends a calendar invitation from your mailbox for an approved booking",
        "needs": "the mailbox already configured for outreach (smtp in revenueos.yaml + SMTP_PASSWORD)",
        "how": "connected automatically when the mailbox is",
    }


def ready() -> bool:
    import os

    return bool(os.environ.get("SMTP_PASSWORD"))


def missing() -> list[str]:
    return [] if ready() else ["SMTP_PASSWORD + smtp.host in revenueos.yaml"]


def _ics_dt(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def build_invite(*, organizer_name: str, organizer_email: str, attendee_email: str, subject: str, start: datetime,
                 minutes: int = 15, description: str = "", location: str = "") -> tuple[str, str]:
    uid = f"{uuid.uuid4()}@revenueos"
    end = start + timedelta(minutes=minutes)
    esc = lambda s: s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")  # noqa: E731
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//RevenueOS//EN", "METHOD:REQUEST", "BEGIN:VEVENT",
        f"UID:{uid}", f"DTSTAMP:{_ics_dt(datetime.now(UTC))}", f"DTSTART:{_ics_dt(start)}", f"DTEND:{_ics_dt(end)}",
        f"SUMMARY:{esc(subject)}", f"DESCRIPTION:{esc(description)}",
        f"ORGANIZER;CN={esc(organizer_name)}:mailto:{organizer_email}",
        f"ATTENDEE;CN={esc(attendee_email)};ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{attendee_email}",
    ]
    if location:
        lines.append(f"LOCATION:{esc(location)}")
    lines += ["STATUS:CONFIRMED", "END:VEVENT", "END:VCALENDAR"]
    return uid, "\r\n".join(lines) + "\r\n"


def invite_message(*, sender_name: str, sender_email: str, to_email: str, subject: str, text: str, ics: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{sender_name} <{sender_email}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_attachment(ics.encode("utf-8"), maintype="text", subtype="calendar", filename="invite.ics",
                       params={"method": "REQUEST", "charset": "utf-8"})
    return msg
