"""Nothing reaches a customer deliverable that is not for the customer: no tip jars, no operator identity."""
from __future__ import annotations

from revenueos.sanitize import SOLICIT_RE, deliverable_lint, strip_solicitations

TIP = "Your support can make a significant difference in our progress and innovation! via CashApp $Someone or https://buymeacoffee.com/someone Click Here to buy me a coffee!"


def test_strip_solicitations_removes_the_line_and_nothing_else():
    text = f"# Skill\n\n{TIP}\n\nDo the work.\n"
    out, n = strip_solicitations(text)
    assert n == 1 and TIP not in out and "Do the work." in out and out.startswith("# Skill")
    # a catalogue row that merely names a platform is not a solicitation
    row = "| **Buy Me a Coffee** | 88 | Creator | Profile | Creator page with product links. |\n"
    assert strip_solicitations(row) == (row, 0)
    assert not SOLICIT_RE.search(row)


def test_deliverable_lint_drops_foreign_identity_lines():
    text = (f"# Triage\n**Prepared for:** operator@me.example\n{TIP}\n"
            "Contact the salon at hello@salon.example or the booking line.\n"
            "Send the file to someone@gmail.example when done.\nUse tel:+61855500100 links.\n")
    out, notes = deliverable_lint(text, allowed_emails=set(), allowed_domains={"salon.example"})
    assert "Prepared for" not in out and TIP not in out and "someone@gmail.example" not in out
    assert "hello@salon.example" in out and "tel:+61855500100" in out
    assert any("solicitation" in n for n in notes) and any("someone@gmail.example" in n for n in notes)


def test_vendored_skills_carry_no_solicitations():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    hits = [p for base in ("skills", "agents", "capabilities") for p in (repo / base).rglob("*.md")
            if SOLICIT_RE.search(p.read_text(encoding="utf-8", errors="replace"))]
    assert hits == [], hits[:5]
