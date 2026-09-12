"""Every worker exercised end-to-end without network or an LLM credential."""
from __future__ import annotations

import json
from email.message import EmailMessage

from revenueos.workers import (
    all_workers,
    execute_action,
    outreach,
    run_worker,
    seo,
)

LEADS_CSV = """email,first_name,last_name,company,title,website,linkedin_url,reason,lead_id,qualified_at
maria@brightsmile.example,Maria,Lopez,BrightSmile Dental,Practice Manager,https://brightsmile.example,https://linkedin.com/in/maria,"3-chair clinic, posts about no-shows",L1,2026-09-10
ops@molar.example,,,Molar & Co,Owner,https://molar.example,,"6 chairs, still uses phone reminders",L2,2026-09-10
"""

ADS_CSV = """date,account_id,account_name,campaign_id,campaign_name,campaign_status,creative_id,creative_name,conversion_action,conversions,budget,spend,currency
2026-09-01,acc1,Acme,c1,Brand,enabled,cr1,Brand A,lead,3,50,40,USD
2026-09-02,acc1,Acme,c1,Brand,enabled,cr1,Brand A,lead,2,50,45,USD
2026-09-01,acc1,Acme,c2,Generic Dental,enabled,cr2,Generic A,lead,0,100,180,USD
2026-09-02,acc1,Acme,c2,Generic Dental,enabled,cr2,Generic A,lead,0,100,190,USD
2026-09-01,acc1,Acme,c3,Paused Old,paused,cr3,Old,lead,0,10,5,USD
"""


def test_worker_roster():
    names = set(all_workers())
    assert names == {"discover", "outreach", "inbox", "seo", "ads-audit", "content", "monitor", "measure", "growth"}


def test_discover_from_csv_drop(workspace, store, onboarded):
    (workspace.exports / "leads-sept.csv").write_text(LEADS_CSV)
    r = run_worker("discover", workspace, store, onboarded, None)
    assert r.ok and r.actions_created == 2, r
    assert store.counts_by_type() == {"prospect": 2}
    leads = store.list_leads()
    assert {l["business_name"] for l in leads} == {"BrightSmile Dental", "Molar & Co"}
    assert next(l for l in leads if l["business_name"] == "BrightSmile Dental")["reason"].startswith("3-chair")
    # idempotent
    r2 = run_worker("discover", workspace, store, onboarded, None)
    assert r2.ok and r2.actions_created == 0


def test_outreach_drafts_then_dry_run_send(workspace, store, onboarded, monkeypatch):
    (workspace.exports / "leads.csv").write_text(LEADS_CSV)
    run_worker("discover", workspace, store, onboarded, None)
    r = run_worker("outreach", workspace, store, onboarded, None)
    assert r.ok and r.actions_created == 2, r
    follow_ups = store.list_actions("pending", "follow_up")
    assert len(follow_ups) == 2
    a = next(f for f in follow_ups if f["context"]["to"] == "maria@brightsmile.example")
    assert "BrightSmile Dental" in a["title"] or "BrightSmile Dental" in a["content"]
    assert "3-chair clinic" in a["content"]  # the qualification reason personalises the opener
    assert "Acme Recall" in a["content"] or "cal.example" in a["content"]
    # deterministic: the same lead always gets the same subject variant
    draft = store.get_draft(a["context"]["draft_id"])
    assert outreach.pick(draft["lead_id"], 2, "subject") == outreach.pick(draft["lead_id"], 2, "subject")

    monkeypatch.setenv("REVENUEOS_DRY_RUN", "1")
    out = execute_action(workspace, store, onboarded, None, a)
    assert "dry-run" in out
    written = list(workspace.outputs.glob("email-*.txt"))
    assert len(written) == 1
    text = written[0].read_text()
    assert "reply 'stop' to unsubscribe" in text and "— Sam Rivera" in text  # CASL footer + sender from revenueos.yaml
    assert store.get_lead(draft["lead_id"])["status"] == "sent"
    assert store.daily_send_count() == 1

    # suppression list is enforced
    store.add_unsubscribe("ops@molar.example")
    b = next(f for f in follow_ups if f["context"]["to"] == "ops@molar.example")
    assert "suppression" in execute_action(workspace, store, onboarded, None, b)

    # daily cap is enforced
    onboarded.config.setdefault("outreach", {})["daily_cap"] = 1
    store.add_unsubscribe("nobody@example.com")
    c_id = store.create_action("follow_up", "x", "y", context={"executor": "send_email", "draft_id": draft["id"], "lead_id": draft["lead_id"], "to": "third@example.com"})
    assert "daily cap" in execute_action(workspace, store, onboarded, None, store.get_action(c_id))


