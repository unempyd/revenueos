"""DISCOVER — find qualified prospects.

Sources, in order:
  1. OpenOutreach (GPL-3.0, eracle/OpenOutreach) across a process boundary: if the
     `openoutreach` CLI is installed (pip) or the container is up (services/openoutreach),
     run `openoutreach find N --json` and ingest its JSON-Lines.  Nothing from that repo is
     linked into this process; stdout is the whole contract, exactly as its SKILL.md states.
  2. CSV drops in data/exports/leads*.csv using the OpenOutreach / Instantly / Smartlead
     column names (email, first_name, last_name, company, title, website, linkedin_url, reason).
Every new lead becomes one `prospect` action.
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


def ingest(store: Store, rows: list[dict[str, Any]], run_id: int, default_source: str) -> int:
    created = 0
    for r in rows:
        source = r.get("_source", default_source)
        source_id = r.get("lead_id") or r.get("email") or r.get("linkedin_url") or r.get("website")
        company = r.get("company") or r.get("business_name") or r.get("email", "").split("@")[-1]
        if not company:
            continue
        lead_id = store.upsert_lead(
            source, source_id, company,
            first_name=r.get("first_name"), last_name=r.get("last_name"), title=r.get("title"),
            website_url=r.get("website"), linkedin_url=r.get("linkedin_url"),
            contact_email=(r.get("email") or None), reason=r.get("reason"),
        )
        who = " ".join(x for x in (r.get("first_name"), r.get("last_name")) if x) or r.get("email") or company
        title = f"{who} at {company}" if who != company else company
        aid = store.create_action(
            "prospect", f"Qualified prospect: {title}",
            r.get("reason") or f"Found via {source}. {r.get('title') or ''}".strip(),
            run_id=run_id, context={"lead_id": lead_id, "source": source},
            source_url=r.get("linkedin_url") or r.get("website") or None,
            dedupe_key=f"prospect:{lead_id}",
        )
        if aid:
            created += 1
    return created


class DiscoverWorker:
    name = "discover"
    description = "Find qualified prospects (OpenOutreach via process boundary, or CSV drops in data/exports/)."
    upstream = "eracle/OpenOutreach (process boundary), ai-sales-agent lead model"

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult:
        goal = int((ctx.config.get("discover") or {}).get("goal", 5))
        oo_rows = fetch_openoutreach(goal) if openoutreach_command() else []
        csv_rows = read_csv_drops(ws)
        created = ingest(store, oo_rows, run_id, "openoutreach") + ingest(store, csv_rows, run_id, "csv")
        note = "" if openoutreach_command() else " OpenOutreach not installed (pip install openoutreach, or docker compose --profile outreach up)."
        return WorkerResult(
            ok=True,
            summary=f"{created} new prospect(s) from {len(oo_rows)} OpenOutreach rows and {len(csv_rows)} CSV rows.{note}",
            actions_created=created,
            details={"openoutreach_rows": len(oo_rows), "csv_rows": len(csv_rows)},
        )
