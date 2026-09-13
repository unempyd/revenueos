"""The full control audit: the vendored catalogue, evidence-only verdicts, failures as actions, re-audit as measurement."""
from __future__ import annotations

import json

from revenueos.workers import ads_controls, run_worker
from revenueos.workers.measure import measure_control

CAMPS = [
    {"id": "11", "name": "Brand", "status": "ENABLED", "daily_budget": 20.0, "cost": 150.0, "conversions": 0.0, "clicks": 300, "impressions": 9000},
    {"id": "12", "name": "Balayage", "status": "ENABLED", "daily_budget": 10.0, "cost": 90.0, "conversions": 6.0, "clicks": 200, "impressions": 4000},
]


class FakeLLM:
    model = "fake"

    def __init__(self):
        self.calls = 0

    def complete_sync(self, messages, *, temperature=None, max_tokens=4096):
        self.calls += 1
        user = messages[-1].content
        ids = [ln.split(":")[0].strip("- ").strip() for ln in user.split("=== CONTROLS TO EVALUATE ===")[1].strip().splitlines()]
        out = []
        for cid in ids:
            if cid == "G09":  # budget pacing: Brand spends 150 over 28 days on a 20/day budget → fine; call it pass with evidence
                out.append({"schema_version": "1.0.0", "control_id": cid, "status": "pass", "evidence": [{"campaign_id": "11", "field": "spend", "value": 150.0}],
                            "confidence": "high", "observation": "Spend is within budget.", "diagnosis": "", "recommendation": ""})
            elif cid == "G-WS1":  # zero-conversion investigation: Brand has 300 clicks, 0 conversions
                out.append({"schema_version": "1.0.0", "control_id": cid, "status": "fail", "evidence": [{"campaign_id": "11", "field": "conversions", "value": 0}],
                            "confidence": "high", "observation": "Brand spent 150.00 for 300 clicks and no conversions.", "diagnosis": "Traffic converts nowhere.",
                            "recommendation": "Review search terms and the landing page before spending more."})
            elif cid == "G01":  # a verdict without evidence must be downgraded
                out.append({"schema_version": "1.0.0", "control_id": cid, "status": "fail", "evidence": [], "confidence": "high", "observation": "names look messy",
                            "diagnosis": "", "recommendation": "rename"})
            elif cid == "G02":  # garbage
                out.append({"control_id": cid, "status": "maybe"})
            # every other control: omitted → unknown
        return json.dumps(out)


def test_catalogue_counts():
    assert len(ads_controls.controls_for("google")) == 97 and len(ads_controls.controls_for("meta")) == 72
    assert ads_controls.controls_for("google")[0]["intent"] == "Campaign naming convention"


def test_snapshot_from_live_campaigns_validates():
    snap = ads_controls.snapshot_from_campaigns("google", "123-456-7890", "aud", CAMPS)
    assert snap["currency"] == "AUD" and snap["spend"] == 240.0 and len(snap["campaigns"]) == 2 and snap["budgets"][0]["daily_budget"] == 20.0


def test_evaluate_and_audit_create_actions_only_for_evidenced_failures(workspace, store, onboarded):
    llm = FakeLLM()
    snap = ads_controls.snapshot_from_campaigns("google", "123", "USD", CAMPS)
    res = ads_controls.audit(workspace, store, onboarded, llm, store.start_run("ads-live"), "google", snap, "test")
    assert res["checked"] == 97 and res["fail"] == 1 and res["pass"] == 1 and res["unknown"] == 95 and res["created"] == 1
    assert llm.calls == 5  # 97 controls in batches of 24
    acts = store.list_actions("pending", "campaign_attention")
    assert len(acts) == 1 and acts[0]["title"] == "Google ads: Zero-conversion keyword investigation — failing"
    assert acts[0]["context"]["control_id"] == "G-WS1" and acts[0]["context"]["skill"] == "ads/ads-google"
    saved = json.loads((workspace.exports / "ads-google.controls.json").read_text())
    by = {f["control_id"]: f for f in saved["findings"]}
    assert by["G01"]["status"] == "unknown" and by["G02"]["status"] == "unknown" and by["G09"]["status"] == "pass"
    report = (workspace.reports / "ads-google-controls.md").read_text()
    assert "97 controls evaluated: 1 fail" in report and "No health score" in report
    assert store.latest_metrics()["ads_controls_fail"] == 1.0
    # measured by the next audit: still failing → no_effect; then passing → measured
    a = acts[0]
    status, data = measure_control(workspace, a)
    assert status == "pending" and "no newer" in data["note"]
    saved["window"]["end"] = "2099-01-01"
    (workspace.exports / "ads-google.controls.json").write_text(json.dumps(saved))
    assert measure_control(workspace, a)[0] == "no_effect"
    by["G-WS1"]["status"] = "pass"
    (workspace.exports / "ads-google.controls.json").write_text(json.dumps(saved))
    status, data = measure_control(workspace, a)
    assert status == "measured" and data["metric"] == "control_pass" and data["after_value"] == 1.0


def test_ads_audit_worker_reports_controls_without_a_model(workspace, store, onboarded):
    from tests.test_workers import ADS_CSV

    (workspace.exports / "ads-google.csv").write_text(ADS_CSV)
    r = run_worker("ads-audit", workspace, store, onboarded, None)
    assert r.ok and "97 google controls available" in r.summary
