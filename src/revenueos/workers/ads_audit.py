"""ADS AUDIT — the measurement brain (claude-ads deterministic core).

Input: CSV exports dropped in data/exports/ads-<platform>.csv in claude-ads' generic
13-column format (date, account_id, account_name, campaign_id, campaign_name,
campaign_status, creative_id, creative_name, conversion_action, conversions, budget, spend,
currency).  The vendored `GenericCSVExportAdapter` validates and normalises the export
into an AccountSnapshot; this worker then runs the deterministic checks that need no LLM:

  * ad_waste            — enabled campaigns with spend and zero conversions in the window
  * campaign_attention  — spend concentration (one campaign > 60 % of spend) and
                          campaigns pacing more than 25 % over their daily budget

When findings JSON exists at data/exports/ads-<platform>.findings.json (produced by the
claude-ads audit skills: 250+ prose controls run by Claude), the vendored weighted scorer
`score_account` and report renderer produce data/reports/ads-<platform>.md.  The shipped
scoring profiles are all `disabled` upstream, so a health score requires enabling one in
vendor/claude_ads_data/control-plane/manifests/scoring-profiles.json.
"""
from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..context import BusinessContext
from ..llm import LLM
from ..paths import Workspace
from ..store import Store
from ..vendor.claude_ads_core import ContractError, GenericCSVExportAdapter, ScoringError, score_account
from ..vendor.claude_ads_core.adapters import AdapterError
from ..vendor.claude_ads_core.reporting import render_markdown
from . import WorkerResult

PLATFORMS = ("google", "meta", "youtube", "linkedin", "tiktok", "microsoft", "apple", "amazon", "reddit", "pinterest", "snapchat", "x")
SKILL_FOR = {"google": "ads/ads-google", "meta": "ads/ads-meta", "youtube": "ads/ads-youtube",
             "linkedin": "ads/ads-linkedin", "tiktok": "ads/ads-tiktok", "microsoft": "ads/ads-microsoft"}


def _d(v: str | None) -> Decimal:
    try:
        return Decimal((v or "0").replace(",", "")) if v not in (None, "") else Decimal(0)
    except Exception:
        return Decimal(0)


