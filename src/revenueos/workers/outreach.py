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
from ..store import Store, now
from ..vendor.sales_agent.models import EmailDraft, Lead
from ..vendor.sales_agent.recipes import Hook, Recipe
from ..vendor.sales_agent.render import render_cold_text
from . import WorkerResult

# Bound into this module's namespace (not just imported for local use) so a test can
# monkeypatch `revenueos.workers.outreach.homepage_signals` independently of seo's own copy:
# a draft's factual claim ("phone shown but not tappable", "no LocalBusiness schema") is
# re-checked against the live homepage right before drafting and right before sending —
# a claim observed at discover time can go stale by the time a human approves it.
from .seo import homepage_signals  # noqa: F401

MODEL_TEMPLATE = "template-v2"

# Text that must never reach a recipient: unrendered placeholders, canon scaffolding, scrape vocabulary.
FORBIDDEN = (
    "{", "}", "[derived", "[hypothesis", "[from the website", "one sentence.", "two to three sentences", "write approved",
    "state the ", "list phrases", "use bullets only", "public repos", "followers", "forked ", "stargazer", "github profile",
    "lorem", "todo", "tbd", "xxxx", "<insert", "[insert",
)
SCAFFOLD_LINE = ("- what ", "- who ", "- how ", "- where ", "- when ", "- optional", "- specific events", "- signs they", "- common concerns")


class DraftRejected(ValueError):
    """The draft is not fit to send; the reason names the offending text."""


def lint_draft(subject: str, body: str) -> None:
    text = f"{subject}\n{body}"
    low = text.lower()
    for bad in FORBIDDEN:
        if bad in low:
            raise DraftRejected(f"forbidden text {bad!r}")
    for line in body.splitlines():
        if line.strip().lower().startswith(SCAFFOLD_LINE):
            raise DraftRejected(f"canon scaffolding line {line.strip()[:40]!r}")
    if len(subject.strip()) < 8 or len(subject) > 90:
        raise DraftRejected("subject length")
    if len(body.split()) < 40 or len(body.split()) > 220:
        raise DraftRejected(f"body length {len(body.split())} words")
    if not any(g in body[:40] for g in ("Hi ", "Hello ", "Dear ")):
        raise DraftRejected("no greeting")


def _clean(text: str) -> str:
    """Strip our own provenance tags before anything is said to a customer."""
    import re

    return re.sub(r"\s*\[(?:derived|hypothesis|from the website)[^\]]*\]", "", text or "").strip()


def observation(lead: dict[str, Any]) -> tuple[str, str] | None:
    """(kind, sentence): one specific, observed thing about THEIR site, from the evidence the lead source recorded.
    Ad tags alone are context, not an insight; without a defect to point at there is nothing to say."""
    reason = _clean(lead.get("reason") or "")
    if "OpenStreetMap" not in reason and "on the site" not in reason:
        # a human-written qualification note ("3-chair clinic, posts about no-shows") is a specific observation too
        if reason and not any(bad in reason.lower() for bad in ("forked", "followers", "public repos", "profile lists", "starred")):
            return "note", f"One thing that stood out: {reason.rstrip('.')}."
        return None
    parts = [p.strip() for p in reason.split(";")]
    site = (lead.get("website_url") or "").removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/")
    tags = next((p for p in parts if p.startswith("ad/analytics tags on the site: ")), "")
    spend = []
    if "Meta Pixel" in tags:
        spend.append("Meta ads (there is a Meta Pixel on it)")
    if "Google Ads tag" in tags:
        spend.append("Google Ads (there is a Google Ads conversion tag on it)")
    lead_in = f"{site} is set up for {' and '.join(spend)}, so you are paying to bring people to the page, but " if spend else f"On {site}, "
    if any(p == "phone shown but not tappable" for p in parts):
        return "phone", lead_in + "the phone number is plain text: a visitor on a phone cannot tap it to call you."
    if any(p == "no LocalBusiness schema" for p in parts):
        return "schema", lead_in + "the page carries no LocalBusiness markup, so Google is not told your business type, address and hours for local results."
    return None


