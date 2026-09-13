"""The full control audit: the vendored catalogue, evidence-only verdicts, failures as actions, re-audit as measurement."""
from __future__ import annotations

import json

from revenueos.workers import ads_controls, ads_terms, run_worker
from revenueos.workers.measure import measure_control, measure_search_terms

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
    assert llm.calls == 5  # 97 controls in batches of 24; every batch answered, so no batch is split and retried
    assert res["not_evaluated"] == 93  # the controls this fake never returns: named as not evaluated, not as missing account data
    acts = store.list_actions("pending", "campaign_attention")
    assert len(acts) == 1 and acts[0]["title"] == "Google ads: Zero-conversion keyword investigation — failing"
    assert acts[0]["context"]["control_id"] == "G-WS1" and acts[0]["context"]["skill"] == "ads/ads-google"
    saved = json.loads((workspace.exports / "ads-google.controls.json").read_text())
    by = {f["control_id"]: f for f in saved["findings"]}
    assert by["G01"]["status"] == "unknown" and by["G02"]["status"] == "unknown" and by["G09"]["status"] == "pass"
    report = (workspace.reports / "ads-google-controls.md").read_text()
    assert "97 controls evaluated: 1 fail" in report and "No health score" in report
    assert "2 unknown (evidence not in the data)" in report and "93 not evaluated (no usable model verdict" in report
    assert "- **G03**" not in report  # a control the model never answered is not listed as an evidence gap
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


# ── keyword / search-term evidence ───────────────────────────────────────────
KEYWORDS_EXPORT = """Search keyword report
Sep 1, 2026 - Sep 13, 2026
Keyword status,Keyword,Match type,Campaign,Ad group,Clicks,Impr.,Cost,Conversions,Quality Score
Enabled,generic dentist,Broad match,Generic Dental,Core,"1,200","45,000","1,230.50",0,3
Enabled,dentist near me,Phrase match,Generic Dental,Core,400,9000,310.00,4,7
Enabled,dental implants brand,Exact match,Brand,Brand,0,0,0.00,0,--
Total: All keywords,,,,,"1,600","54,000","1,540.50",4,
"""
TERMS_EXPORT = """Search terms report
Sep 1, 2026 - Sep 13, 2026
Search term,Match type,Added/Excluded,Campaign,Ad group,Clicks,Impr.,Cost,Conversions
dentist salary,Broad match,None,Generic Dental,Core,300,8000,210.00,0
dental assistant jobs,Broad match,None,Generic Dental,Core,120,3000,95.00,0
dentist near me,Phrase match (close variant),Added,Generic Dental,Core,400,9000,310.00,4
free dental,Broad match,Excluded,Generic Dental,Core,50,900,40.00,0
cheap crown,Broad match,None,Generic Dental,Core,12,400,9.00,0
Total: All search terms,,,,,882,"21,300",664.00,4
"""


def test_google_ui_exports_are_read_with_their_title_and_total_rows(tmp_path):
    kw = tmp_path / "ads-google.keywords.csv"
    kw.write_text(KEYWORDS_EXPORT)
    rows = ads_terms.read_keywords_csv(kw)
    assert [r["text"] for r in rows] == ["generic dentist", "dentist near me", "dental implants brand"]
    assert rows[0] == {"keyword_id": None, "text": "generic dentist", "match_type": "broad", "status": "enabled", "quality_score": 3, "campaign_id": None,
                       "campaign_name": "Generic Dental", "ad_group_id": None, "ad_group_name": "Core", "cost": 1230.5, "clicks": 1200, "impressions": 45000, "conversions": 0.0}
    assert rows[2]["quality_score"] is None and rows[2]["impressions"] == 0
    st = tmp_path / "ads-google.search-terms.csv"
    st.write_bytes(("\ufeff" + TERMS_EXPORT).encode("utf-16"))  # the older UTF-16 download, tab or comma
    terms = ads_terms.read_search_terms_csv(st)
    assert {t["text"]: t["status"] for t in terms} == {"dentist salary": "none", "dental assistant jobs": "none", "dentist near me": "added", "free dental": "excluded", "cheap crown": "none"}
    assert next(t for t in terms if t["text"] == "dentist near me")["match_type"] == "phrase"
    waste = ads_terms.search_term_waste(terms, 20.0)
    assert [t["text"] for t in waste] == ["dentist salary", "dental assistant jobs"]  # excluded and converting terms are not waste; 9.00 is under the floor


