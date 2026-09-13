"""Business context: the one questionnaire, and everything workers know about the customer.

Schema and validator come from markster-os (company-context/ — 12 canon files with fixed
headings, validated by the vendored validate_markster_os.py).  Persistent corrections use
ai-cmo-operator's append-to-top CORRECTIONS.md format (learning-loop/CORRECTIONS.md).
Machine-facing settings (website, connections, sending identity) live in revenueos.yaml.
"""
from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import yaml

from .paths import Workspace
from .vendor.markster import validate_markster_os as mk

# The questionnaire. (key, prompt, company-context target) — targets are (file, heading).
QUESTIONS: list[tuple[str, str, tuple[str, str] | None]] = [
    ("company_name", "Company name", None),
    ("website", "Website URL (https://...)", None),
    ("category", "In one line: what category are you in?", ("identity.md", "Category")),
    ("what_we_do", "What do you sell, in one plain sentence?", ("identity.md", "One-Sentence Definition")),
    ("primary_audience", "Who is the target customer? (role, company type, size, geography)", ("audience.md", "Primary ICP")),
    ("pain_points", "What problem do they have that you solve?", ("audience.md", "Pain Points")),
    ("core_offer", "Main offer and price point", ("offer.md", "Core Offer")),
    ("differentiators", "Why you and not the alternatives?", ("messaging.md", "Differentiators")),
    ("competitors", "Top competitors (comma-separated names or domains)", None),
    ("channels", "Channels you already use (comma-separated: website, cold email, linkedin, google ads, meta ads, seo, ...)", ("channels.md", "Primary Channels")),
    ("tone", "Tone of voice (e.g. clear, direct, warm)", ("voice.md", "Tone")),
    ("sender_name", "Name that outreach is sent from", None),
    ("sender_email", "Email address outreach is sent from", None),
    ("booking_url", "Booking / demo link (optional)", None),
    # no canon target: the objective is machine state (store: objectives), not a company-context file
    ("objective", "What is the one revenue objective for the next 90 days?", None),
]

PLACEHOLDER_MANIFEST_NAME = "Your Company"


