"""OUTREACH — draft → human approval → send → learn (ai-sales-agent's pipeline).

Drafting is deterministic by default, exactly as upstream argues (LLM drift is the enemy
of predictable cold outreach): a Recipe of Hooks (vendored data model) is filled from
company-context, the subject variant is picked by hash(lead_id) — a free A/B split — and
the body is substitution only.  With an LLM present, the opener line is personalised
from the lead's `reason` column and the messaging canon.

Every draft is a `follow_up` action.  Approving it flips the vendored approval state
machine (pending → approved); executing it renders with the vendored `render_cold_text`
(CASL footer + 'reply stop to unsubscribe') and sends over SMTP with the daily cap and
suppression list enforced, then records the immutable `email_sends` row.
"""
from __future__ import annotations

import hashlib
import os
import smtplib
from email.message import EmailMessage
from typing import Any

from ..context import BusinessContext
from ..llm import LLM
from ..paths import Workspace
from ..store import Store
from ..vendor.sales_agent.models import EmailDraft, Lead
from ..vendor.sales_agent.recipes import Hook, Recipe
from ..vendor.sales_agent.render import render_cold_text
from . import WorkerResult

MODEL_TEMPLATE = "template-v1"


def build_recipe(ctx: BusinessContext) -> Recipe:
    """One Recipe with 2–3 Hooks derived from the canon (offer, differentiators, pain points)."""
    company = ctx.company_name
    offer = ctx.section("offer.md", "Core Offer") or ctx.manifest.get("core_offer", "")
    outcome = ctx.section("offer.md", "Outcome Statement")
    diff = ctx.section("messaging.md", "Differentiators")
    one_liner = ctx.section("messaging.md", "One-Liner") or ctx.section("identity.md", "One-Sentence Definition")
    pains = ctx.section("audience.md", "Pain Points")
    booking = ctx.config.get("booking_url") or ""
    cta = f"Worth a 15-minute look? {booking}" if booking else "Worth a 15-minute look this week?"
    hooks = [
        Hook(
            name="problem_first",
            subjects=("quick question about {shop_name}", "{shop_name} + " + company),
            opener="Noticed {shop_name} — {reason}.",
            body=f"{pains.splitlines()[0] if pains else 'Most teams like yours hit the same wall.'}\n\n{one_liner}\n\n{cta}",
        ),
        Hook(
            name="offer_first",
            subjects=("an idea for {shop_name}", "{shop_name}: " + (outcome.splitlines()[0][:60] if outcome else "a shortcut")),
            opener="Hi — writing because {reason}.",
            body=f"{offer}\n\n{diff.splitlines()[0] if diff else ''}\n\n{cta}".strip(),
        ),
    ]
    return Recipe(key="revenueos-default", hooks=tuple(hooks))


def pick(lead_id: str, n: int, salt: str = "") -> int:
    """Deterministic A/B split, same idea as upstream's `_hash_pick` (hash(lead_id) % n)."""
    digest = hashlib.sha256(f"{lead_id}:{salt}".encode()).hexdigest()
    return int(digest, 16) % max(1, n)


def draft_for(lead: dict[str, Any], recipe: Recipe, llm: LLM | None, ctx: BusinessContext) -> tuple[str, str, str, str]:
    hook = recipe.hooks[pick(lead["id"], len(recipe.hooks), "hook")]
    subject_tpl = hook.subjects[pick(lead["id"], len(hook.subjects), "subject")]
    shop = lead.get("business_name") or "your team"
    reason = (lead.get("reason") or "you look like a fit for what we do").rstrip(".")
    subject = subject_tpl.format(shop_name=shop)
    opener = hook.opener.format(shop_name=shop, reason=reason)
    if llm is not None:
        try:
            opener = llm.ask(
                "You write the first sentence of a cold email. Plain, specific, no flattery, no em dashes, under 25 words. "
                "Output the sentence only.\n\n" + ctx.prompt_summary(2500),
                f"Prospect: {shop}. Title: {lead.get('title') or 'unknown'}. Why they qualify: {reason}. Website: {lead.get('website_url') or 'n/a'}.",
                max_tokens=200,
            ).strip().splitlines()[0]
        except Exception:
            pass
    body = f"{opener}\n\n{hook.body.format(shop_name=shop, reason=reason)}"
    return hook.name, subject_tpl, subject, body