def test_inbox_classifies_reply_bounce_stop(workspace, store, onboarded, monkeypatch):
    (workspace.exports / "leads.csv").write_text(LEADS_CSV)
    run_worker("discover", workspace, store, onboarded, None)
    run_worker("outreach", workspace, store, onboarded, None)
    monkeypatch.setenv("REVENUEOS_DRY_RUN", "1")
    for a in store.list_actions("pending", "follow_up"):
        execute_action(workspace, store, onboarded, None, a)
        store.set_action_status(a["id"], "executed")
    drop = workspace.exports / "inbox"
    drop.mkdir()

    def eml(name, frm, subject, body):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"], m["Message-ID"] = frm, "sam@acme-scheduling.example", subject, f"<{name}@x>"
        m.set_content(body)
        (drop / f"{name}.eml").write_bytes(bytes(m))

    eml("reply", "Maria Lopez <maria@brightsmile.example>", "Re: quick question", "Sure, Tuesday works.\n\nOn Mon, Sam wrote:\n> hello\n> there\n")
    eml("stop", "ops@molar.example", "Re: an idea", "STOP\n")
    eml("bounce", "MAILER-DAEMON@mx.example", "Undeliverable: hello", "Final-Recipient: rfc822; gone@nowhere.example\nStatus: 5.1.1\n")
    r = run_worker("inbox", workspace, store, onboarded, None)
    assert r.ok and r.details == {"replies": 1, "bounces": 1, "stops": 1}, r
    fu = store.list_actions("pending", "follow_up")
    assert len(fu) == 1 and fu[0]["content"] == "Sure, Tuesday works."  # quoted history stripped by talon
    assert store.is_unsubscribed("ops@molar.example")
    assert not list(drop.glob("*.eml"))  # consumed


def test_ads_audit_flags_waste_and_concentration(workspace, store, onboarded):
    (workspace.exports / "ads-google.csv").write_text(ADS_CSV)
    r = run_worker("ads-audit", workspace, store, onboarded, None)
    assert r.ok, r
    kinds = {(a["action_type"], a["context"]["kind"]) for a in store.list_actions()}
    assert ("ad_waste", "ad_waste") in kinds            # c2 spent 370 with 0 conversions
    assert ("campaign_attention", "concentration") in kinds  # c2 > 60 % of spend
    assert ("campaign_attention", "overpacing") in kinds     # c2 pacing 185/day on a 100 budget
    assert not any("Paused Old" in a["title"] for a in store.list_actions())  # paused campaigns are not waste
    assert store.latest_metrics()["ad_spend"] == 460.0
    assert run_worker("ads-audit", workspace, store, onboarded, None).actions_created == 0  # idempotent per window


def test_ads_audit_scores_when_findings_exist(workspace, store, onboarded):
    (workspace.exports / "ads-google.csv").write_text(ADS_CSV)
    control = {
        "schema_version": "1.0.0", "control_id": "G-001", "category": "tracking", "severity": "high",
        "required_inputs": ["conversions"], "source_ids": ["s1"], "maturity": "source-grounded", "geographies": ["global"],
        "scoring_behavior": "health", "stability": "stable",
    }
    finding = {"schema_version": "1.0.0", "control_id": "G-001", "status": "fail", "evidence": [{"campaign_id": "c2"}],
               "confidence": "high", "observation": "no conversion tracking on c2", "diagnosis": "tag missing",
               "recommendation": "install the conversion tag"}
    (workspace.exports / "ads-google.findings.json").write_text(json.dumps(
        {"control_definitions": [control], "findings": [finding], "category_weights": {"tracking": 100.0}}))
    r = run_worker("ads-audit", workspace, store, onboarded, None)
    assert r.ok and "health" in r.summary, r
    report = workspace.reports / "ads-google.md"
    assert report.exists() and "Health score: **0.0**" in report.read_text()  # local report: no lifecycle attestation
    assert store.latest_metrics()["ad_health_score"] == 0.0  # one high-severity fail out of one control
    # with the operator's encryption attestation, the upstream claude-ads renderer produces the full report
    onboarded.config["ads"] = {"data_lifecycle": {"attested": True, "encryption_evidence": ["FileVault on the operator laptop"]}}
    (workspace.exports / "ads-google.csv").write_text(ADS_CSV.replace("2026-09-0", "2026-10-0"))
    r = run_worker("ads-audit", workspace, store, onboarded, None)
    assert r.ok and "health" in r.summary, r
    assert "Prioritized actions" in report.read_text()