# Kinds `phone` and `schema` assert a fact about the live homepage; that fact is checked
# again — once before drafting, once again right before send — because a site can change
# (or the fetch that produced the discover-time `reason` can simply have been wrong) in
# the time between discovery and a human approving the draft. `note` is a human-written
# qualification note (e.g. "3-chair clinic, posts about no-shows") and is not a claim about
# the live page, so it is never re-checked.
CLAIM_TEXT = {
    "phone": "the phone number is shown as plain text, not a tappable tel: link",
    "schema": "the page carries no LocalBusiness schema markup",
}


def _claim_holds(kind: str, signals: dict[str, Any]) -> bool:
    """Does today's homepage_signals() still support the claim named by `kind`?"""
    if kind == "phone":
        return bool(signals.get("phones")) and not signals.get("tel_link")
    if kind == "schema":
        return not signals.get("local_schema")
    return True


def _observed_now(kind: str, signals: dict[str, Any]) -> str:
    """What today's read actually shows, for the withdrawal message — only called when
    `_claim_holds` is False, i.e. the draft's claim no longer matches the live page."""
    if kind == "phone":
        return "no phone number at all on the page" if not signals.get("phones") else "the phone number is now a tappable tel: link"
    if kind == "schema":
        return "the page now carries LocalBusiness schema"
    return "the page no longer matches the draft's claim"


def build_recipe(ctx: BusinessContext) -> Recipe:
    """One hook, written for the owner of a small business. The opener is filled per lead from what was
    observed on their site; nothing in the body comes from an unfilled canon section."""
    company = ctx.company_name
    booking = ctx.config.get("booking_url") or ""
    ask = f"Worth 15 minutes this week? {booking}".strip() if booking else "If that is worth 15 minutes this week, reply and the full list comes back the same day."
    signer = (ctx.config.get("sender") or {}).get("signature")
    intro = f"I run {company}," if signer else f"{company} is"
    body = (
        f"{intro} a small tool that reads a business's website and ads, lists the specific things costing it "
        "customers, and fixes the ones you approve. Every fix is re-checked afterwards so you can see whether it changed anything.\n\n"
        f"For {{shop_name}} the full list is ready. We are in early access: we set it up and run it, you approve each "
        "change, and you pay nothing until the first measured result.\n\n"
        f"{ask}"
    )
    hooks = [Hook(
        name="observed_first",
        subjects=("{shop_name}: one thing costing you calls", "{shop_name}: what Google can't read on your site", "quick note about {shop_name}'s website"),
        opener="Hi {first_name},\n\nI had a look at {shop_name}'s website this week. {observation}",
        body=body,
    )]
    return Recipe(key="revenueos-observed", hooks=tuple(hooks))


def pick(lead_id: str, n: int, salt: str = "") -> int:
    """Deterministic A/B split, same idea as upstream's `_hash_pick` (hash(lead_id) % n)."""
    digest = hashlib.sha256(f"{lead_id}:{salt}".encode()).hexdigest()
    return int(digest, 16) % max(1, n)


def signature(ctx: BusinessContext) -> str:
    """A person's name when the owner set sender.signature, otherwise the company; the site on its own line."""
    sender = ctx.config.get("sender") or {}
    name = (sender.get("signature") or "").strip()
    site = ctx.website or ""
    lines = [name, ctx.company_name] if name and name != ctx.company_name else [ctx.company_name]
    if site:
        lines.append(site)
    return "\n".join(lines)


def draft_for(lead: dict[str, Any], recipe: Recipe, llm: LLM | None, ctx: BusinessContext) -> tuple[str, str, str, str, str]:
    """Deterministic: no LLM in the body. Raises DraftRejected when there is nothing observed to say."""
    hook = recipe.hooks[pick(lead["id"], len(recipe.hooks), "hook")]
    shop = _clean(lead.get("business_name") or "")
    if not shop:
        raise DraftRejected("no business name")
    found = observation(lead)
    if not found:
        raise DraftRejected("nothing observed about this business's site; refusing a generic email")
    kind, obs = found
    subject_tpl = {"phone": hook.subjects[0], "schema": hook.subjects[1]}.get(kind, hook.subjects[2])
    first = (lead.get("first_name") or "").strip()
    greeting_name = first or f"{shop} team"
    subject = subject_tpl.format(shop_name=shop)
    opener = hook.opener.format(shop_name=shop, first_name=greeting_name, observation=obs)
    body = f"{opener}\n\n{hook.body.format(shop_name=shop)}\n\n{signature(ctx)}"
    lint_draft(subject, body)
    return hook.name, subject_tpl, subject, body, kind


