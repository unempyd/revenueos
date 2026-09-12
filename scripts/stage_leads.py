#!/usr/bin/env python3
"""Stage connector output as a lead sheet for `revenueos run discover`.

Reads the CSV that tools/clis/github-prospects.js writes (login, name, company, email, blog,
location, bio, twitter_username, public_repos, followers, created_at, html_url) — or any CSV
with those columns — and writes the lead-sheet columns discover ingests
(email, first_name, last_name, company, title, website, linkedin_url, reason, lead_id).

It never writes `qualified_at`. Staging describes where a row came from; only the
qualification gate (revenueos.qualify) decides whether a lead is qualified, and it reports
found / contactable / qualified separately. The `reason` written here is the raw signal
("forked <repo>"), which is weak on its own — say so, do not dress it up.

    node tools/clis/github-prospects.js forks owner/repo --enrich --format csv > data/exports/github-forks.csv
    python scripts/stage_leads.py data/exports/github-forks.csv --signal "forked owner/repo" > data/exports/leads-github.csv
"""
from __future__ import annotations

import argparse
import csv
import sys

OUT_COLUMNS = ("email", "first_name", "last_name", "company", "title", "website", "linkedin_url", "reason", "lead_id")


def stage(rows: list[dict[str, str]], signal: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for r in rows:
        login = (r.get("login") or r.get("lead_id") or "").removeprefix("gh:").strip()
        company = (r.get("company") or "").strip()
        if not login and not (r.get("email") or company):
            continue
        name = (r.get("name") or " ".join(x for x in (r.get("first_name"), r.get("last_name")) if x) or "").split()
        out.append({
            "email": (r.get("email") or "").strip(),
            "first_name": name[0] if name else "", "last_name": " ".join(name[1:]),
            "company": company, "title": (r.get("bio") or r.get("title") or "")[:80].replace("\n", " "),
            "website": (r.get("blog") or r.get("website") or "").strip(),
            "linkedin_url": (r.get("html_url") or r.get("linkedin_url") or "").strip(),
            "reason": (f"{signal}; profile lists company {company}" if company else signal).strip("; "),
            "lead_id": f"gh:{login}" if login else (r.get("email") or company),
        })
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("csv", help="connector CSV (github-prospects.js output) or an existing lead sheet")
    ap.add_argument("--signal", default="listed on a GitHub profile", help="what the row is evidence of (written to `reason`)")
    a = ap.parse_args(argv)
    with open(a.csv, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    staged = stage(rows, a.signal)
    w = csv.DictWriter(sys.stdout, fieldnames=list(OUT_COLUMNS))
    w.writeheader()
    w.writerows(staged)
    with_email = sum(1 for r in staged if r["email"])
    print(f"staged {len(staged)} row(s); {with_email} with an email; qualification is decided by `revenueos run discover`", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