def test_seo_findings_from_crawl(workspace, store, onboarded, monkeypatch):
    fake = json.dumps({"ok": True, "site": "https://acme-scheduling.example", "pages_fetched": 3, "sitemap_found": False, "pages": [
        {"url": "https://acme-scheduling.example", "meta": {"title": "Acme", "description": ""}, "excerpt": "x" * 400},
        {"url": "https://acme-scheduling.example/pricing", "meta": {"title": "Acme", "description": "Pricing"}, "excerpt": "short"},
        {"url": "https://acme-scheduling.example/blog", "meta": {}, "excerpt": "y" * 400},
    ]})

    async def fake_crawl(url, max_pages=10):
        return fake

    monkeypatch.setattr(seo, "crawl_website", fake_crawl)
    monkeypatch.setattr(seo, "authority_gap", lambda user, comps: {"user_domain": user, "user_dr": 12, "authority_mark": {"domain": "weave.com"},
                                                                     "rows": [{"rank": 1, "domain": "weave.com", "dr": 70, "comparison": "lower"},
                                                                              {"rank": 2, "domain": "nexhealth.com", "dr": 5, "comparison": "higher"}]})
    r = run_worker("seo", workspace, store, onboarded, None)
    assert r.ok, r
    kinds = {a["context"]["kind"] for a in store.list_actions("pending", "seo_opportunity")}
    assert kinds == {"missing_description", "thin_page", "missing_title", "duplicate_title", "no_sitemap", "authority_gap"}
    gap = next(a for a in store.list_actions() if a["context"]["kind"] == "authority_gap")
    assert "weave.com (DR 70)" in gap["content"] and "nexhealth" not in gap["content"]
    assert all(a["context"]["skill"].startswith("seo/") for a in store.list_actions())


def test_monitor_creates_market_signals(workspace, store, onboarded, monkeypatch):
    from revenueos.vendor.pulse import discovery

    async def fake_hn(client, query, since):
        return [{"objectID": "1", "title": "How do you handle no-shows at a small clinic?", "story_text": "We lose hours", "points": 12, "created_at": "2026-09-10T10:00:00Z"},
                {"objectID": "2", "story_title": "Weave alternative?", "comment_text": "looking to switch", "points": 3, "created_at": "2026-09-11T10:00:00Z"}]

    monkeypatch.setattr(discovery, "_hn_search", fake_hn)
    r = run_worker("monitor", workspace, store, onboarded, None)
    assert r.ok and r.actions_created == 2 and r.details["gated"] is False, r
    sig = store.list_actions("pending", "market_signal")
    assert {a["source_url"] for a in sig} == {"https://news.ycombinator.com/item?id=1", "https://news.ycombinator.com/item?id=2"}
    assert run_worker("monitor", workspace, store, onboarded, None).actions_created == 0


def test_content_matches_channels_to_skills(workspace, store, onboarded):
    r = run_worker("content", workspace, store, onboarded, None)
    assert r.ok and r.actions_created >= 3, r
    acts = store.list_actions("pending", "content_opportunity")
    assert {a["context"]["channel"] for a in acts} == {"cold email", "seo", "linkedin"}
    assert all("/" in a["context"]["skill"] for a in acts)
    # no LLM → executing explains instead of failing
    assert "ANTHROPIC_API_KEY" in execute_action(workspace, store, onboarded, None, acts[0])


def test_workers_refuse_before_onboarding(workspace, store):
    from revenueos.context import BusinessContext

    ctx = BusinessContext.load(workspace)
    for name in ("outreach", "content", "monitor"):
        r = run_worker(name, workspace, store, ctx, None)
        assert not r.ok and "not onboarded" in r.error
    assert store.list_runs()[0]["status"] == "failed"