class OutreachWorker:
    name = "outreach"
    description = "Draft first-touch emails for scored prospects; nothing sends without approval."
    upstream = "Meshpilot-AGI/ai-sales-agent recipes + approval state machine + renderer"

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult:
        if not ctx.is_onboarded():
            return WorkerResult(ok=False, summary="", error="not onboarded — run `revenueos init` first")
        recipe = build_recipe(ctx)
        limit = int((ctx.config.get("outreach") or {}).get("drafts_per_run", 10))
        created, skipped, stale = 0, 0, 0
        rejected: list[str] = []
        # only leads the gate qualified ('scored'); 'new' means found-but-not-qualified
        candidates = [l for l in store.list_leads() if l["status"] == "scored" and (l.get("contact_email") or "").strip()]
        for lead in candidates[:limit]:
            email = (lead.get("contact_email") or "").strip()
            if not email or store.is_unsubscribed(email):
                skipped += 1
                continue
            found = observation(lead)
            site = (lead.get("website_url") or "").strip()
            if found and found[0] in ("phone", "schema"):
                # re-check the claim against the live homepage before drafting: the discover-time
                # `reason` can already be stale, or simply wrong, by the time this worker runs.
                signals = homepage_signals(site) if site else {"ok": False}
                if not signals.get("ok") or not _claim_holds(found[0], signals):
                    stale += 1
                    continue
            try:
                hook_name, variant, subject, body, kind = draft_for(lead, recipe, llm, ctx)
            except DraftRejected as e:
                rejected.append(f"{lead.get('business_name')}: {e}")
                continue
            draft_id = store.create_draft(lead["id"], f"{recipe.key}/{hook_name}", variant, subject, body, MODEL_TEMPLATE)
            context: dict[str, Any] = {"executor": "send_email", "draft_id": draft_id, "lead_id": lead["id"], "to": email, "hook": hook_name, "kind": kind}
            if kind in ("phone", "schema"):
                context.update({"site": site, "claim": CLAIM_TEXT.get(kind, ""), "claim_verified_at": now()})
            aid = store.create_action(
                "follow_up", f"Send to {email} — {subject}", body,
                run_id=run_id, dedupe_key=f"draft:{draft_id}",
                context=context,
            )
            created += 1 if aid else 0
        return WorkerResult(ok=True, summary=f"{created} email draft(s) waiting for your approval; {skipped} lead(s) skipped (no email or unsubscribed); "
                                              f"{len(rejected)} draft(s) refused by the lint; {stale} skipped (claim no longer observed).",
                            actions_created=created, details={"candidates": len(candidates), "skipped": skipped, "rejected": rejected, "stale": stale})


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


# Dash constructions in outbound copy read as machine-written, and this is the third time one has
# reached a real message after being asked for. It is a send-time refusal now rather than a habit
# to remember. Hyphenated words are fine: "tap-to-call" is a word, " - " is a dash.
_DASHES = {"\u2014": "em dash", "\u2013": "en dash", "\u2012": "figure dash", "\u2212": "minus sign"}


def dash_in(text: str) -> str | None:
    """The name of the first dash construction in `text`, or None."""
    for ch, name in _DASHES.items():
        if ch in text:
            return name
    if " - " in text:
        return "spaced hyphen"
    return None


def send_smtp(cfg: dict[str, Any], to_email: str, subject: str, text: str,
              html: str | None = None) -> str:
    """Send one message. With `html` it goes as multipart/alternative, text part first.

    The guard runs on the subject and the text part only. The HTML part is full of CSS, and
    `-apple-system` and `letter-spacing:-.28px` are not dashes in prose. Since the text part
    carries the same sentences as the HTML, checking it checks the copy.
    """
    found = dash_in(subject) or dash_in(text)
    if found:
        raise RuntimeError(
            f"not sending: the copy contains a {found}, which reads as machine-written. "
            "Rewrite the sentence rather than swapping the character.")
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
    # Apple Mail renders a message carrying this header as "This message is from a mailing list"
    # with an Unsubscribe banner above the first line. On genuine one-to-one outreach that is a
    # misrepresentation and it undoes the personal framing the copy is built on. The header is a
    # bulk-sender convention, so it is opt-in: a sender doing real volume wants it, and sets
    # outreach.list_unsubscribe true. The opt-out itself lives in the body either way, which is
    # what the Australian Spam Act actually requires.
    if ((cfg.get("outreach") or {}).get("list_unsubscribe")):
        msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=stop>"
    msg.set_content(text)
    if html:
        # Text first, HTML second. A message whose text part is a stub telling the reader to open
        # it in a browser is the shape of one with something to hide, and filters score it that way.
        msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)
    return msg["Message-ID"] or ""


