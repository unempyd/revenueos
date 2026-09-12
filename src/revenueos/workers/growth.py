"""GROWTH — real, external adoption numbers for the business's own public surfaces.

Reads what the outside world actually did, and records it as metrics so RESULTS shows
external outcomes instead of "a file was written":

  * GitHub repository (`growth.github_repo` in revenueos.yaml, e.g. "owner/name"): stars,
    forks, watchers, open issues, unique visitors and views (14 days), unique cloners and
    clones (14 days), release asset downloads, and issues labelled `hosted-access` (leads).
    Traffic endpoints need the owner's token: the `gh` CLI if signed in, else GITHUB_TOKEN.
  * PyPI (`growth.pypi_package`): recent downloads from pypistats (public, no key).
  * Website (`growth.site_url`): reachability and sitemap presence (crawl), so a dead site
    is caught the same day.

Each run records a `growth_*` metric with the current value; the previous value is kept in
the metrics table, so RESULTS can show the delta since the last run. Nothing here is a proxy
for revenue: paid subscriptions arrive through Stripe (`billing`), and are counted there.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

import httpx

from ..context import BusinessContext
from ..llm import LLM
from ..paths import Workspace
from ..store import Store
from . import WorkerResult

GH_API = "https://api.github.com"


def _gh(path: str) -> dict[str, Any] | list[Any] | None:
    """GitHub API via the signed-in gh CLI (has the owner's token) or GITHUB_TOKEN, else anonymous."""
    if shutil.which("gh"):
        proc = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False, timeout=30)
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                return json.loads(proc.stdout)
            except json.JSONDecodeError:
                return None
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "revenueos-growth"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    try:
        r = httpx.get(f"{GH_API}/{path}", headers=headers, timeout=20.0)
        return r.json() if r.status_code == 200 else None
    except httpx.HTTPError:
        return None


def github_metrics(repo: str) -> dict[str, float]:
    out: dict[str, float] = {}
    info = _gh(f"repos/{repo}")
    if isinstance(info, dict) and "stargazers_count" in info:
        out["growth_github_stars"] = float(info["stargazers_count"])
        out["growth_github_forks"] = float(info["forks_count"])
        out["growth_github_watchers"] = float(info.get("subscribers_count") or 0)
        out["growth_github_open_issues"] = float(info.get("open_issues_count") or 0)
    views = _gh(f"repos/{repo}/traffic/views")
    if isinstance(views, dict) and "count" in views:
        out["growth_github_views_14d"] = float(views["count"])
        out["growth_github_unique_visitors_14d"] = float(views["uniques"])
    clones = _gh(f"repos/{repo}/traffic/clones")
    if isinstance(clones, dict) and "count" in clones:
        out["growth_github_clones_14d"] = float(clones["count"])
        out["growth_github_unique_cloners_14d"] = float(clones["uniques"])
    releases = _gh(f"repos/{repo}/releases")
    if isinstance(releases, list):
        out["growth_release_downloads"] = float(sum(a.get("download_count", 0) for rel in releases for a in rel.get("assets", [])))
    leads = _gh(f"repos/{repo}/issues?labels=hosted-access&state=all&per_page=100")
    if isinstance(leads, list):
        out["growth_hosted_access_requests"] = float(len([i for i in leads if "pull_request" not in i]))
    return out


def pypi_metrics(package: str) -> dict[str, float]:
    try:
        r = httpx.get(f"https://pypistats.org/api/packages/{package}/recent", timeout=20.0)
        if r.status_code == 200:
            d = r.json().get("data", {})
            return {"growth_pypi_downloads_week": float(d.get("last_week", 0)), "growth_pypi_downloads_month": float(d.get("last_month", 0))}
    except httpx.HTTPError:
        pass
    return {}


def site_metrics(url: str) -> dict[str, float]:
    try:
        r = httpx.get(url, timeout=20.0, follow_redirects=True)
        ok = float(r.status_code == 200)
        sm = httpx.get(url.rstrip("/") + "/sitemap.xml", timeout=20.0, follow_redirects=True)
        return {"growth_site_reachable": ok, "growth_site_sitemap": float(sm.status_code == 200)}
    except httpx.HTTPError:
        return {"growth_site_reachable": 0.0, "growth_site_sitemap": 0.0}


class GrowthWorker:
    name = "growth"
    description = "Record real external adoption: GitHub stars/traffic/clones, hosted-access requests, PyPI downloads, site reachability."
    upstream = "GitHub REST API, pypistats, site crawl"

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult:
        cfg = ctx.config.get("growth") or {}
        before = store.latest_metrics()
        found: dict[str, float] = {}
        if cfg.get("github_repo"):
            found.update(github_metrics(cfg["github_repo"]))
        if cfg.get("pypi_package"):
            found.update(pypi_metrics(cfg["pypi_package"]))
        if cfg.get("site_url") or ctx.website:
            found.update(site_metrics(cfg.get("site_url") or ctx.website))
        if not found:
            return WorkerResult(ok=False, summary="", error="nothing to measure: set growth.github_repo / growth.pypi_package / growth.site_url in revenueos.yaml")
        deltas = {}
        for k, v in found.items():
            store.record_metric(k, v, source="growth")
            if k in before:
                deltas[k] = v - before[k]
        created = 0
        leads = int(found.get("growth_hosted_access_requests", 0))
        if leads and leads > int(before.get("growth_hosted_access_requests", 0)):
            aid = store.create_action("prospect", f"{leads} hosted-access request(s) on GitHub — reply and send a checkout link",
                                      "Someone asked for RevenueOS Hosted through the issue template. This is a real inbound lead.",
                                      run_id=run_id, dedupe_key=f"hosted-access:{leads}",
                                      source_url=f"https://github.com/{cfg.get('github_repo')}/issues?q=label%3Ahosted-access")
            created += 1 if aid else 0
        head = ", ".join(f"{k.removeprefix('growth_')}={v:g}" + (f" ({deltas[k]:+g})" if k in deltas and deltas[k] else "") for k, v in sorted(found.items()))
        return WorkerResult(ok=True, summary=f"external: {head}", actions_created=created, details={"metrics": found, "deltas": deltas})
