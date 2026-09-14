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
    # "book" must start a word: facebook.com links are social profiles, not booking pages
    "booking_link": r"href=[\"'][^\"']*(?:(?<![a-z])book|appointment|reserve|schedule)[^\"']*[\"']",
    "phone_text": r"(?:\+[1-9]\d{0,2}[ \-]?(?:\(\d{1,4}\)[ \-]?)?\d(?:[ \-]?\d){6,11}|\(?0\d{1,3}\)?(?:[ \-]?\d){7,9})",
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
    # Site builders (GoDaddy, Wix) render the visible page from JSON inside <script>, so scan the whole
    # document as text (no tag stripping: a "<" inside a script would swallow the number). The digit
    # boundaries keep a 15-digit pixel id from looking like a phone number; "+0…" is a CSS unicode range.
    text = html_mod.unescape(html).replace("\\u002B", "+")
    # SVG path data is a stream of space/hyphen separated numbers, so a coordinate
    # run like "M0 1 2 2 2 2 0 0 1-2-2" matches the UK branch of phone_text exactly.
    # A site with an icon set therefore reports a page full of phantom phone numbers
    # (get-ryze.ai: 113 <svg> elements, 3 fabricated numbers, 2 false findings).
    # Strip vector and style payloads before scanning; <script> is deliberately kept
    # because site builders render visible copy from JSON inside it.
    text = re.sub(r"<svg\b.*?</svg>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"\sd=[\"'][^\"']*[\"']", " ", text)  # stray path data outside <svg>
    text = re.sub(r"[\u00a0\u2007\u202f\u2009]", " ", text)  # non-breaking and thin spaces inside numbers
    # A number that appears ONLY inside <script> is not a number the page shows. Squarespace keeps
    # its site config there and JSON-LD keeps `telephone` there, and a visitor sees neither. Three
    # outreach emails were drafted telling real businesses their phone number was plain text on
    # pages that display no phone number at all, entirely on that evidence. Scanning script stays,
    # because site builders do render visible copy from JSON inside it, but the two sources stop
    # being interchangeable: only a number found in visible markup can carry a claim about what a
    # visitor sees.
    visible_text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.S | re.I)
    phones = []
    # Two more false-positive shapes, seen on real sites: a date/timestamp run embedded in an
    # image filename ("2024-09-11-14-47-06" reads as local number "09-11-14-47-06") and a SKU or
    # tracking id that happens to be all digits but far longer than any real phone number
    # ("0462806720515", 13 digits). Neither is a phone number the extraction should ever surface,
    # so a candidate is rejected when: (a) it is immediately flanked by a separator that is itself
    # flanked by a digit \u2014 i.e. it is a slice out of a longer separator-joined numeric token \u2014 or
    # (b) its own digit count falls outside a real phone number's range (local: 9-11 digits
    # including the leading 0; international "+...": up to 15 digits, the ITU E.164 ceiling).
    # (c) A hex id ("01a05801-3517-74cc-…", a Shopify extension UUID) yields a slice such as
    # "05801-3517-74" whose neighbours are letters, not digits: a candidate touching a letter or
    # digit on either side is part of a longer token and is rejected too ("Phone:0438…" keeps its
    # colon and passes).
    boundary_pre, boundary_post = r"(?<![A-Za-z0-9])(?<!\d[\-/.:_])", r"(?![A-Za-z0-9])(?![\-/.:_]\d)"
    for m in re.finditer(boundary_pre + SIGNAL_PATTERNS["phone_text"] + boundary_post, text):
        ph = re.sub(r"\s+", " ", m.group(0)).strip()
        digit_chars = [ch for ch in ph if ch.isdigit()]
        digits = len(digit_chars)
        lo, hi = (8, 15) if ph.startswith("+") else (9, 11)
        # A run of one repeated digit ("0000000000") is an image/thumbnail cache-buster
        # placeholder, never a real phone number, no matter how many digits it has.
        if lo <= digits <= hi and len(set(digit_chars)) > 1 and ph not in phones:
            phones.append(ph)
    intl = [ph for ph in phones if ph.startswith("+")]
    out["phones"] = (intl or phones)[:3]  # a site that prints its country code is telling you which numbers are its own
    # The same validated candidates, restricted to what renders.
    seen_visible: list[str] = []
    for m in re.finditer(boundary_pre + SIGNAL_PATTERNS["phone_text"] + boundary_post, visible_text):
        ph = re.sub(r"\s+", " ", m.group(0)).strip()
        if ph in out["phones"] and ph not in seen_visible:
            seen_visible.append(ph)
    out["phones_visible"] = seen_visible
    out["phones_script_only"] = [ph for ph in out["phones"] if ph not in seen_visible]
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
    if signals.get("phones_visible"):
        facts.append("phone numbers shown on the page: " + ", ".join(signals["phones_visible"]))
    if signals.get("booking_url"):
        facts.append(f"booking link: {signals['booking_url']}")
    if signals.get("emails"):
        facts.append("email on the page: " + ", ".join(signals["emails"]))
    observed = (" Observed: " + "; ".join(facts) + ".") if facts else ""
    # Gate on the validated, de-duplicated list rather than the raw regex hit:
    # the boolean stays true for any digit run that merely looked like a number.
    # Only a number the visitor can see supports "the homepage shows a phone number". With phones
    # extracted but none of them visible, the page renders no number and there is no finding here.
    if signals.get("phones_visible") and not signals.get("tel_link"):
        out.append({"kind": "phone_not_tappable", "url": url,
                    "why": "The homepage shows a phone number as plain text with no tel: link, so visitors on a phone cannot tap to call." + observed,
                    "before": {"tel_link": False, "phone_text": True,
                               "phones": signals.get("phones_visible", [])}})
    # Only a business that actually trades from a place should carry LocalBusiness
    # markup. Firing this on every site tells a B2B SaaS to add opening hours it
    # does not have — a fabricated finding. Require evidence of a physical
    # presence first: a phone number, a booking link, or a postal address.
    looks_local = bool(signals.get("phones") or signals.get("booking_url") or signals.get("address"))
    if looks_local and not signals.get("local_schema"):
        out.append({"kind": "no_local_schema", "url": url,
                    "why": "No LocalBusiness schema.org markup on the homepage; Google cannot read the business type, address and hours from the page." + observed,
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


def site_file_for(url: str, site: str) -> str:
    """The HTML file behind a URL on a static site: '/' → index.html, '/pricing.html' → pricing.html, '/about/' → about/index.html."""
    path = urlparse(url).path or "/"
    base = urlparse(site if site.startswith("http") else f"https://{site}").path.rstrip("/")
    if base and path.startswith(base):
        path = path[len(base):] or "/"
    if path.endswith("/"):
        return (path.strip("/") + "/index.html").lstrip("/")
    return path.lstrip("/") or "index.html"


def deployable_fix(finding: dict[str, Any], ctx: BusinessContext, signals: dict[str, Any] | None) -> dict[str, Any] | None:
    """The exact <head> change for findings that have one; None means it stays a deliverable for a human."""
    kind = finding["kind"]
    if kind == "no_canonical":
        return {"kind": "canonical", "url": finding["url"]}
    if kind == "no_local_schema":
        data: dict[str, Any] = {"@context": "https://schema.org", "@type": "LocalBusiness", "name": ctx.company_name, "url": finding["url"]}
        phones = (signals or {}).get("phones") or []
        if phones:
            data["telephone"] = phones[0]
        desc = ctx.section("identity.md", "One-Sentence Definition")
        if desc and "[" not in desc:
            data["description"] = desc[:300]
        return {"kind": "jsonld", "data": data}
    return None


def passed_checks(crawl: dict[str, Any], signals: dict[str, Any], findings: list[dict[str, Any]]) -> list[str]:
    """What was checked and found fine — shown next to the findings so the audit is not only a list of problems."""
    kinds = {f["kind"] for f in findings}
    out = []
    if crawl.get("ok"):
        if crawl.get("sitemap_found") or "no_sitemap" not in kinds:
            out.append("sitemap present")
        if "missing_title" not in kinds:
            out.append("every crawled page has a title")
        if "missing_description" not in kinds:
            out.append("every crawled page has a meta description")
        if "thin_page" not in kinds:
            out.append("no thin pages")
        if "duplicate_title" not in kinds:
            out.append("no duplicate titles")
    if signals.get("ok"):
        if signals.get("tel_link") or not signals.get("phone_text"):
            out.append("phone number tappable" if signals.get("tel_link") else "no phone number shown")
        if signals.get("local_schema"):
            out.append("LocalBusiness schema present")
        if signals.get("canonical"):
            out.append("canonical declared")
        if signals.get("booking_link"):
            out.append("online booking link present")
    return out


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
        from ..connections import ConnectionStore

        cs = ConnectionStore(ws)
        site_conn = cs.get("github_site") or cs.get("wordpress")
        for f in findings:
            src, skill = SKILL_FOR[f["kind"]]
            context = {"kind": f["kind"], "skill": f"{src}/{skill}", "executor": "run_skill", "skill_input": f["why"], "before": f.get("before")}
            fix = deployable_fix(f, ctx, signals if f["kind"] in ("no_canonical", "no_local_schema") else None)
            if site_conn and fix:
                context.update({"executor": "site_deploy", "fix": fix, "file": site_file_for(f["url"], site), "page_id": site_conn.meta.get("home_page_id")})
            aid = store.create_action(
                "seo_opportunity", f"SEO: {f['kind'].replace('_', ' ')} — {f['url']}", f["why"] + (" Deployable: the fix is applied to the site on Execute." if site_conn and fix else ""),
                run_id=run_id, source_url=f["url"], dedupe_key=f"seo:{f['kind']}:{f['url']}", context=context,
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
                            actions_created=created,
                            details={"pages": pages, "authority": gap,
                                     "urls": [pg.get("url") for pg in (crawl.get("pages") or []) if pg.get("url")][:60],
                                     "signals": {k: signals.get(k) for k in ("meta_pixel", "google_ads_tag", "ga4", "booking_link", "tel_link", "local_schema", "canonical")} if signals.get("ok") else {},
                                     "phones": signals.get("phones") or [],
                                     "findings": [{"kind": f["kind"], "url": f["url"]} for f in findings],
                                     "passed": passed_checks(crawl, signals, findings)})