@dataclass
class BusinessContext:
    ws: Workspace
    manifest: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, ws: Workspace) -> BusinessContext:
        ctx = cls(ws)
        mpath = ws.company_context / "manifest.json"
        ctx.manifest = json.loads(mpath.read_text()) if mpath.exists() else {}
        ctx.config = yaml.safe_load(ws.config.read_text()) or {} if ws.config.exists() else {}
        for name in ctx.manifest.get("required_files", []):
            p = ws.company_context / name
            if p.exists():
                ctx.files[name] = p.read_text(encoding="utf-8")
        return ctx

    # ── state ──
    def is_onboarded(self) -> bool:
        return bool(self.manifest.get("company_name")) and self.manifest["company_name"] != PLACEHOLDER_MANIFEST_NAME

    @property
    def company_name(self) -> str:
        return self.manifest.get("company_name", PLACEHOLDER_MANIFEST_NAME)

    @property
    def website(self) -> str | None:
        return self.config.get("website")

    @property
    def competitors(self) -> list[str]:
        return list(self.config.get("competitors") or [])

    @property
    def channels(self) -> list[str]:
        return list(self.config.get("channels") or [])

    def section(self, file: str, heading: str) -> str:
        return extract_section(self.files.get(file, ""), heading)

    def brain(self) -> dict[str, Any]:
        """The 'Product Brain' shape pulse-cmo's relevance gate and query builder expect."""
        category = (self.manifest.get("category") or self.section("identity.md", "Category")).strip()
        pains = [_short(p) for p in _bullets(self.section("audience.md", "Pain Points"))][:6]
        competitors = [_brand(c) for c in self.competitors]
        pains_full = _bullets(self.section("audience.md", "Pain Points"))[:6]
        diffs = _bullets(self.section("messaging.md", "Differentiators"))[:6]
        return {  # the shape pulse-cmo's product_brain.brain_context_block renders
            "name": self.company_name,
            "category": category,
            "one_liner": self.section("identity.md", "One-Sentence Definition") or self.manifest.get("core_offer", ""),
            "wedge": {"capability": diffs[0] if diffs else category, "best_fit_segment": _short(self.section("audience.md", "Primary ICP"), 12),
                      "why_they_care": pains_full[0] if pains_full else ""},
            "icp": [{"who": self.section("audience.md", "Primary ICP"), "pains": pains_full}],
            "entities": {"competitors": competitors, "feature_names": diffs},
            "icp_vocabulary": _bullets(self.section("audience.md", "Buyer Language"))[:10],
            "disqualifiers": _bullets(self.section("audience.md", "Disqualifiers"))[:6],
            "search_seeds": {
                "pain": pains,
                "question": [f"{category} recommendation", f"which {category}"] if category else [],
                "comparison": [f"{category} comparison"] if category else [],
                "switching": [f"switching {category}", f"moving away from {competitors[0]}"] if category and competitors else [],
                "shopping": [f"best {category}", category] if category else [],
            },
        }

    def prompt_summary(self, max_chars: int = 6000) -> str:
        """Selective context loading (ai-cmo-operator rule: never load everything)."""
        parts = [f"# {self.company_name}"]
        for file, headings in (
            ("identity.md", ("One-Sentence Definition", "Category", "What We Do", "What We Do Not Do")),
            ("audience.md", ("Primary ICP", "Pain Points", "Desired Outcomes", "Objections")),
            ("offer.md", ("Core Offer", "Outcome Statement")),
            ("messaging.md", ("One-Liner", "Differentiators", "Banned Phrases")),
            ("voice.md", ("Tone", "Avoid")),
            ("truth-rules.md", ("No Fabrication",)),
        ):
            for h in headings:
                body = self.section(file, h)
                if body and not _is_placeholder(body):
                    parts.append(f"## {h}\n{body}")
        text = "\n\n".join(parts)
        corrections = self.recent_corrections()
        if corrections:
            text += "\n\n<corrections>\n" + corrections + "\n</corrections>"
        from .learning import recent_lessons  # local: learning imports roles, which imports this module

        lessons = recent_lessons(self.ws)
        if lessons:
            text += "\n\n<lessons>\n" + lessons + "\n</lessons>"
        return text[:max_chars]

    def recent_corrections(self, days: int = 30, max_chars: int = 2500) -> str:
        p = self.ws.corrections
        if not p.exists():
            return ""
        text = p.read_text(encoding="utf-8")
        blocks = re.split(r"(?m)^## (?=\d{4}-\d{2}-\d{2})", text)[1:]
        cutoff = datetime.now(UTC).timestamp() - days * 86400
        keep = []
        for b in blocks:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", b)
            if m and datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=UTC).timestamp() >= cutoff:
                keep.append("## " + b.strip())
        return "\n\n".join(keep)[:max_chars]

    def add_correction(self, title: str, context: str, correction: str, apply_when: str, source: str = "cli") -> None:
        """ai-cmo-operator format: append-only, newest at top, never delete."""
        p = self.ws.corrections
        existing = p.read_text(encoding="utf-8") if p.exists() else "# CORRECTIONS\n\n"
        block = (
            f"## {datetime.now(UTC):%Y-%m-%d} — {title}\n"
            f"**Context:** {context}\n**Correction:** {correction}\n**Apply when:** {apply_when}\n**Source:** {source}\n\n"
        )
        head, sep, tail = existing.partition("\n## ")
        p.write_text(head.rstrip("\n") + "\n\n" + block + (sep + tail if sep else ""), encoding="utf-8")

    # ── validation (markster-os hard gate) ──
    def validate(self) -> list[str]:
        errors: list[str] = []
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                mk.validate_company_context(self.ws.root)
        except SystemExit:
            errors.append(buf.getvalue().strip() or "company-context validation failed")
        return errors

    # ── onboarding ──
    def onboard(self, answers: dict[str, str]) -> None:
        cc = self.ws.company_context
        manifest = json.loads((cc / "manifest.json").read_text())
        manifest.update({
            "company_name": answers.get("company_name", manifest["company_name"]),
            "category": answers.get("category", manifest.get("category")),
            "primary_audience": answers.get("primary_audience", manifest.get("primary_audience")),
            "core_offer": answers.get("core_offer", manifest.get("core_offer")),
            "default_tone": answers.get("tone") or manifest.get("default_tone"),
        })
        (cc / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        for key, _prompt, target in QUESTIONS:
            if target and answers.get(key):
                file, heading = target
                p = cc / file
                p.write_text(replace_section(p.read_text(encoding="utf-8"), heading, answers[key]), encoding="utf-8")
        cfg = dict(self.config)
        cfg.update({
            "website": answers.get("website") or cfg.get("website"),
            "competitors": _csv(answers.get("competitors")) or cfg.get("competitors", []),
            "channels": _csv(answers.get("channels")) or cfg.get("channels", []),
            "sender": {
                "name": answers.get("sender_name") or (cfg.get("sender") or {}).get("name"),
                "email": answers.get("sender_email") or (cfg.get("sender") or {}).get("email"),
            },
            "booking_url": answers.get("booking_url") or cfg.get("booking_url"),
            "onboarded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        })
        self.ws.config.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        self.__dict__.update(BusinessContext.load(self.ws).__dict__)


# ── markdown helpers ──────────────────────────────────────────────────────────
def extract_section(text: str, heading: str) -> str:
    m = re.search(rf"(?m)^## {re.escape(heading)}\s*$\n(.*?)(?=^## |\Z)", text, flags=re.DOTALL)
    return m.group(1).strip() if m else ""


def replace_section(text: str, heading: str, body: str) -> str:
    """Replace the body under `## heading` (up to the next `## `), keeping heading order intact."""
    pattern = rf"(?ms)(^## {re.escape(heading)}\s*$\n)(.*?)(?=^## |\Z)"
    if not re.search(pattern, text):
        raise KeyError(f"heading {heading!r} not found")
    return re.sub(pattern, lambda m: m.group(1) + "\n" + body.strip() + "\n\n", text, count=1)


def _bullets(section: str) -> list[str]:
    items = [re.sub(r"^[-*]\s+", "", ln).strip() for ln in section.splitlines() if ln.strip().startswith(("-", "*"))]
    if not items and section:
        items = [s.strip() for s in re.split(r"[;\n]", section) if s.strip()]
    return items


def _short(text: str, max_words: int = 6) -> str:
    """Search-engine sized seed: the first clause of a pain point, at most a few words."""
    clause = re.split(r"[,;:.()]|\bwhich\b|\bthat\b|\band\b", text, maxsplit=1)[0]
    return " ".join(clause.split()[:max_words]).strip()


def _brand(domain_or_name: str) -> str:
    """'fathom.com' → 'fathom'; 'Weave' → 'Weave'."""
    host = domain_or_name.strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.")
    return host.split("/")[0].split(".")[0] if "." in host else domain_or_name.strip()


def _csv(raw: str | None) -> list[str]:
    return [s.strip() for s in (raw or "").split(",") if s.strip()]


def _is_placeholder(body: str) -> bool:
    return bool(re.match(r"^(Write|State|List|Describe|Name|Include|Define|Explain|Replace)\b", body.strip()))
