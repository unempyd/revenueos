"""What must never reach a customer: an upstream author's tip jar, the operator's identity, a payment handle.

Used at three points: `scripts/vendor.py` when skills are assembled, `execute_content` when a SKILL.md is
handed to the model, and `deliverable_lint` on every deliverable before it is written to data/outputs/.
"""
from __future__ import annotations

import re

# A line asking the reader for money for the skill's author (not a catalogue row about a platform).
SOLICIT_RE = re.compile(
    r"cashapp\s*\$|\bvia cashapp\b|buymeacoffee\.com/\w|ko-fi\.com/\w|patreon\.com/\w|paypal\.me/\w|"
    r"support can make a significant difference|buy me a coffee!|tip jar|venmo\s*@",
    re.I,
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PREPARED_FOR_RE = re.compile(r"^\s*[*_#>\s]*(?:prepared for|prepared by|requested by|prepared on behalf of)\b.*$", re.I)


def strip_solicitations(text: str) -> tuple[str, int]:
    """Remove every line that solicits money for someone other than the business. Returns (text, lines removed)."""
    kept, removed = [], 0
    for line in text.splitlines():
        if SOLICIT_RE.search(line):
            removed += 1
            continue
        kept.append(line)
    return "\n".join(kept) + ("\n" if text.endswith("\n") else ""), removed


def deliverable_lint(text: str, *, allowed_emails: set[str] | frozenset[str] = frozenset(),
                     allowed_domains: set[str] | frozenset[str] = frozenset()) -> tuple[str, list[str]]:
    """Clean a deliverable for the customer and report what was removed.

    - solicitation lines are dropped;
    - 'Prepared for: someone@else' lines are dropped (the deliverable is for the business, full stop);
    - any line carrying an email that is neither the business's nor on its domain is dropped;
    - a payment handle that survives is an error (raised by the caller)."""
    text, n = strip_solicitations(text)
    notes = [f"removed {n} solicitation line(s)"] if n else []
    allowed = {e.lower() for e in allowed_emails}
    domains = {d.lower() for d in allowed_domains}
    kept = []
    for line in text.splitlines():
        if PREPARED_FOR_RE.match(line):
            notes.append(f"removed line: {line.strip()[:80]}")
            continue
        foreign = [e for e in EMAIL_RE.findall(line)
                   if e.lower() not in allowed and e.split("@")[1].lower() not in domains]
        if foreign:
            notes.append(f"removed line with address not belonging to the business: {foreign[0]}")
            continue
        kept.append(line)
    return "\n".join(kept).strip() + "\n", notes