def test_attach_bounds_rows_and_reports_coverage():
    snap = ads_controls.snapshot_from_campaigns("google", "123", "USD", CAMPS)
    kws = [{"keyword_id": str(i), "text": f"kw {i}", "match_type": "broad", "status": "enabled", "quality_score": 2 if i % 2 else 8, "campaign_id": None,
            "campaign_name": "brand", "ad_group_id": None, "ad_group_name": "g", "cost": float(i), "clicks": i, "impressions": 0 if i < 3 else i * 10, "conversions": 0.0} for i in range(200)]
    terms = [{"text": f"term {i}", "match_type": "broad", "status": "none", "campaign_id": None, "campaign_name": "Balayage", "ad_group_id": None, "ad_group_name": "g",
              "cost": float(i), "clicks": i, "impressions": i, "conversions": 0.0} for i in range(10)]
    negs = {"campaign": [{"text": "free", "match_type": "broad", "campaign_id": "11", "campaign_name": "Brand"}], "lists": [{"list_id": "1", "name": "L", "keywords": []}],
            "list_campaigns": [{"list_id": "1", "campaign_id": "11"}, {"list_id": "1", "campaign_id": "12"}]}
    ads_terms.attach(snap, keywords=kws, search_terms=terms, negatives=negs)
    cov = snap["evidence_coverage"]
    assert len(snap["keywords"]) == 150 and snap["keywords"][0]["text"] == "kw 199"  # largest spenders first
    assert cov["keywords_total"] == 200 and cov["keywords_listed"] == 150 and cov["keywords_zero_impressions"] == 3 and cov["keywords_quality_score_le_3"] == 100
    assert cov["search_terms_not_added_or_excluded"] == 10 and cov["negatives_campaign_level"] == 1 and cov["negative_list_attachments"] == 2
    assert snap["keywords"][0]["campaign_id"] == "11" and snap["search_terms"][0]["campaign_id"] == "12"  # names resolved to the snapshot's ids
    text = ads_terms.bounded_json(snap, 12000)
    small = json.loads(text)  # valid JSON, never a mid-document slice
    assert len(text) <= 12000 and len(small["keywords"]) < 150 and small["evidence_coverage"]["keywords_listed"] == len(small["keywords"])
    assert small["evidence_coverage"]["keywords_total"] == 200


