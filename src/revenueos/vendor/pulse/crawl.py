"""Lightweight site crawling — no Playwright, just httpx + selectolax.

Good enough for sitemaps and SSR'd marketing pages. SPA-heavy sites that
hide content behind hydration will return less, which is fine for MVP.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import structlog
from selectolax.parser import HTMLParser

from .registry import Tool, tool

log = structlog.get_logger()

USER_AGENT = "pulse-bot/0.1 (+https://pulse.cc)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}


# --- GitHub repos ----------------------------------------------------------

_GITHUB_RE = re.compile(r"^https?://(www\.)?github\.com/([^/\s]+)/([^/\s?#]+)", re.I)
_RESERVED = {"features", "topics", "trending", "marketplace", "sponsors", "about",
             "pricing", "explore", "settings", "orgs", "apps", "collections"}


def _github_repo(url: str) -> tuple[str, str] | None:
    """Return (owner, repo) if url is a github.com/owner/repo, else None."""
    m = _GITHUB_RE.match(url)
    if not m:
        return None
    owner, repo = m.group(2), m.group(3)
    if owner.lower() in _RESERVED:
        return None
    return owner, repo.removesuffix(".git")


async def _fetch_github_repo(client: httpx.AsyncClient, owner: str, repo: str) -> dict:
    """Pull clean repo metadata + README via the GitHub API (no auth needed for
    public repos; uses GITHUB_TOKEN if set for a higher rate limit)."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    api = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        r = await client.get(api, headers=headers, timeout=15.0, follow_redirects=True)
    except Exception as e:
        return {"ok": False, "error": f"github api fetch failed: {e}"}
    if r.status_code == 404:
        return {"ok": False, "error": "repo not found", "not_repo": True}
    if r.status_code >= 400:
        return {"ok": False, "error": f"github api returned {r.status_code}"}
    data = r.json()

    # README (raw)
    readme = ""
    try:
        rr = await client.get(
            f"{api}/readme",
            headers={**headers, "Accept": "application/vnd.github.raw+json"},
            timeout=15.0,
            follow_redirects=True,
        )
        if rr.status_code == 200:
            readme = rr.text
    except Exception:
        pass
    # strip markdown noise + badges, keep prose
    readme_text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", readme)        # images/badges
    readme_text = re.sub(r"<[^>]+>", " ", readme_text)               # html tags
    readme_text = re.sub(r"\s+", " ", readme_text).strip()[:5000]

    return {
        "ok": True,
        "kind": "github_repo",
        "full_name": data.get("full_name"),
        "name": data.get("name"),
        "description": data.get("description") or "",
        "homepage": (data.get("homepage") or "").strip(),
        "topics": data.get("topics") or [],
        "language": data.get("language"),
        "stars": data.get("stargazers_count"),
        "forks": data.get("forks_count"),
        "open_issues": data.get("open_issues_count"),
        "license": (data.get("license") or {}).get("spdx_id"),
        "html_url": data.get("html_url"),
        "readme_excerpt": readme_text,
    }


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url, headers=HEADERS, follow_redirects=True, timeout=15.0)
        if r.status_code >= 400:
            return ""
        ctype = r.headers.get("content-type", "")
        if "text/html" not in ctype and "application/xhtml" not in ctype:
            return r.text if "xml" in ctype else ""
        return r.text
    except Exception as e:
        log.warning("fetch_failed", url=url, error=repr(e))
        return ""


def _extract_text(html: str, max_chars: int = 3000) -> str:
    if not html:
        return ""
    tree = HTMLParser(html)
    for sel in ["script", "style", "noscript", "iframe"]:
        for node in tree.css(sel):
            node.decompose()
    body = tree.css_first("main") or tree.css_first("article") or tree.body
    if body is None:
        return ""
    text = re.sub(r"\s+", " ", body.text(separator=" ")).strip()
    return text[:max_chars]


def _extract_meta(html: str) -> dict[str, str]:
    if not html:
        return {}
    tree = HTMLParser(html)
    out: dict[str, str] = {}
    title = tree.css_first("title")
    if title and title.text():
        out["title"] = title.text().strip()
    for tag in tree.css("meta"):
        name = tag.attributes.get("name") or tag.attributes.get("property") or ""
        content = tag.attributes.get("content") or ""
        if not name or not content:
            continue
        if name in ("description", "og:title", "og:description", "og:image", "twitter:title"):
            out[name] = content.strip()[:300]
    h1 = tree.css_first("h1")
    if h1 and h1.text():
        out["h1"] = h1.text().strip()[:200]
    return out


