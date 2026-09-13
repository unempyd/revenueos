"""SEO — monitoring → diagnosis → prioritisation.

Three real checks, no vendor keys:
  1. Site crawl (vendored pulse-cmo crawl_website): missing/duplicate titles and meta
     descriptions, thin pages, no sitemap.
  2. Authority Mark (vendored marketingskills/seo authority_mark.py): the business domain
     vs. its competitors on Ahrefs' free public domain-rating endpoint.
  3. Local project probe (vendored seo-growth-loop probe_project.py) when the customer's
     website source lives on disk (`seo.site_repo` in revenueos.yaml): finds sitemap/robots,
     content and template surfaces the SEO skills can act on.
Each finding becomes a `seo_opportunity` action pointing at the SEO skill that fixes it.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..context import BusinessContext
from ..llm import LLM
from ..paths import Workspace
from ..store import Store
from ..vendor.pulse.crawl import crawl_website
from . import WorkerResult


def _crawl(url: str, max_pages: int = 10):
    """pulse decorates crawl_website with @tool; call the underlying coroutine (tests may monkeypatch a plain function)."""
    fn = getattr(crawl_website, "fn", crawl_website)
    return fn(url, max_pages=max_pages)

VENDOR_SEO = Path(__file__).resolve().parents[1] / "vendor" / "seo"

SKILL_FOR = {
    "missing_title": ("seo", "title-meta-rewriter"),
    "missing_description": ("seo", "title-meta-rewriter"),
    "duplicate_title": ("seo", "cannibalization-check"),
    "thin_page": ("seo", "content-refresh-brief"),
    "no_sitemap": ("seo", "technical-seo-triage"),
    "authority_gap": ("seo", "authority-mark-keyword-difficulty"),
    "indexing_surface": ("seo", "technical-seo-triage"),
    # homepage signals (local businesses): each is re-checked by measure.py on the live page
    "phone_not_tappable": ("operations", "landing-page-cro"),
    "no_local_schema": ("operations", "local-seo"),
    "no_canonical": ("seo", "technical-seo-triage"),
}

SIGNAL_PATTERNS = {
    "tel_link": r"href=[\"']tel:",
    "mailto_link": r"href=[\"']mailto:",
    "booking_link": r"href=[\"'][^\"']*(?:book|appointment|reserve|schedule)[^\"']*[\"']",
    "phone_text": r"(?:\+\d{1,3}[ \-]?(?:\(\d{1,4}\)[ \-]?)?\d(?:[ \-]?\d){6,11}|\(?0\d{1,3}\)?(?:[ \-]?\d){7,9})",
    "meta_pixel": r"connect\.facebook\.net/[a-z_]+/fbevents\.js|fbq\(\s*['\"]init",
    "google_ads_tag": r"AW-\d{6,}|googleadservices\.com/pagead/conversion",
    "ga4": r"\bG-[A-Z0-9]{6,}\b",
    "gtm": r"GTM-[A-Z0-9]{4,}",
    "local_schema": r"\"@type\"\s*:\s*\"(?:LocalBusiness|HairSalon|BeautySalon|Dentist|Restaurant|Store|MedicalBusiness|LegalService|Plumber|Electrician|AutoRepair|RealEstateAgent|ProfessionalService|HomeAndConstructionBusiness|FinancialService|HealthAndBeautyBusiness)\"",
    "canonical": r"<link[^>]+rel=[\"']canonical[\"']",
}


def homepage_signals(site: str, timeout: float = 15.0) -> dict[str, Any]:
    """Boolean signals read from the live homepage HTML. `ok: False` when the page cannot be fetched;
    nothing is inferred then."""

    import httpx

    url = site if site.startswith("http") else f"https://{site}"
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; RevenueOS/0.1; +https://unempyd.github.io/revenueos/)"})
    except httpx.HTTPError as e:
        return {"ok": False, "error": type(e).__name__}
    if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
        return {"ok": False, "error": f"http {r.status_code}"}
    return parse_signals(r.text[:800_000], str(r.url))


def parse_signals(html: str, url: str) -> dict[str, Any]:
    """Boolean signals plus the facts a deliverable needs verbatim: phone numbers as shown, the booking link,
    email addresses on the page."""
    import html as html_mod
    import re

    out: dict[str, Any] = {"ok": True, "url": url}
    for name, pattern in SIGNAL_PATTERNS.items():
        out[name] = bool(re.search(pattern, html, re.I))
    visible = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)  # never read phone digits out of a pixel id
    text = html_mod.unescape(re.sub(r"<[^>]+>", " ", visible))
    phones = []
    for m in re.finditer(r"(?<!\d)" + SIGNAL_PATTERNS["phone_text"] + r"(?!\d)", text):
        ph = re.sub(r"\s+", " ", m.group(0)).strip()
        if sum(ch.isdigit() for ch in ph) >= 8 and ph not in phones:
            phones.append(ph)
    out["phones"] = phones[:4]
    m = re.search(SIGNAL_PATTERNS["booking_link"], html, re.I)
    out["booking_url"] = re.search(r"href=[\"']([^\"']+)", m.group(0), re.I).group(1) if m else None
    out["emails"] = sorted({e.lower() for e in re.findall(r"mailto:([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", html, re.I)})[:4]
    return out


def signal_findings(site: str, signals: dict[str, Any]) -> list[dict[str, Any]]:
    if not signals.get("ok"):
        return []
    url = signals.get("url") or site
    out = []
    facts = []
    if signals.get("phones"):
        facts.append("phone numbers shown on the page: " + ", ".join(signals["phones"]))
    if signals.get("booking_url"):
        facts.append(f"booking link: {signals['booking_url']}")
    if signals.get("emails"):
        facts.append("email on the page: " + ", ".join(signals["emails"]))
    observed = (" Observed: " + "; ".join(facts) + ".") if facts else ""
    if signals.get("phone_text") and not signals.get("tel_link"):
        out.append({"kind": "phone_not_tappable", "url": url,
                    "why": "The homepage shows a phone number as plain text with no tel: link, so visitors on a phone cannot tap to call." + observed,
                    "before": {"tel_link": False, "phone_text": True, "phones": signals.get("phones", [])}})
    if not signals.get("local_schema"):
        out.append({"kind": "no_local_schema", "url": url,
                    "why": "No LocalBusiness (or a subtype such as HairSalon) schema.org markup on the homepage; Google cannot read the business type, address and hours from the page." + observed,
                    "before": {"local_schema": False}})
    if not signals.get("canonical"):
        out.append({"kind": "no_canonical", "url": url, "why": "The homepage declares no canonical URL.", "before": {"canonical": False}})
    return out


def sitemap_under_path(site: str) -> bool:
    """Sites hosted under a path prefix (GitHub Pages project sites, /site/ mounts) keep their
    sitemap under that prefix, not at the host root the crawler probes."""
    import httpx

    base = site if site.startswith("http") else f"https://{site}"
    try:
        r = httpx.get(base.rstrip("/") + "/sitemap.xml", timeout=15.0, follow_redirects=True)
        return r.status_code == 200 and b"<urlset" in r.content[:2000] or b"<sitemapindex" in r.content[:2000]
    except httpx.HTTPError:
        return False


def _domain(url: str) -> str:
    host = urlparse(url if url.startswith("http") else f"https://{url}").netloc
    return host.removeprefix("www.")


NON_HTML_SUFFIXES = (".xml", ".json", ".txt", ".pdf", ".rss", ".atom", ".csv", ".gz")


def is_html_page(url: str) -> bool:
    """A crawled URL that is not a real HTML page (sitemaps, feeds, robots.txt) must never
    be scored for title/description/H1/thin-content: those fields are absent by design and
    counting them manufactures defects that do not exist."""
    path = urlparse(url or "").path.lower()
    if path.endswith(NON_HTML_SUFFIXES):
        return False
    return "sitemap" not in path and not path.endswith("/robots.txt")


def canonical_url(url: str) -> str:
    """One key per page: scheme/host lowercased, fragment and query dropped, '/index.html' and a trailing
    slash removed. 'https://x.au' and 'https://x.au/' are the same page, not a duplicate title."""
    p = urlparse((url or "").strip())
    path = p.path or "/"
    for idx in ("/index.html", "/index.htm", "/index.php"):
        if path.lower().endswith(idx):
            path = path[: -len(idx)] + "/"
    path = path.rstrip("/") or "/"
    return f"{p.scheme.lower() or 'https'}://{p.netloc.lower()}{path}"


def crawl_findings(site: str, max_pages: int = 12) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw = asyncio.run(_crawl(site, max_pages=max_pages))
    data = json.loads(raw)
    findings: list[dict[str, Any]] = []
    if not data.get("ok"):
        return findings, data
    titles: dict[str, list[str]] = {}
    seen_pages: set[str] = set()
    for page in data.get("pages", []):
        url, meta, excerpt = page.get("url"), page.get("meta") or {}, page.get("excerpt") or ""
        if not is_html_page(url):
            continue
        key = canonical_url(url)
        if key in seen_pages:
            continue  # the crawler fetched the same page under a second spelling
        seen_pages.add(key)
        title = (meta.get("title") or "").strip()
        desc = (meta.get("description") or "").strip()
        before = {"title": title, "description": desc, "body_chars": len(excerpt)}
        if not title:
            findings.append({"kind": "missing_title", "url": url, "why": "Page has no <title>.", "before": before})
        else:
            titles.setdefault(title.lower(), []).append((url, before))
        if not desc:
            findings.append({"kind": "missing_description", "url": url, "why": "Page has no meta description.", "before": before})
        if len(excerpt) < 300:
            findings.append({"kind": "thin_page", "url": url, "why": f"Only ~{len(excerpt)} characters of body text were extracted.", "before": before})
    for t, pages_ in titles.items():
        if len(pages_) > 1:
            urls = [u for u, _ in pages_]
            findings.append({"kind": "duplicate_title", "url": urls[0], "why": f"{len(urls)} pages share the title {t!r}: {', '.join(urls[:4])}",
                             "before": pages_[0][1]})
    if not data.get("sitemap_found") and not sitemap_under_path(site):
        findings.append({"kind": "no_sitemap", "url": site, "why": "No sitemap.xml was discovered at the host root or under the site path."})
    return findings, data


def authority_gap(user_domain: str, competitors: list[str]) -> dict[str, Any] | None:
    comp_domains = [_domain(c) for c in competitors if c]
    if not comp_domains:
        return None
    cmd = [sys.executable, str(VENDOR_SEO / "authority_mark.py"), "--user-domain", user_domain, "--json"]
    for d in comp_domains[:10]:
        cmd += ["--serp-domain", d]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=120)
    if proc.returncode != 0 or not proc.stdout.strip():
        return {"error": (proc.stderr or proc.stdout)[-300:]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": proc.stdout[-300:]}


def probe_repo(repo: Path) -> dict[str, Any] | None:
    if not repo.is_dir():
        return None
    proc = subprocess.run([sys.executable, str(VENDOR_SEO / "probe_project.py"), str(repo)], capture_output=True, text=True, check=False, timeout=120)
    if proc.returncode != 0:
        return {"error": proc.stderr[-300:]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"raw": proc.stdout[-1000:]}


class SeoWorker:
    name = "seo"
    description = "Crawl the site, compare authority with competitors, and queue prioritised SEO fixes."
    upstream = "marketingskills/seo scripts + pulse-cmo crawl"

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult:
        site = ctx.website
        if not site:
            return WorkerResult(ok=False, summary="", error="no website in revenueos.yaml — run `revenueos init`")
        created = 0
        findings, crawl = crawl_findings(site)
        signals = homepage_signals(site)
        if signals.get("ok"):
            for name in ("meta_pixel", "google_ads_tag", "ga4", "gtm", "booking_link", "tel_link", "local_schema"):
                store.record_metric(f"site_{name}", float(bool(signals.get(name))), site=site)
        findings += signal_findings(site, signals)
        for f in findings:
            src, skill = SKILL_FOR[f["kind"]]
            aid = store.create_action(
                "seo_opportunity", f"SEO: {f['kind'].replace('_', ' ')} — {f['url']}", f["why"],
                run_id=run_id, source_url=f["url"], dedupe_key=f"seo:{f['kind']}:{f['url']}",
                context={"kind": f["kind"], "skill": f"{src}/{skill}", "executor": "run_skill", "skill_input": f["why"], "before": f.get("before")},
            )
            created += 1 if aid else 0

        gap = authority_gap(_domain(site), ctx.competitors) if ctx.competitors else None
        if gap and not gap.get("error") and gap.get("user_dr") is not None:
            user_dr = gap["user_dr"]
            stronger = [r for r in gap.get("rows", []) if r.get("dr") is not None and r["dr"] > user_dr]
            if stronger:
                names = ", ".join(f"{r['domain']} (DR {r['dr']})" for r in stronger[:5])
                src, skill = SKILL_FOR["authority_gap"]
                aid = store.create_action(
                    "seo_opportunity", "SEO: competitors out-rank you on domain authority",
                    f"Your domain rating is {user_dr}; stronger competitors: {names}. "
                    f"Authority Mark (first competitor you cannot out-rank yet): {(gap.get('authority_mark') or {}).get('domain', 'none')}.",
                    run_id=run_id, dedupe_key="seo:authority_gap",
                    context={"kind": "authority_gap", "skill": f"{src}/{skill}", "executor": "run_skill",
                             "skill_input": json.dumps(gap)[:2000]},
                )
                created += 1 if aid else 0

        repo = (ctx.config.get("seo") or {}).get("site_repo")
        probe = probe_repo(Path(repo).expanduser()) if repo else None
        if probe and not probe.get("error") and not probe.get("hits", {}).get("indexing_surface"):
            aid = store.create_action(
                "seo_opportunity", "SEO: site repo has no sitemap/robots surface",
                "probe_project found no sitemap.xml, robots.txt or sitemap generator in the site source.",
                run_id=run_id, dedupe_key="seo:repo:no_indexing_surface",
                context={"kind": "indexing_surface", "skill": "seo/technical-seo-triage", "executor": "run_skill"},
            )
            created += 1 if aid else 0

        pages = crawl.get("pages_fetched", 0)
        return WorkerResult(ok=True, summary=f"{created} new site issue(s), {len(findings)} open, from {pages} crawled pages"
                            + (", authority compared" if gap and not gap.get("error") and gap.get("user_dr") is not None
                               else (f", authority unavailable ({gap.get('user_error') or gap.get('error')})" if gap else "")) + ".",
                            actions_created=created, details={"pages": pages, "authority": gap})
