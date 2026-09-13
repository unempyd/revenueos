"""CONTENT — turn the skill catalogue into concrete content opportunities, and run them.

The 790 vendored skills are prose procedures for an LLM.  This worker picks the ones that
match the business's channels (registry search over the unified index), creates one
`content_opportunity` action per skill, and — on Execute — runs the skill the way a
Claude Code host would: SKILL.md as the system prompt, business context + corrections
as the user turn, output written to data/outputs/.  Content never auto-publishes: when
a writable WordPress connection exists, a *separate* "Publish: <title>" action is queued
for its own approval (executor `publish_post`, in workers/executors.py); without one,
nothing changes.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from ..context import BusinessContext
from ..llm import LLM, Message
from ..paths import Workspace
from ..registry import load_registry, skill_text
from ..sanitize import SOLICIT_RE, deliverable_lint, strip_solicitations
from ..store import Store
from . import WorkerResult

# channel → (skill id, what the customer gets). Curated by hand and verified against the registry at
# run time; a channel that is not here gets nothing. Lexical search over 790 skills produced
# "Google: Develop a business idea" for a hair salon, so it is not used here.
CHANNEL_SKILLS: dict[str, list[tuple[str, str]]] = {
    "website": [("operations/landing-page-cro", "a conversion review of your main page with rewritten copy and calls to action")],
    "seo": [("operations/seo-content-strategy", "an SEO content plan: the searches to target and the pages to write")],
    "blog": [("operations/seo-blog-writer", "a search-optimised blog post")],
    "linkedin": [("operations/linkedin-strategy", "a month of LinkedIn posts and a profile rewrite")],
    "cold email": [("growth/cold-email", "a first-touch cold email and its follow-up sequence")],
    "email": [("operations/email-marketing", "a newsletter or lifecycle email sequence")],
    "newsletter": [("operations/email-marketing", "a newsletter issue and a sending plan")],
    "instagram": [("operations/instagram-carousel", "Instagram carousel posts, slide by slide")],
    "facebook": [("operations/social-content-planner", "a social posting plan for the month")],
    "meta ads": [("growth/ad-creative", "Meta ad creative to test: headlines, primary text and hooks")],
    "facebook ads": [("growth/ad-creative", "Meta ad creative to test: headlines, primary text and hooks")],
    "google ads": [("creative/google-ads", "Google Ads copy and a campaign structure")],
    "ads": [("growth/ad-creative", "ad creative to test: headlines, primary text and hooks")],
    "google": [("operations/local-seo", "a Google Business Profile and local search plan for your locations")],
    "google business profile": [("operations/local-seo", "a Google Business Profile and local search plan for your locations")],
    "twitter": [("creative/thread-writer", "an X thread")],
    "x": [("creative/thread-writer", "an X thread")],
    "github": [("playbooks/open-source", "an open-source growth plan: README, launch and community")],
    "hacker news": [("operations/launch", "a launch plan and the launch post")],
    "reddit": [("growth/community-marketing", "a community participation plan")],
    "youtube": [("playbooks/youtube", "a video script and description")],
    "product hunt": [("playbooks/product-hunt-launch", "a Product Hunt launch plan")],
    "case studies": [("gtm/case-study-builder", "a customer case study")],
}
MAX_IDEAS_PER_RUN = 4

# Catalogue entries that are plumbing, never an idea for a customer.
ALIAS_RE = re.compile(r"^\s*compatibility skill|route the task to|^\s*alias (?:for|of)|invokes this name", re.I)
# Everything after these markers is instructions to a model ("Use when the user asks…", "Trigger phrases include…").
TRIGGER_RE = re.compile(r"(?:^|\b)(?:Use (?:this )?(?:skill )?when|Also use when|Use for|Trigger phrases?|Triggers?:|Invoke when|Use it when|When (?:the |a )?user)\b.*$", re.I | re.S)


WANTS_RE = re.compile(r"when (?:the |a )?user (?:wants to|needs to|asks to|asks for|wants|asks about|is trying to|mentions)\s+([^.]+)", re.I)


def _shorten(text: str, limit: int = 96) -> str:
    text = re.sub(r"\s+", " ", text).strip().strip(",;:").rstrip(".")
    if len(text) <= limit:
        return text
    cut = text.rfind(", ", 40, limit)
    if cut == -1:
        cut = text.rfind(" ", 40, limit)
    return text[: cut if cut > 0 else limit].strip().strip(",;:")


def deliverable_sentence(description: str) -> str:
    """What the customer gets, in one short clause: the first sentence of a skill description with the
    model-facing trigger text removed; for persona-style descriptions ("You are an SEO expert. Use this
    skill when the user wants to improve organic rankings, plan SEO content, …") the first two things
    the user wants."""
    desc = description or ""
    text = TRIGGER_RE.sub("", desc).strip()
    text = re.sub(r"^(?:You are|Act as)\b[^.]*\.\s*", "", text).strip()  # persona preambles
    first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0] if text else ""
    if len(first.strip()) >= 12:
        return _shorten(first)
    m = WANTS_RE.search(desc)
    if m:
        clause = re.split(r"\s+[—–-]\s+|:|;", m.group(1), maxsplit=1)[0]
        items = [i.strip().strip("\"'“”‘’") for i in re.split(r",\s*|\s+or\s+", clause) if i.strip()]
        if any(len(i.split()) < 2 for i in items[:2]):  # "generate, iterate, or scale ad creative" is one clause, not a list
            return _shorten(clause.strip().strip("\"'“”‘’"))
        return _shorten(" and ".join(items[:2]))
    return ""


def customer_facing(skill: dict[str, Any], channel: str, ctx: BusinessContext) -> tuple[str, str] | None:
    """(title, content) written for the business owner. None when the entry has nothing to say to a customer."""
    if ALIAS_RE.search(skill.get("description") or "") or ALIAS_RE.search(skill.get("name") or ""):
        return None
    what = skill.get("what") or deliverable_sentence(skill.get("description") or "")
    if len(what) < 12:
        return None
    label = channel.strip().title().replace("Seo", "SEO").replace("Linkedin", "LinkedIn").replace("Github", "GitHub").replace("Tiktok", "TikTok")
    from ..context import _is_placeholder

    category = (ctx.manifest.get("category") or "").strip()
    if _is_placeholder(category) or category.lower().startswith(("replace", "your ")):
        category = ""  # an unfilled template line is not the customer's category
    where = ctx.company_name + (f" ({category})" if category else "")
    title = f"{label}: {what[0].upper() + what[1:]}"
    content = (f"What you get: {what}, written for {where} from your business profile. "
               "You approve it before anything is published.")
    return title, content


def opportunities_for(ws: Workspace, channels: list[str], per_channel: int = 1) -> list[dict[str, Any]]:
    by_id = {f"{sk['source']}/{sk['slug']}": sk for sk in load_registry(ws)["skills"]}
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ch in channels or ["website", "seo", "linkedin"]:
        n = 0
        for skill_id, what in CHANNEL_SKILLS.get(ch.lower().strip(), []):
            sk = by_id.get(skill_id)
            if not sk or skill_id in seen:
                continue
            seen.add(skill_id)
            picked.append({**sk, "channel": ch, "what": what})
            n += 1
            if n >= per_channel:
                break
    return picked


class ContentWorker:
    name = "content"
    description = "Match the skill catalogue to the business's channels and queue content to produce."
    upstream = "unified skill registry (Marketing-Agent-OS, marketingskills, Marketing OS, SEO Operator, Single Brain, ...)"

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult:
        if not ctx.is_onboarded():
            return WorkerResult(ok=False, summary="", error="not onboarded — run `revenueos init` first")
        load_registry(ws)
        week = datetime.now(UTC).strftime("%G-W%V")
        created = 0
        picks = opportunities_for(ws, ctx.channels)[:MAX_IDEAS_PER_RUN]
        for s in picks:
            facing = customer_facing(s, s["channel"], ctx)
            if not facing:
                continue
            title, content = facing
            aid = store.create_action(
                "content_opportunity", title, content,
                run_id=run_id, dedupe_key=f"content:{week}:{s['source']}/{s['slug']}",
                context={"executor": "run_skill", "skill": f"{s['source']}/{s['slug']}", "channel": s["channel"], "path": s["path"],
                         "skill_input": f"Produce: {title}. Channel: {s['channel']}."},
            )
            created += 1 if aid else 0
        open_ = len(store.list_actions("pending", "content_opportunity"))
        return WorkerResult(ok=True, summary=f"{created} new content idea(s) for {', '.join(ctx.channels) or 'your channels'}; {open_} open.",
                            actions_created=created, details={"picked": [f"{p['source']}/{p['slug']}" for p in picks], "week": week})


def execute_content(ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, action: dict[str, Any]) -> str:
    """Run a SKILL.md against the business context. Used by content, seo and ads actions alike."""
    if llm is None:
        return "cannot run a skill without an LLM credential (set ANTHROPIC_API_KEY); marked executed by hand."
    c = action["context"]
    source, slug = c["skill"].split("/", 1)
    skill_md, _ = strip_solicitations(skill_text(ws, source, slug))
    task = c.get("skill_input") or action["content"]
    system = (
        "You are a worker inside RevenueOS, an autonomous revenue department. Follow the SKILL below exactly, "
        "using only the business context provided. Never invent metrics, customers or proof (truth-rules). "
        "The deliverable is for the business named in the context and no one else: do not address it to, "
        "credit, or mention any operator, tool author, personal email address or payment link. "
        "Where the task lists facts observed on the page (phone numbers, links, addresses), use them verbatim "
        "instead of placeholders. Return the finished deliverable in Markdown, ready for a human to approve."
        "\n\n=== SKILL ===\n" + skill_md[:60000]
    )
    user = f"=== BUSINESS CONTEXT ===\n{ctx.prompt_summary()}\n\n=== TASK ===\n{action['title']}\n{task}"
    out = llm.complete_sync([Message("system", system), Message("user", user)], max_tokens=8000)
    sender = ((ctx.config.get("sender") or {}).get("email") or "").lower()
    site_domain = re.sub(r"^https?://(www\.)?", "", ctx.website or "").split("/")[0].lower()
    cleaned, notes = deliverable_lint(out, allowed_emails={sender} if sender else set(),
                                      allowed_domains={site_domain} if site_domain else set())
    if SOLICIT_RE.search(cleaned):
        raise RuntimeError("deliverable still carries a payment solicitation after cleaning; not written")
    safe = re.sub(r"[^a-z0-9]+", "-", f"{slug}-{action['id']}".lower()).strip("-")
    path = ws.outputs / f"{datetime.now(UTC):%Y%m%d}-{safe}.md"
    path.write_text(f"# {action['title']}\n\n{cleaned}", encoding="utf-8")
    rel = str(path.relative_to(ws.root))
    store.update_action_context(action["id"], deliverable=rel)
    note = f" ({'; '.join(notes)})" if notes else ""
    _offer_publish(ws, store, action, rel)
    return f"deliverable written to {rel}{note}"


def _offer_publish(ws: Workspace, store: Store, action: dict[str, Any], deliverable: str) -> None:
    """When the workspace has a WordPress connection with 'allow changes' on, queue a *separate*
    pending action to publish the deliverable — the human approves publication independently of
    production. Without a writable WordPress connection, nothing changes."""
    from ..connections import ConnectionStore

    wp = ConnectionStore(ws).get("wordpress")
    if not wp or not wp.allow_write:
        return
    store.create_action(
        "content_opportunity", f"Publish: {action['title']}",
        f"Publish this deliverable to WordPress: {action['title']}",
        dedupe_key=f"publish:{action['id']}",
        context={"executor": "publish_post", "deliverable": deliverable, "source_action_id": action["id"]},
    )