def aggregate(path: Path) -> dict[str, dict[str, Any]]:
    """Per-campaign spend / conversions / budget-days from the (already validated) export."""
    camps: dict[str, dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            cid = row["campaign_id"]
            c = camps.setdefault(cid, {"campaign_id": cid, "name": row.get("campaign_name"), "status": (row.get("campaign_status") or "").lower(),
                                       "spend": Decimal(0), "conversions": Decimal(0), "budget_by_day": {}})
            c["spend"] += _d(row.get("spend"))
            c["conversions"] += _d(row.get("conversions"))
            if row.get("budget"):
                c["budget_by_day"][row["date"]] = _d(row.get("budget"))
    for c in camps.values():
        days = c.pop("budget_by_day")
        c["days"] = len(days) or 1
        c["daily_budget"] = (sum(days.values(), Decimal(0)) / len(days)) if days else None
    return camps


def deterministic_findings(camps: dict[str, dict[str, Any]], currency: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    total = sum((c["spend"] for c in camps.values()), Decimal(0))
    for c in camps.values():
        if c["spend"] > 0 and c["conversions"] == 0 and c["status"] in ("enabled", "active", "running", ""):
            findings.append({"kind": "ad_waste", "campaign": c, "why": f"{c['spend']:.2f} {currency} spent over {c['days']} day(s) with zero conversions."})
        if c["daily_budget"] and c["daily_budget"] > 0:
            pace = c["spend"] / c["days"] / c["daily_budget"]
            if pace > Decimal("1.25"):
                findings.append({"kind": "overpacing", "campaign": c, "why": f"Spending {pace:.0%} of daily budget ({c['spend'] / c['days']:.2f}/day vs {c['daily_budget']:.2f})."})
        if total > 0 and len(camps) > 1 and c["spend"] / total > Decimal("0.6"):
            findings.append({"kind": "concentration", "campaign": c, "why": f"{c['spend'] / total:.0%} of all spend sits in this one campaign."})
    return findings


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def run_manifest(ctx: BusinessContext, platform: str, run_id: int, source: str) -> dict[str, Any] | None:
    """claude-ads refuses to render a report over non-public data unless the operator attests
    encryption at rest / in transit with evidence.  RevenueOS will not fabricate that: it must be
    declared in revenueos.yaml under `ads.data_lifecycle` (see CLAUDE.md).  Returns None otherwise."""
    dl = (ctx.config.get("ads") or {}).get("data_lifecycle") or {}
    evidence = list(dl.get("encryption_evidence") or [])
    if not (dl.get("attested") and evidence):
        return None
    started = datetime.now(UTC).isoformat(timespec="seconds")
    delete_after = dl.get("delete_after") or (datetime.now(UTC) + timedelta(days=int(dl.get("retention_days", 90)))).isoformat(timespec="seconds")
    return {
        "schema_version": "1.0.0", "run_id": f"revenueos-{run_id}-{platform}", "started_at": started, "scopes": [platform],
        "adapters": [{"platform": platform, "mode": "export"}], "sources": [source], "privacy_class": "internal",
        "data_lifecycle": {
            "schema_version": "1.0.0", "lifecycle_id": f"revenueos-exports-{platform}", "classification": "internal",
            "retention": {"minimum_seconds": 0, "mode": "operator-defined", "delete_after": delete_after,
                          "purpose": "ad account audit", "exception_reason": None},
            "encryption": {"at_rest": "verified", "in_transit": "verified", "evidence_refs": evidence},
            "access": {"owner": dl.get("owner") or ctx.company_name, "authorized_roles": ["operator"], "access_log_locator": None},
            "deletion": {"status": "scheduled", "method": "delete data/exports and data/reports files", "verification_required": True,
                         "verification_artifact_locator": None},
            "incident": {"owner": dl.get("owner") or ctx.company_name, "reporting_channel": dl.get("incident_channel") or "email", "status": "not-triggered", "record_locator": None},
        },
        "worker_status": {"ads-audit": "completed"}, "completeness": "complete",
    }


def local_report(platform: str, snapshot: dict[str, Any], findings: list[dict[str, Any]], scoring: dict[str, Any]) -> str:
    lines = [f"# Ads audit — {platform}", "", f"Window {snapshot['window']['start']} → {snapshot['window']['end']}, spend {snapshot.get('spend')} {snapshot.get('currency')}.",
             "", f"Health score: **{scoring.get('health_score')}** ({scoring.get('status')}, evidence coverage {scoring.get('evidence_coverage')})", "", "## Findings", ""]
    for f in findings:
        lines.append(f"- **{f['control_id']}** {f['status']} ({f['confidence']}): {f['observation']} → {f['recommendation']}")
    lines += ["", "_Rendered by RevenueOS. The full claude-ads report (with redaction and lifecycle attestation) is produced once `ads.data_lifecycle.attested` is set in revenueos.yaml._"]
    return "\n".join(lines) + "\n"


class AdsAuditWorker:
    name = "ads-audit"
    description = "Ingest ad exports, flag wasted spend and pacing problems, score + report when findings exist."
    upstream = "AgriciDaniel/claude-ads claude_ads_core (adapters, scoring, reporting)"

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult:
        exports = sorted(ws.exports.glob("ads-*.csv"))
        if not exports:
            return WorkerResult(ok=True, summary="No ad exports found in data/exports/ (expected ads-<platform>.csv).", actions_created=0)
        created, audited, errors = 0, [], []
        for path in exports:
            platform = path.stem.split("-", 1)[1].lower()
            if platform not in PLATFORMS:
                errors.append(f"{path.name}: unknown platform {platform!r}")
                continue
            try:
                snapshot = GenericCSVExportAdapter(platform).read_snapshot(str(path))
            except (AdapterError, ContractError) as exc:
                errors.append(f"{path.name}: {exc}")
                continue
            currency = snapshot.get("currency", "")
            camps = aggregate(path)
            for f in deterministic_findings(camps, currency):
                c = f["campaign"]
                atype = "ad_waste" if f["kind"] == "ad_waste" else "campaign_attention"
                aid = store.create_action(
                    atype, f"{platform.title()} ads: {c['name'] or c['campaign_id']} — {f['kind'].replace('_', ' ')}", f["why"],
                    run_id=run_id, dedupe_key=f"ads:{platform}:{c['campaign_id']}:{f['kind']}:{snapshot['window']['end']}",
                    context={"platform": platform, "campaign_id": c["campaign_id"], "kind": f["kind"],
                             "before": {"spend": float(c["spend"]), "conversions": float(c["conversions"]), "days": c["days"],
                                        "window_end": snapshot["window"]["end"]},
                             "skill": SKILL_FOR.get(platform, "ads/ads-audit"), "executor": "run_skill",
                             "skill_input": f"Campaign {c['name']} ({c['campaign_id']}): {f['why']} Spend {c['spend']}, conversions {c['conversions']}."},
                )
                created += 1 if aid else 0
            store.record_metric("ad_spend", float(snapshot.get("spend") or 0), platform=platform, window_end=snapshot["window"]["end"])

            findings_path = path.with_suffix(".findings.json")
            score_note = ""
            if findings_path.exists():
                try:
                    bundle_in = _load_json(findings_path)
                    result = score_account(bundle_in["control_definitions"], bundle_in["findings"], bundle_in["category_weights"])
                    manifest = bundle_in.get("run_manifest") or run_manifest(ctx, platform, run_id, path.name)
                    report = ws.reports / f"ads-{platform}.md"
                    if manifest:
                        bundle = {"schema_version": "1.0.0", "run_manifest": manifest, "account_snapshot": snapshot,
                                  "control_definitions": bundle_in["control_definitions"], "findings": bundle_in["findings"],
                                  "scoring": result.to_dict()}
                        report.write_text(render_markdown(bundle), encoding="utf-8")
                    else:
                        report.write_text(local_report(platform, snapshot, bundle_in["findings"], result.to_dict()), encoding="utf-8")
                    store.record_metric("ad_health_score", float(result.health_score or 0), platform=platform)
                    score_note = f", health {result.health_score} ({result.status}) → {report.relative_to(ws.root)}"
                except (KeyError, ScoringError, ContractError, ValueError) as exc:
                    errors.append(f"{findings_path.name}: {exc}")
            audited.append(f"{platform} ({len(camps)} campaigns{score_note})")
        summary = f"{created} ad action(s); audited {', '.join(audited) or 'nothing'}."
        if errors:
            summary += " Errors: " + "; ".join(errors)
        return WorkerResult(ok=not errors or bool(audited), summary=summary, actions_created=created,
                            details={"audited": audited, "errors": errors})
