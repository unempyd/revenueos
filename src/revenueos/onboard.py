"""Connect once: a business gives its website and RevenueOS writes the questionnaire itself.

`derive_answers(url, llm)` reads the homepage (and the about/services/contact pages it links to),
pulls the facts a crawler can see (name, description, headings, phones, booking link, email, ad tags),
and fills the 14 onboarding answers. With a model, the answers are written in plain language and
every inference is labelled `[hypothesis]`; without one, the answers are the observed facts,
labelled `[derived from the website]`. The owner corrects them on the Business page at any time.
Nothing is invented: a field the page does not support stays blank.
"""
from __future__ import annotations

import html as html_mod
import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .context import QUESTIONS
from .llm import LLM
from .workers.seo import parse_signals

UA = "RevenueOS/0.2 (+https://unempyd.github.io/revenueos/)"
FOLLOW = ("about", "services", "service", "contact", "pricing", "prices", "menu", "team", "book", "booking")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html_mod.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _meta(html: str, name: str) -> str:
    """content="…" or content='…' — the value may contain the other quote (Adelaide's)."""
    tag = re.search(rf'<meta[^>]+(?:name|property)=["\']{re.escape(name)}["\'][^>]*>', html, re.I)
    if not tag:
        return ""
    m = re.search(r'content=(["\'])(.*?)\1', tag.group(0), re.I | re.S)
    return html_mod.unescape(m.group(2)).strip() if m else ""


def read_site(url: str, client: httpx.Client | None = None, max_pages: int = 5) -> dict[str, Any]:
    """The facts a stranger can see. Never raises for an unreachable page; `ok` says what happened."""
    url = url if url.startswith("http") else f"https://{url}"
    c = client or httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=15.0)
    try:
        r = c.get(url)
    except httpx.HTTPError as e:
        return {"ok": False, "error": type(e).__name__, "url": url}
    if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
        return {"ok": False, "error": f"http {r.status_code}", "url": url}
    home = r.text[:800_000]
    final = str(r.url)
    sig = parse_signals(home, final)
    title = _text(re.search(r"<title[^>]*>(.*?)</title>", home, re.I | re.S).group(1)) if re.search(r"<title", home, re.I) else ""
    site_name = _meta(home, "og:site_name")
    headings = [_text(h) for h in re.findall(r"<h[12][^>]*>(.*?)</h[12]>", home, re.I | re.S)]
    headings = [h for h in headings if 3 <= len(h) <= 120][:8]
    body = _text(re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", home))[:6000]
    links = re.findall(r'href=["\']([^"\'#]+)["\']', home, re.I)
    host = urlparse(final).netloc
    pages: dict[str, str] = {}
    for href in links:
        full = urljoin(final, href)
        if urlparse(full).netloc != host or len(pages) >= max_pages - 1:
            continue
        path = urlparse(full).path.lower()
        if any(k in path for k in FOLLOW) and full not in pages:
            try:
                r2 = c.get(full)
                if r2.status_code == 200 and "text/html" in r2.headers.get("content-type", ""):
                    pages[full] = _text(re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", r2.text[:400_000]))[:3000]
            except httpx.HTTPError:
                continue
    lang = (re.search(r'<html[^>]*\blang=["\']([a-zA-Z-]+)', home) or [None, ""])[1]
    social = sorted({n for n in ("instagram", "facebook", "tiktok", "linkedin", "youtube") if any(f"{n}.com/" in ln for ln in links)})
    return {
        "social": social,
        "ok": True, "url": final, "host": host, "title": title, "site_name": site_name, "description": _meta(home, "description"),
        "headings": headings, "body": body, "pages": pages, "lang": lang, "signals": sig,
        "phones": sig.get("phones") or [], "emails": sig.get("emails") or [], "booking_url": sig.get("booking_url"),
    }


def _company_name(facts: dict[str, Any]) -> str:
    if facts.get("site_name"):
        return facts["site_name"].strip()
    title = facts.get("title") or ""
    for sep in (" | ", " – ", " — ", " - ", ": "):
        if sep in title:
            parts = [p.strip() for p in title.split(sep) if p.strip()]
            parts.sort(key=len)
            return parts[0][:60]
    if title:
        return title[:60]
    return facts.get("host", "").removeprefix("www.").split(".")[0].title()


def heuristic_answers(facts: dict[str, Any]) -> dict[str, str]:
    tag = " [derived from the website]"
    name = _company_name(facts)
    desc = facts.get("description") or (facts.get("headings") or [""])[0]
    booking = facts.get("booking_url") or ""
    if booking and not booking.startswith("http"):
        booking = urljoin(facts["url"], booking)
    sig = facts.get("signals") or {}
    channels = ["website"]
    if sig.get("meta_pixel"):
        channels.append("meta ads")
    if sig.get("google_ads_tag"):
        channels.append("google ads")
    for n in ("instagram", "facebook", "tiktok", "youtube"):
        if n in (facts.get("social") or []):
            channels.append(n)
    return {
        "company_name": name,
        "website": facts["url"],
        "what_we_do": (desc[:240] + tag) if desc else "",
        "core_offer": ((facts.get("headings") or [""])[0][:160] + tag) if facts.get("headings") else "",
        "channels": ", ".join(channels),
        "sender_name": name,
        "sender_email": (facts.get("emails") or [""])[0],
        "booking_url": booking,
    }


PROMPT = """You fill a business's onboarding questionnaire from what its website says. Use only the facts below.
Write in plain English, one or two sentences per field, in the business's own terms. Every inference that the
site does not state outright ends with " [hypothesis]". Leave a field empty ("") rather than invent it.
Return JSON with exactly these keys: {keys}."""


def llm_answers(facts: dict[str, Any], llm: LLM) -> dict[str, str]:
    keys = [k for k, _, _ in QUESTIONS]
    system = PROMPT.format(keys=", ".join(keys))
    user = json.dumps({k: facts.get(k) for k in ("url", "title", "site_name", "description", "headings", "body", "pages", "phones", "emails", "booking_url", "lang")},
                      ensure_ascii=False)[:14000]
    raw = llm.ask(system, user, max_tokens=1800)
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(m.group(0)) if m else {}
    return {k: str(v).strip() for k, v in data.items() if k in keys and v}


def derive_answers(url: str, llm: LLM | None = None, client: httpx.Client | None = None) -> tuple[dict[str, str], dict[str, Any]]:
    """(answers, facts). Heuristic answers always; model answers layered on top when a model is present."""
    facts = read_site(url, client)
    if not facts.get("ok"):
        return {}, facts
    answers = heuristic_answers(facts)
    if llm is not None:
        try:
            answers.update({k: v for k, v in llm_answers(facts, llm).items() if v})
        except Exception as e:  # a model failure never blocks onboarding; the heuristics stand
            facts["llm_error"] = f"{type(e).__name__}: {e}"
    answers["website"] = facts["url"]
    return answers, facts