class OutreachWorker:
    name = "outreach"
    description = "Draft first-touch emails for scored prospects; nothing sends without approval."
    upstream = "Meshpilot-AGI/ai-sales-agent recipes + approval state machine + renderer"

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult:
        if not ctx.is_onboarded():
            return WorkerResult(ok=False, summary="", error="not onboarded — run `revenueos init` first")
        recipe = build_recipe(ctx)
        limit = int((ctx.config.get("outreach") or {}).get("drafts_per_run", 10))
        created, skipped = 0, 0
        candidates = [l for l in store.list_leads() if l["status"] in ("new", "enriched", "scored")]
        for lead in candidates[:limit]:
            email = (lead.get("contact_email") or "").strip()
            if not email or store.is_unsubscribed(email):
                skipped += 1
                continue
            hook_name, variant, subject, body = draft_for(lead, recipe, llm, ctx)
            draft_id = store.create_draft(lead["id"], f"{recipe.key}/{hook_name}", variant, subject, body,
                                          MODEL_TEMPLATE if llm is None else f"{MODEL_TEMPLATE}+{llm.model}")
            aid = store.create_action(
                "follow_up", f"Send to {email} — {subject}", body,
                run_id=run_id, dedupe_key=f"draft:{draft_id}",
                context={"executor": "send_email", "draft_id": draft_id, "lead_id": lead["id"], "to": email, "hook": hook_name},
            )
            created += 1 if aid else 0
        return WorkerResult(ok=True, summary=f"{created} outreach draft(s) ready for approval; {skipped} lead(s) skipped (no email or unsubscribed).",
                            actions_created=created, details={"candidates": len(candidates), "skipped": skipped})


# ── execution (only after approval) ───────────────────────────────────────────
def _as_models(store: Store, draft_id: str) -> tuple[EmailDraft, Lead]:
    d = store.get_draft(draft_id)
    if not d:
        raise KeyError(f"draft {draft_id} not found")
    lead = store.get_lead(d["lead_id"]) or {}
    body = d["edited_body"] or d["body"]
    subject = d["edited_subject"] or d["subject"]
    draft = EmailDraft.model_construct(**{**d, "body": body, "subject": subject})
    return draft, Lead.model_construct(**lead)


def send_smtp(cfg: dict[str, Any], to_email: str, subject: str, text: str) -> str:
    smtp = cfg.get("smtp") or {}
    host, port = smtp.get("host") or os.environ.get("SMTP_HOST"), int(smtp.get("port") or os.environ.get("SMTP_PORT") or 587)
    user = smtp.get("user") or os.environ.get("SMTP_USER") or (cfg.get("sender") or {}).get("email")
    password = os.environ.get("SMTP_PASSWORD")
    sender = (cfg.get("sender") or {}).get("email")
    if not (host and user and password and sender):
        raise RuntimeError("SMTP not configured: set smtp.host/user in revenueos.yaml and SMTP_PASSWORD in the environment")
    msg = EmailMessage()
    msg["From"] = f"{(cfg.get('sender') or {}).get('name') or sender} <{sender}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=stop>"
    msg.set_content(text)
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)
    return msg["Message-ID"] or ""


def execute_send(ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, action: dict[str, Any]) -> str:
    c = action["context"]
    draft, lead = _as_models(store, c["draft_id"])
    to_email = c["to"]
    cap = int((ctx.config.get("outreach") or {}).get("daily_cap", 25))
    if store.is_unsubscribed(to_email):
        return f"not sent: {to_email} is on the suppression list"
    if store.daily_send_count() >= cap:
        return f"not sent: daily cap of {cap} reached; try again tomorrow"
    d = store.get_draft(c["draft_id"]) or {}
    if d.get("approval_state") not in ("approved", "edited"):
        store.set_draft_approval(c["draft_id"], "approved")
    text = render_cold_text(draft, lead)
    dry = os.environ.get("REVENUEOS_DRY_RUN") == "1" or (ctx.config.get("outreach") or {}).get("dry_run", False)
    if dry:
        (ws.outputs / f"email-{c['draft_id'][:8]}.txt").write_text(f"To: {to_email}\nSubject: {draft.subject}\n\n{text}", encoding="utf-8")
        provider, mid = "dry-run", None
    else:
        provider, mid = "smtp", send_smtp(ctx.config, to_email, draft.subject, text)
    send_id = store.record_send(c["draft_id"], c["lead_id"], to_email, draft.subject, text, provider, mid)
    return f"sent via {provider} to {to_email} (send {send_id[:8]})"
