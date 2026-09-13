"""Lead sources that find the ICP: businesses with a website of their own, live ad tags on it, and a
published business address — evidence first, then the gate (revenueos.qualify) decides.

OpenStreetMap (Overpass API, no credential, ODbL) lists local businesses with a `website` tag by kind
and area. Each candidate's homepage is fetched once: ad tags (Meta Pixel, Google Ads, GA4), booking
link, phone, and email addresses on the page or its contact page. Only what was observed goes into
the lead's `reason`; nothing is inferred from a directory listing alone.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from ..qualify import FREEMAIL_DOMAINS, email_domain, website_host
from .seo import parse_signals

UA = "RevenueOS/0.2 (+https://unempyd.github.io/revenueos/)"
OVERPASS = ("https://overpass-api.de/api/interpreter", "https://lz4.overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter")
DEFAULT_KINDS = (
    'shop="hairdresser"', 'shop="beauty"', 'amenity="dentist"', 'amenity="clinic"', 'amenity="veterinary"',
    'shop="florist"', 'leisure="fitness_centre"', 'office="lawyer"', 'office="accountant"', 'shop="car_repair"',
    'craft="plumber"', 'craft="electrician"', 'shop="optician"', 'amenity="restaurant"', 'amenity="cafe"',
)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
ROLE_FIRST = ("hello", "info", "contact", "bookings", "booking", "enquiries", "enquiry", "admin", "office", "reception", "sales")


def overpass_query(bbox: tuple[float, float, float, float], kinds: tuple[str, ...] | list[str], limit: int) -> str:
    s, w, n, e = bbox
    parts = "".join(f'nwr[{k}]["website"];' for k in kinds)
    return f"[out:json][timeout:90][bbox:{s},{w},{n},{e}];({parts});out tags {limit};"


def fetch_osm(bbox: tuple[float, float, float, float], kinds: tuple[str, ...] | list[str] = DEFAULT_KINDS,
              limit: int = 600, timeout: float = 150.0) -> list[dict[str, Any]]:
    """Businesses with a website tag in the box. Returns [] when every mirror refuses."""
    q = overpass_query(bbox, kinds, limit)
    for host in OVERPASS:
        try:
            r = httpx.post(host, data={"data": q}, headers={"User-Agent": UA}, timeout=timeout)
        except httpx.HTTPError:
            continue
        if r.status_code != 200:
            continue
        rows = []
        for el in r.json().get("elements", []):
            t = el.get("tags") or {}
            if not t.get("name") or not t.get("website"):
                continue
            rows.append({
                "osm_id": f"{el.get('type', 'n')[0]}{el.get('id')}", "name": t["name"], "website": t["website"],
                "email": t.get("email") or t.get("contact:email") or "", "phone": t.get("phone") or t.get("contact:phone") or "",
                "kind": t.get("shop") or t.get("amenity") or t.get("office") or t.get("craft") or t.get("leisure") or "business",
                "suburb": t.get("addr:suburb") or t.get("addr:city") or "",
            })
        return rows
    return []


def _pick_email(candidates: list[str], domain: str) -> str | None:
    own = sorted({e.lower() for e in candidates if email_domain(e) == domain})
    if not own:
        return None
    own.sort(key=lambda e: (0 if e.split("@")[0] in ROLE_FIRST else 1, e))
    return own[0]


async def enrich(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    """One homepage fetch (+ /contact when no address on the domain was found)."""
    url = url if url.startswith("http") else f"https://{url}"
    try:
        r = await client.get(url, timeout=12.0)
    except httpx.HTTPError as e:
        return {"ok": False, "error": type(e).__name__}
    if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
        return {"ok": False, "error": f"http {r.status_code}"}
    host = website_host(str(r.url)) or ""
    from ..qualify import email_domain as _dom  # local alias keeps the import graph simple

    html = r.text[:800_000]
    sig = parse_signals(html, str(r.url))
    emails = set(sig.get("emails") or []) | {e.lower() for e in EMAIL_RE.findall(html)}
    domain = host
    if not any(_dom(e) == domain for e in emails):
        for path in ("/contact", "/contact-us", "/about"):
            try:
                r2 = await client.get(f"{r.url.scheme}://{r.url.host}{path}", timeout=10.0)
            except httpx.HTTPError:
                continue
            if r2.status_code == 200 and "text/html" in r2.headers.get("content-type", ""):
                emails |= {e.lower() for e in EMAIL_RE.findall(r2.text)}
                if any(_dom(e) == domain for e in emails):
                    break
    emails = {e for e in emails if not e.endswith((".png", ".jpg", ".svg", ".js", ".css", ".webp")) and "sentry" not in e}
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return {
        "ok": True, "url": str(r.url), "domain": domain, "signals": sig,
        "business_email": _pick_email(sorted(emails), domain),
        "freemail": sorted(e for e in emails if (email_domain(e) or "") in FREEMAIL_DOMAINS)[:2],
        "title": re.sub(r"\s+", " ", title.group(1)).strip()[:120] if title else "",
    }


async def _enrich_all(urls: list[str], concurrency: int = 12) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(headers={"User-Agent": UA}, follow_redirects=True) as client:
        async def one(u: str) -> dict[str, Any]:
            async with sem:
                return await enrich(client, u)
        return await asyncio.gather(*(one(u) for u in urls))


def evidence(row: dict[str, Any], info: dict[str, Any], area: str) -> str:
    sig = info.get("signals") or {}
    tags = [n for n, k in (("Meta Pixel", "meta_pixel"), ("Google Ads tag", "google_ads_tag"), ("GA4", "ga4")) if sig.get(k)]
    bits = [f"{row.get('kind', 'business')} in {row.get('suburb') or area} (OpenStreetMap)"]
    bits.append(("ad/analytics tags on the site: " + ", ".join(tags)) if tags else "no ad tags found on the site")
    if sig.get("booking_url") or sig.get("booking_link"):
        bits.append("online booking link")
    if info.get("business_email"):
        bits.append(f"business email published on the site: {info['business_email']}")
    elif row.get("email"):
        bits.append(f"email listed on OpenStreetMap: {row['email']}")
    if sig.get("phone_text") and not sig.get("tel_link"):
        bits.append("phone shown but not tappable")
    if not sig.get("local_schema"):
        bits.append("no LocalBusiness schema")
    return "; ".join(bits)


def osm_leads(cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Lead-sheet rows for discover.ingest from an `discover.osm` config block:
    {bbox: [s, w, n, e], area: "Adelaide", kinds: [...], limit: 600, require_ad_tag: true}."""
    bbox = tuple(float(x) for x in cfg["bbox"])
    kinds = tuple(cfg.get("kinds") or DEFAULT_KINDS)
    area = cfg.get("area") or "the area"
    candidates = fetch_osm(bbox, kinds, int(cfg.get("limit", 600)))
    infos = asyncio.run(_enrich_all([c["website"] for c in candidates])) if candidates else []
    rows, stats = [], {"candidates": len(candidates), "reachable": 0, "with_ad_tag": 0, "with_email": 0, "staged": 0}
    for c, info in zip(candidates, infos, strict=True):
        if not info.get("ok"):
            continue
        stats["reachable"] += 1
        sig = info["signals"]
        has_ads = bool(sig.get("meta_pixel") or sig.get("google_ads_tag"))
        stats["with_ad_tag"] += 1 if has_ads else 0
        email = info.get("business_email") or (c.get("email") if email_domain(c.get("email") or "") == info["domain"] else "") or ""
        stats["with_email"] += 1 if email else 0
        if cfg.get("require_ad_tag", True) and not has_ads:
            continue
        if not email:
            continue
        rows.append({
            "email": email, "first_name": "", "last_name": "", "company": c["name"], "title": "",
            "website": info["url"], "linkedin_url": "", "reason": evidence(c, info, area), "lead_id": f"osm:{c['osm_id']}",
            "_source": "osm", "_signals": {k: sig.get(k) for k in ("meta_pixel", "google_ads_tag", "ga4", "booking_link", "tel_link", "phone_text", "local_schema")},
        })
        stats["staged"] += 1
    return rows, stats