def test_ads_audit_uses_sidecar_exports_and_queues_search_term_waste(workspace, store, onboarded):
    from tests.test_workers import ADS_CSV

    (workspace.exports / "ads-google.csv").write_text(ADS_CSV)
    (workspace.exports / "ads-google.keywords.csv").write_text(KEYWORDS_EXPORT)
    (workspace.exports / "ads-google.search-terms.csv").write_text(TERMS_EXPORT)
    r = run_worker("ads-audit", workspace, store, onboarded, None)
    assert r.ok and "3 keywords, 5 search terms" in r.summary and not r.details["errors"], r
    acts = [a for a in store.list_actions("pending", "campaign_attention") if a["context"]["kind"] == "search_term_waste"]
    assert len(acts) == 1
    a = acts[0]
    assert a["title"] == "Google ads: 2 search terms spent 305.00 USD with no conversions and no exclusion"
    assert "'dentist salary' (broad, Generic Dental)" in a["content"] and "free dental" not in a["content"]
    assert a["context"]["before"]["terms"][0]["campaign_id"] == "c2"  # export names resolved to the snapshot's campaign ids
    assert a["context"]["before"]["spend"] == 305.0 and a["context"]["skill"] == "ads/ads-google" and a["context"]["executor"] == "run_skill"
    rec = json.loads((workspace.exports / "ads-google.terms.json").read_text())
    assert rec["window"]["end"] == "2026-09-02" and len(rec["search_terms"]) == 5 and rec["source"] == "ads-google.csv"
    assert run_worker("ads-audit", workspace, store, onboarded, None).actions_created == 0  # idempotent per window
    # measured by the next read of the same terms
    status, data = measure_search_terms(workspace, a)
    assert status == "pending" and "no newer" in data["note"]
    rec["window"]["end"] = "2026-09-30"
    for t in rec["search_terms"]:
        if t["text"] == "dentist salary":
            t["status"] = "excluded"
        if t["text"] == "dental assistant jobs":
            t["cost"] = 95.0  # still spending
    (workspace.exports / "ads-google.terms.json").write_text(json.dumps(rec))
    status, data = measure_search_terms(workspace, a)
    assert status == "measured" and data["metric"] == "wasted_search_term_spend" and data["before_value"] == 305.0 and data["after_value"] == 95.0
    assert data["after"]["excluded"] == 1 and "1 of 2 terms now excluded" in data["note"]
    rec["search_terms"] = [t for t in rec["search_terms"]]
    for t in rec["search_terms"]:
        t["status"] = "none" if t["text"] == "dentist salary" else t["status"]
        t["cost"] = 210.0 if t["text"] == "dentist salary" else t["cost"]
    (workspace.exports / "ads-google.terms.json").write_text(json.dumps(rec))
    assert measure_search_terms(workspace, a)[0] == "no_effect"


def test_ads_live_reads_term_evidence_and_survives_a_failed_read(workspace, store, onboarded, monkeypatch):
    from revenueos.connections import Connection, ConnectionStore, google
    from revenueos.workers.live import AdsLiveWorker

    cs = ConnectionStore(workspace)
    cs.put(Connection(provider="google", account="o@b.example", scopes=["adwords"], secrets={"tokens": {}}, meta={"ads_customer_id": "123"}))
    terms = [{"text": "dentist salary", "match_type": "broad", "status": "none", "campaign_id": "11", "campaign_name": "Brand", "ad_group_id": "5", "ad_group_name": "Core",
              "cost": 210.0, "clicks": 300, "impressions": 8000, "conversions": 0.0}]
    monkeypatch.setattr(google, "ads_campaigns", lambda cs_: CAMPS)
    monkeypatch.setattr(google, "ads_keywords", lambda cs_: [{"keyword_id": "1", "text": "hair salon", "match_type": "broad", "status": "enabled", "quality_score": 3,
                                                              "campaign_id": "11", "campaign_name": "Brand", "ad_group_id": "5", "ad_group_name": "Core", "cost": 120.0,
                                                              "clicks": 240, "impressions": 8000, "conversions": 0.0}])
    monkeypatch.setattr(google, "ads_search_terms", lambda cs_: terms)

    def boom(cs_):
        raise RuntimeError("Google Ads campaign negatives: 400 unsupported field")

    monkeypatch.setattr(google, "ads_negative_keywords", boom)
    seen = {}
    monkeypatch.setattr(ads_controls, "audit", lambda ws, st, ctx, llm, run_id, platform, snap, source: seen.update(snap=snap) or {"note": "no model"})
    r = AdsLiveWorker().run(workspace, store, onboarded, None, store.start_run("ads-live"))
    assert r.ok and "1 keywords, 1 search terms" in r.summary and "not read — negatives: RuntimeError" in r.summary, r.summary
    assert seen["snap"]["evidence_coverage"]["search_term_spend_not_added_or_excluded"] == 210.0 and "negatives" not in seen["snap"]
    acts = store.list_actions("pending", "campaign_attention")
    assert len(acts) == 1 and acts[0]["context"]["kind"] == "search_term_waste" and acts[0]["context"]["before"]["terms"][0]["text"] == "dentist salary"
    assert json.loads((workspace.exports / "ads-google.terms.json").read_text())["source"] == "google-ads-api"
    assert "ad_waste" in {a["action_type"] for a in store.list_actions()}  # Brand still spends 150 with 0 conversions → the existing pause action