def execute_plain_send(ws: Workspace, store: Store, ctx: BusinessContext, action: dict[str, Any]) -> str:
    """An email RevenueOS wrote itself rather than a lead draft (the Pro offer, a licence delivery):
    the body IS the action's content, there is no lead, no recipe and no send row to attribute it to.
    Same refusals and the same dry-run behaviour as a cold email."""
    c = action["context"]
    to_email = (c.get("to") or "").strip()
    if not to_email:
        return "not sent: no recipient address on this action"
    if store.is_unsubscribed(to_email):
        return f"not sent: {to_email} is on the suppression list"
    subject = c.get("subject") or action["title"]
    text = action.get("content") or ""
    html = c.get("html") or None
    dry = os.environ.get("REVENUEOS_DRY_RUN") == "1" or (ctx.config.get("outreach") or {}).get("dry_run", False)
    if dry:
        path = ws.outputs / f"email-action-{action['id']}.txt"
        path.write_text(f"To: {to_email}\nSubject: {subject}\n\n{text}", encoding="utf-8")
        wrote = str(path)
        if html:
            # The HTML is what the recipient actually sees, so a dry run that writes only the
            # text part cannot be reviewed. Write both, and name both.
            page = ws.outputs / f"email-action-{action['id']}.html"
            page.write_text(html, encoding="utf-8")
            wrote += f" and {page}"
        return f"written to {wrote} (dry run) — nothing was sent"
    send_smtp(ctx.config, to_email, subject, text, html=html)
    return f"sent via smtp to {to_email}"


def execute_send(ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, action: dict[str, Any]) -> str:
    c = action["context"]
    if not c.get("draft_id"):
        return execute_plain_send(ws, store, ctx, action)
    draft, lead = _as_models(store, c["draft_id"])
    to_email = c["to"]
    cap = int((ctx.config.get("outreach") or {}).get("daily_cap", 25))
    if store.is_unsubscribed(to_email):
        return f"not sent: {to_email} is on the suppression list"
    if store.daily_send_count() >= cap:
        return f"not sent: daily cap of {cap} reached; try again tomorrow"
    kind = c.get("kind")
    if kind in ("phone", "schema"):
        # The draft's one claim about the live site is re-checked right before it can send:
        # a claim recorded at discover time (or even at draft time) can already be stale.
        site = c.get("site") or (lead.website_url or "")
        signals = homepage_signals(site) if site else {"ok": False}
        if not signals.get("ok"):
            return f"not sent: could not re-check {site} today (fetch failed); try again later"
        if not _claim_holds(kind, signals):
            claim = c.get("claim") or CLAIM_TEXT.get(kind, "the claim")
            observed = _observed_now(kind, signals)
            # email_drafts.approval_state has no 'withdrawn' value in its CHECK constraint (vendored
            # schema: pending/approved/rejected/edited/superseded); 'rejected' is the existing state
            # for "this draft is no longer valid" (the qualification gate uses it the same way when
            # a lead is disqualified after a draft already exists) — the word "withdrawn" below is
            # only the human-facing message text.
            store.set_draft_approval(c["draft_id"], "rejected", by="freshness-check")
            store.set_action_status(action["id"], "ignored")
            store.update_action_context(action["id"], claim_recheck_failed_at=now(), claim_recheck_observed=observed)
            return f"not sent: the draft says {claim} but today's read of {site} shows {observed}; draft withdrawn"
        store.update_action_context(action["id"], claim_verified_at=now(), claim=c.get("claim") or CLAIM_TEXT.get(kind, ""))
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