def _extract_links(html: str, base_url: str, same_host: bool = True) -> list[str]:
    if not html:
        return []
    tree = HTMLParser(html)
    base_host = urlparse(base_url).netloc
    out: set[str] = set()
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        if not href or href.startswith("#") or href.startswith("mailto:"):
            continue
        full = urljoin(base_url, href)
        if same_host and urlparse(full).netloc != base_host:
            continue
        out.add(full.split("#")[0])
    return sorted(out)


def _dump_capped(obj: dict, limit: int = 12000) -> str:
    """Serialize to JSON under a char budget WITHOUT cutting mid-string.

    The old code did `json.dumps(...)[:limit]`, which could slice inside a
    string and hand the model invalid JSON. Instead we drop whole trailing
    pages (least important) until it fits, so the result always parses.
    """
    s = json.dumps(obj, ensure_ascii=False)
    if len(s) <= limit:
        return s
    pages = obj.get("pages")
    if isinstance(pages, list):
        while len(pages) > 1 and len(json.dumps(obj, ensure_ascii=False)) > limit:
            pages.pop()
        if pages and len(json.dumps(obj, ensure_ascii=False)) > limit:
            pages[0]["excerpt"] = (pages[0].get("excerpt") or "")[: max(0, limit - 1200)]
    s = json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit]


def distill_crawl(obj: dict) -> dict:
    """Compact a crawl_website result into the evidence block downstream
    generators read (persisted as the project's crawl_summary)."""
    pages = obj.get("pages") or []
    home = pages[0] if pages else {}
    meta = home.get("meta") or {}
    texts = []
    for p in pages:
        ex = (p.get("excerpt") or "").strip()
        if ex:
            texts.append(f"[{p.get('url')}]\n{ex}")
    return {
        "site": obj.get("site"),
        "source": obj.get("source") or "web",
        "title": meta.get("title") or "",
        "description": meta.get("description") or meta.get("og:description") or "",
        "h1": meta.get("h1") or "",
        "pages_fetched": obj.get("pages_fetched"),
        "text": "\n\n".join(texts)[:6000],
        "repo": obj.get("repo"),  # present only for github projects
    }


async def _try_sitemap(client: httpx.AsyncClient, root: str) -> list[str]:
    parsed = urlparse(root)
    sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
    text = await _fetch(client, sitemap_url)
    if not text:
        return []
    locs = re.findall(r"<loc>([^<]+)</loc>", text)
    return [u.strip() for u in locs if u.strip()][:50]


@tool
async def crawl_website(url: str, max_pages: int = 10) -> str:
    """Crawl a website and extract its product info, positioning, and structure.

    Reads the homepage, follows sitemap or homepage links, returns metadata
    + text from up to `max_pages` pages. Use this for the first analysis of
    a user's own site, or when analyzing a competitor.

    Args:
        url: Full homepage URL with https://.
        max_pages: Max pages to read (default 10, capped at 20).
    """
    max_pages = max(1, min(int(max_pages), 20))
    if not url.startswith("http"):
        url = "https://" + url

    # GitHub repo? pull clean metadata + README via the API instead of
    # scraping the JS-heavy repo page (and crawling issues/commits).
    gh = _github_repo(url)
    if gh:
        async with httpx.AsyncClient() as client:
            repo = await _fetch_github_repo(client, *gh)
        if repo.get("ok"):
            return json.dumps({
                "ok": True,
                "site": url,
                "source": "github",
                "pages_fetched": 1,
                "repo": {k: v for k, v in repo.items() if k not in ("ok", "kind")},
                # synthesize a page so downstream brand-voice / product-info
                # extraction has familiar fields to work with
                "pages": [{
                    "url": url,
                    "meta": {
                        "title": repo.get("full_name"),
                        "description": repo.get("description"),
                        "h1": repo.get("name"),
                    },
                    "excerpt": (
                        f"{repo.get('description') or ''}\n\n"
                        f"Language: {repo.get('language')} · {repo.get('stars')} stars · "
                        f"license: {repo.get('license') or 'n/a'} · "
                        f"topics: {', '.join(repo.get('topics') or []) or 'none'}\n\n"
                        f"README:\n{repo.get('readme_excerpt') or ''}"
                    ),
                }],
            }, ensure_ascii=False)[:12000]
        # if it wasn't actually a repo, fall through to a normal crawl

    async with httpx.AsyncClient() as client:
        home_html = await _fetch(client, url)
        if not home_html:
            return json.dumps({"ok": False, "error": f"could not fetch {url}"})

        home_meta = _extract_meta(home_html)
        home_text = _extract_text(home_html, max_chars=4000)

        sitemap_urls = await _try_sitemap(client, url)
        link_urls = _extract_links(home_html, url, same_host=True)

        seen = {url}
        candidates = []
        for u in sitemap_urls + link_urls:
            if u in seen:
                continue
            seen.add(u)
            candidates.append(u)
            if len(candidates) >= max_pages - 1:
                break

        async def fetch_page(u: str) -> dict:
            html = await _fetch(client, u)
            return {
                "url": u,
                "meta": _extract_meta(html),
                "excerpt": _extract_text(html, max_chars=1500),
            }

        sub_results = await asyncio.gather(
            *[fetch_page(u) for u in candidates], return_exceptions=False
        )

    pages = [
        {"url": url, "meta": home_meta, "excerpt": home_text},
        *[p for p in sub_results if p["meta"] or p["excerpt"]],
    ]

    return _dump_capped(
        {
            "ok": True,
            "site": url,
            "pages_fetched": len(pages),
            "sitemap_found": bool(sitemap_urls),
            "pages": pages,
        }
    )