class TruncatingLLM:
    """The failure the 2026-09-13 re-run hit: a batch of verdicts running past the token ceiling, so the
    array never closes. Whole batches were filed as `unknown` — a claim about the account that was never made."""

    model = "fake"

    def __init__(self, cut_after=2, fail_first=False):
        self.calls = 0
        self.cut_after = cut_after
        self.fail_first = fail_first

    def complete_sync(self, messages, *, temperature=None, max_tokens=4096):
        self.calls += 1
        ids = [ln.split(":")[0].strip("- ").strip() for ln in messages[-1].content.split("=== CONTROLS TO EVALUATE ===")[1].strip().splitlines()]
        if self.fail_first and self.calls == 1:
            raise RuntimeError("overloaded")
        body = ",".join(json.dumps({"schema_version": "1.0.0", "control_id": cid, "status": "pass", "evidence": [{"campaign_id": "11", "field": "spend", "value": 150.0}],
                                    "confidence": "medium", "observation": f"verdict for {cid}", "diagnosis": "", "recommendation": ""})
                        for cid in ids[: self.cut_after])
        return "Here are the findings:\n[" + body + ',\n{"schema_version": "1.0.0", "control_id": "' + (ids[self.cut_after] if len(ids) > self.cut_after else "GX")


def test_a_truncated_batch_keeps_its_verdicts_and_names_the_rest_not_evaluated(workspace, store, onboarded):
    llm = TruncatingLLM(cut_after=2)
    snap = ads_controls.snapshot_from_campaigns("google", "123", "USD", CAMPS)
    res = ads_controls.audit(workspace, store, onboarded, llm, store.start_run("ads-live"), "google", snap, "test")
    assert res["checked"] == 97 and res["pass"] > 0  # the complete objects before the cut survive
    saved = {f["control_id"]: f for f in json.loads((workspace.exports / "ads-google.controls.json").read_text())["findings"]}
    assert saved["G01"]["status"] == "pass" and saved["G01"]["observation"] == "verdict for G01"
    assert saved["G03"]["status"] == "unknown" and saved["G03"]["diagnosis"] == ads_controls.NOT_EVALUATED
    assert res["not_evaluated"] == res["unknown"] and res["pass"] + res["not_evaluated"] == 97
    report = (workspace.reports / "ads-google-controls.md").read_text()
    assert "0 unknown (evidence not in the data)" in report and f"{res['not_evaluated']} not evaluated" in report


def test_an_unusable_batch_is_split_and_retried_and_a_raising_call_does_not_lose_the_run(workspace, store, onboarded):
    class Unusable:
        model = "fake"

        def __init__(self):
            self.calls = 0
            self.sizes = []

        def complete_sync(self, messages, *, temperature=None, max_tokens=4096):
            self.calls += 1
            ids = [ln.split(":")[0].strip("- ").strip() for ln in messages[-1].content.split("=== CONTROLS TO EVALUATE ===")[1].strip().splitlines()]
            self.sizes.append(len(ids))
            if self.calls == 1:
                raise RuntimeError("overloaded")
            if len(ids) > 12:
                return "I cannot produce JSON for this batch."
            return json.dumps([{"schema_version": "1.0.0", "control_id": cid, "status": "unknown", "evidence": [], "confidence": "none",
                                "observation": "", "diagnosis": f"needs ad group data for {cid}", "recommendation": ""} for cid in ids])

    llm = Unusable()
    snap = ads_controls.snapshot_from_campaigns("google", "123", "USD", CAMPS)
    res = ads_controls.audit(workspace, store, onboarded, llm, store.start_run("ads-live"), "google", snap, "test")
    assert llm.sizes[:3] == [24, 12, 12]  # the raising call and the unusable ones are split once and asked again
    assert res["checked"] == 97 and res["not_evaluated"] == 0  # every control ends with a real verdict
    saved = {f["control_id"]: f for f in json.loads((workspace.exports / "ads-google.controls.json").read_text())["findings"]}
    assert saved["G01"]["diagnosis"] == "needs ad group data for G01"
    assert store.latest_metrics()["ads_controls_not_evaluated"] == 0.0
