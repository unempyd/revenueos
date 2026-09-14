"""DISCOVER — find prospects and let the gate say which are qualified.

Sources, in order:
  0. OpenStreetMap + the business's own homepage (`discover.osm` in revenueos.yaml): local
     businesses with a website tag in a bounding box; each homepage is read for ad tags, a
     booking link, phone and a business email. Evidence goes into `reason` (workers/sources.py).
  1. OpenOutreach (GPL-3.0, eracle/OpenOutreach) across a process boundary: if the
     `openoutreach` CLI is installed (pip) or the container is up (services/openoutreach),
     run `openoutreach find N --json` and ingest its JSON-Lines.  Nothing from that repo is
     linked into this process; stdout is the whole contract, exactly as its SKILL.md states.
  2. CSV drops in data/exports/leads*.csv using the OpenOutreach / Instantly / Smartlead
     column names (email, first_name, last_name, company, title, website, linkedin_url, reason).
Every row passes the qualification gate (revenueos.qualify) before anything else looks at
it. Only a QUALIFIED lead — business email + business website + a real company name —
becomes a `prospect` action and counts on TODAY. A `qualified_at` column in an import is
ignored: files describe leads, they do not get to assert qualification.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
from typing import Any

from ..context import BusinessContext
from ..llm import LLM
from ..paths import Workspace
from ..qualify import clean_company, normalise_row, qualify
from ..store import Store
from . import WorkerResult

LEAD_COLUMNS = ("email", "first_name", "last_name", "company", "title", "website", "linkedin_url", "reason")


def openoutreach_command() -> list[str] | None:
    """How to reach OpenOutreach without linking it: local CLI, or the compose service."""
    override = os.environ.get("REVENUEOS_OPENOUTREACH_CMD")
    if override:
        return override.split()
    if shutil.which("openoutreach"):
        return ["openoutreach"]
    if shutil.which("docker"):
        probe = subprocess.run(["docker", "compose", "ps", "-q", "openoutreach"], capture_output=True, text=True, check=False)
        if probe.returncode == 0 and probe.stdout.strip():
            return ["docker", "compose", "exec", "-T", "openoutreach", "openoutreach"]
    return None


def fetch_openoutreach(goal: int = 5, timeout: int = 900) -> list[dict[str, Any]]:
    cmd = openoutreach_command()
    if not cmd:
        return []
    proc = subprocess.run([*cmd, "find", str(goal), "--json"], capture_output=True, text=True, check=False, timeout=timeout)
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():  # exit != 0 still means rows already on stdout (partial goal)
        line = line.strip()
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def read_csv_drops(ws: Workspace) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(ws.exports.glob("leads*.csv")):
        with path.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
                if r.get("email") or r.get("company"):
                    r["_source"] = f"csv:{path.name}"
                    rows.append(r)
    return rows


def ingest(store: Store, rows: list[dict[str, Any]], run_id: int, default_source: str) -> dict[str, int]:
    """Upsert every row as a lead, run the gate, and create a prospect action only for the
    qualified ones. Returns the funnel for this batch: found, contactable, qualified, created."""
    found = contactable = qualified = created = 0
    for raw in rows:
        r = normalise_row(raw)
        source = r.get("_source", default_source)
        source_id = r.get("lead_id") or r.get("email") or r.get("linkedin_url") or r.get("profile_url") or r.get("website")
        company, _problem = clean_company(r.get("company") or r.get("business_name") or "")
        if not company and r.get("email"):
            company = r["email"].split("@")[-1]
        if not company:
            continue
        found += 1
        verdict = qualify(r)
        contactable += 1 if verdict.contactable else 0
        lead_id = store.upsert_lead(
            source, source_id, company,
            first_name=r.get("first_name"), last_name=r.get("last_name"), title=r.get("title"),
            website_url=r.get("website") or None, linkedin_url=r.get("linkedin_url") or r.get("profile_url") or None,
            contact_email=(r.get("email") or None), reason=r.get("reason"),
        )
        sig = r.get("_signals") or {}
        score = lead_value(sig) if verdict.qualified else 0
        store.set_qualification(lead_id, verdict.qualified, verdict.reasons, score=score, signals=sig or None)
        if not verdict.qualified:
            continue
        qualified += 1
        who = " ".join(x for x in (r.get("first_name"), r.get("last_name")) if x) or r.get("email") or company
        title = f"{who} at {company}" if who != company else company
        aid = store.create_action(
            "prospect", f"Qualified prospect: {title}",
            r.get("reason") or f"Found via {source}. {r.get('title') or ''}".strip(),
            run_id=run_id, context={"lead_id": lead_id, "source": source, "qualification": verdict.as_dict()},
            source_url=r.get("website") or r.get("linkedin_url") or None,
            dedupe_key=f"prospect:{lead_id}",
        )
        if aid:
            created += 1
    return {"found": found, "contactable": contactable, "qualified": qualified, "created": created}


def lead_value(sig: dict[str, Any]) -> int:
    """How much revenue is visibly leaking here — spend first, then waste.

    The target is a business already buying traffic whose traffic is visibly failing to convert.
    Both halves are required, so the score is gated on spend rather than summed with it: a site
    with no ad tag scores 0 no matter how many SEO defects it has, because there is no spend to
    rescue and nothing to sell against. A big spender with a clean funnel also scores low, because
    there is nothing wrong to fix.

    Spend is inferred only from tags that cost money to install: an `AW-` conversion id exists
    only on an account running Google Ads, and a Meta Pixel only on one running Meta. Waste is
    inferred from the gap between paying for a click and being able to receive it — no booking
    path, no way to call from a phone, no measurement to tell them it is not working.
    """
    spend = (3 if sig.get("google_ads_tag") else 0) + (2 if sig.get("meta_pixel") else 0) + (1 if sig.get("gtm") else 0)
    if not spend:
        return 0
    waste = ((3 if not (sig.get("booking_link") or sig.get("booking_url")) else 0)
             + (2 if not sig.get("ga4") else 0)
             + (2 if sig.get("phone_text") and not sig.get("tel_link") else 0)
             + (1 if not sig.get("local_schema") else 0))
    return spend + waste


class DiscoverWorker:
    name = "discover"
    description = "Find prospects (OpenOutreach via process boundary, or CSV drops in data/exports/) and qualify them: business email + website + real company."
    upstream = "eracle/OpenOutreach (process boundary), ai-sales-agent lead model"

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult:
        dcfg = ctx.config.get("discover") or {}
        goal = int(dcfg.get("goal", 5))
        osm_rows: list[dict[str, Any]] = []
        osm_stats: dict[str, int] = {}
        if dcfg.get("osm"):
            from .sources import osm_leads

            osm_rows, osm_stats = osm_leads(dcfg["osm"])
        oo_rows = fetch_openoutreach(goal) if openoutreach_command() else []
        csv_rows = read_csv_drops(ws)
        o = ingest(store, osm_rows, run_id, "osm")
        a = ingest(store, oo_rows, run_id, "openoutreach")
        b = ingest(store, csv_rows, run_id, "csv")
        batch = {k: o[k] + a[k] + b[k] for k in a}
        funnel = store.lead_funnel()
        note = None if (openoutreach_command() or dcfg.get("osm")) else "no lead source connected: set discover.osm in revenueos.yaml, drop a leads*.csv in data/exports/, or install OpenOutreach"
        return WorkerResult(
            ok=True,
            summary=(f"{batch['found']} lead(s) read: {batch['contactable']} contactable, {batch['qualified']} qualified, "
                     f"{batch['created']} new. All leads: {funnel['found']} found · {funnel['contactable']} contactable · {funnel['qualified']} qualified."),
            actions_created=batch["created"],
            details={"openoutreach_rows": len(oo_rows), "csv_rows": len(csv_rows), "osm": osm_stats, "batch": batch, "funnel": funnel,
                     **({"note": note} if note and not osm_rows else {})},
        )