async def _web_search_fallback(base_url: str, api_key: str, query: str, n: int = 5) -> list[dict]:
    """Snippet-level fallback when a crawl returns almost nothing (JS-rendered
    sites). Hits the same OpenAdapter search endpoint the web tools use."""
    if not (base_url and api_key):
        return []
    try:
        async with httpx.AsyncClient(timeout=20.0) as cx:
            r = await cx.post(
                f"{base_url.rstrip('/')}/v1/tools/search",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"query": query, "num_results": n},
            )
        if r.status_code >= 400:
            return []
        data = r.json()
        items = data.get("results") or data.get("data") or data.get("items") or []
        out = []
        for it in items[:n]:
            out.append({
                "title": (it.get("title") or it.get("name") or "").strip(),
                "url": it.get("url") or it.get("link") or "",
                "snippet": (it.get("snippet") or it.get("description") or it.get("content") or "").strip()[:280],
            })
        return [o for o in out if o["snippet"] or o["title"]]
    except Exception as e:
        log.warning("competitor_search_fallback_failed", query=query, error=repr(e))
        return []


def make_crawl_tools(
    store: Any = None,
    project_id: int | None = None,
    *,
    web_base_url: str = "",
    web_api_key: str = "",
) -> list[Tool]:
    """Crawl tools. When (store, project_id) are bound, `analyze_competitor`
    PERSISTS what it crawled into the project's competitor_reads so the
    competitor-analysis + market-gap generators work from real evidence instead
    of the competitor's name. (Previously the crawl was thrown away.)"""

    @tool
    async def analyze_competitor(competitor_url: str) -> str:
        """Crawl a competitor site and extract its real positioning, pricing,
        and features. The findings are saved so the Competitor Analysis and
        market-gap tools use this evidence, not just the competitor's name.

        Args:
            competitor_url: Competitor homepage URL.
        """
        raw = await crawl_website.fn(url=competitor_url, max_pages=6)
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            obj = {"ok": False}
        if not obj.get("ok"):
            return raw
        read = distill_crawl(obj)
        host = (urlparse(read.get("site") or competitor_url).hostname or "").replace("www.", "")
        read["url"] = read.get("site") or competitor_url
        read["name"] = read.get("title") or host
        # JS-rendered competitor sites give the lightweight crawler almost
        # nothing — back-fill with search snippets so the analysis isn't blind.
        if len(read.get("text") or "") < 200 and store is not None:
            snfrom = host or competitor_url
            snippets = await _web_search_fallback(
                web_base_url, web_api_key, f"{snfrom} pricing positioning alternative", 5
            )
            if snippets:
                read["search_snippets"] = snippets
                read["source"] = "search"
        if store is not None and project_id is not None:
            try:
                store.add_competitor_read(project_id, read)
            except Exception as e:
                log.warning("persist_competitor_read_failed", error=repr(e))
        return _dump_capped(obj)

    return [crawl_website, analyze_competitor]
