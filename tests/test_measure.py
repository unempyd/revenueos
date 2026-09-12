"""The loop's last two stages: measure the result, record and display it."""
from __future__ import annotations

import json

from revenueos.today import build_brief
from revenueos.workers import execute_action, measure, run_worker, seo
from tests.test_workers import ADS_CSV, LEADS_CSV


def _fake_crawl(pages):
    async def crawl(url, max_pages=10):
        return json.dumps({"ok": True, "site": url, "pages_fetched": len(pages), "sitemap_found": True, "pages": pages})
    return crawl


def test_seo_fix_is_measured_by_recrawl(workspace, store, onboarded, monkeypatch):
    before = [{"url": "https://acme-scheduling.example/pricing", "meta": {"title": "Pricing", "description": ""}, "excerpt": "x" * 400}]
    monkeypatch.setattr(seo, "crawl_website", _fake_crawl(before))
    monkeypatch.setattr(seo, "authority_gap", lambda u, c: None)
    run_worker("seo", workspace, store, onboarded, None)
    action = next(a for a in store.list_actions() if a["context"]["kind"] == "missing_description")
    assert action["context"]["before"] == {"title": "Pricing", "description": "", "body_chars": 400}

    # execute (no LLM → explanatory outcome) and record
    out = execute_action(workspace, store, onboarded, None, action)
    store.set_action_status(action["id"], "executed")
    store.record_outcome(action["id"], "pending", note=out)

    # measure while the site is unchanged → no_effect
    monkeypatch.setattr(measure, "crawl_website", _fake_crawl(before))
    r = run_worker("measure", workspace, store, onboarded, None)
    assert r.ok and r.details == {"measured": 0, "pending": 0}
    assert store.latest_outcome(action["id"])["status"] == "no_effect"

    # the customer deploys the fix → measured 0 → 1
    after = [{"url": "https://acme-scheduling.example/pricing", "meta": {"title": "Pricing", "description": "Plans from $149"}, "excerpt": "x" * 400}]
    monkeypatch.setattr(measure, "crawl_website", _fake_crawl(after))
    r = run_worker("measure", workspace, store, onboarded, None)
    assert r.details["measured"] == 1
    o = store.latest_outcome(action["id"])
    assert o["status"] == "measured" and o["metric"] == "meta_description_fixed" and o["after_value"] == 1.0
    assert json.loads(o["after_json"])["description"] == "Plans from $149"

    # final outcomes are not re-recorded
    r = run_worker("measure", workspace, store, onboarded, None)
    assert r.details == {"measured": 0, "pending": 0}

    brief = build_brief(store)
    assert brief.summary["executed"] == 1 and brief.summary["measured"] == 1
    text = brief.render_text("Acme")
    assert "RESULTS" in text and "meta_description_fixed 0 → 1" in text


def test_sitemap_fix_is_measured_and_next_learns_from_it(workspace, store, onboarded, monkeypatch):
    from revenueos.today import rank_next

    site = [{"url": "https://acme-scheduling.example", "meta": {"title": "Acme", "description": "Scheduling"}, "excerpt": "x" * 400}]

    async def no_sitemap(url, max_pages=10):
        return json.dumps({"ok": True, "site": url, "pages_fetched": 1, "sitemap_found": False, "pages": site})

    async def with_sitemap(url, max_pages=10):
        return json.dumps({"ok": True, "site": url, "pages_fetched": 1, "sitemap_found": True, "pages": site})

    monkeypatch.setattr(seo, "crawl_website", no_sitemap)
    monkeypatch.setattr(seo, "authority_gap", lambda u, c: None)
    run_worker("seo", workspace, store, onboarded, None)
    action = next(a for a in store.list_actions() if a["context"]["kind"] == "no_sitemap")
    # before anything is executed, ranking falls back to priors (seo above content)
    store.create_action("content_opportunity", "blog: x", "y", context={"executor": "run_skill", "skill": "core/x"})
    ranked = rank_next(store)
    assert ranked[0]["id"] == action["id"] and ranked[0]["win_rate"] == 0.5
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("REVENUEOS_DRY_RUN", raising=False)
    fid = store.create_action("follow_up", "Send to x@y.example", "hi", context={"executor": "send_email", "to": "x@y.example", "draft_id": "d"})
    blocked = next(r for r in rank_next(store, limit=10) if r["id"] == fid)
    assert "no mailbox" in blocked["blocked"] and blocked["score"] < ranked[0]["score"]

    store.set_action_status(action["id"], "executed")
    monkeypatch.setattr(measure, "crawl_website", no_sitemap)
    run_worker("measure", workspace, store, onboarded, None)
    assert store.latest_outcome(action["id"])["status"] == "no_effect"
    monkeypatch.setattr(measure, "crawl_website", with_sitemap)
    r = run_worker("measure", workspace, store, onboarded, None)
    o = store.latest_outcome(action["id"])
    assert r.details["measured"] == 1 and o["metric"] == "sitemap_present" and o["after_value"] == 1.0
    # a measured win raises the win rate for that action type in the next ranking
    store.create_action("seo_opportunity", "SEO: missing title — /x", "z", context={"kind": "missing_title", "executor": "run_skill", "skill": "seo/title-meta-rewriter"}, source_url="https://acme-scheduling.example/x")
    top = rank_next(store)[0]
    assert top["action_type"] == "seo_opportunity" and top["win_rate"] == 1.0 and top["executed_of_type"] == 1


def test_outreach_result_is_measured_by_reply(workspace, store, onboarded, monkeypatch):
    (workspace.exports / "leads.csv").write_text(LEADS_CSV)
    run_worker("discover", workspace, store, onboarded, None)
    run_worker("outreach", workspace, store, onboarded, None)
    monkeypatch.setenv("REVENUEOS_DRY_RUN", "1")
    action = next(a for a in store.list_actions("pending", "follow_up") if a["context"]["to"] == "maria@brightsmile.example")
    execute_action(workspace, store, onboarded, None, action)
    store.set_action_status(action["id"], "executed")

    r = run_worker("measure", workspace, store, onboarded, None)
    assert r.details == {"measured": 0, "pending": 1}
    assert store.latest_outcome(action["id"])["note"].startswith("sent ")

    send = next(s for s in store.recent_sends() if s["to_email"] == "maria@brightsmile.example")
    store.mark_send(send["id"], replied=True)
    store.transition_lead(send["lead_id"], "booked")
    with store._conn() as c:
        c.execute("UPDATE leads SET deal_value=1788 WHERE id=?", (send["lead_id"],))
    r = run_worker("measure", workspace, store, onboarded, None)
    assert r.details["measured"] == 1
    o = store.latest_outcome(action["id"])
    assert o["metric"] == "replied" and o["after_value"] == 1.0 and json.loads(o["after_json"])["lead_status"] == "booked"
    s = store.results_summary()
    assert s["emails_sent"] == 1 and s["replies"] == 1 and s["booked"] == 1 and s["pipeline_value"] == 1788.0


def test_ads_result_is_measured_by_next_export(workspace, store, onboarded):
    (workspace.exports / "ads-google.csv").write_text(ADS_CSV)
    run_worker("ads-audit", workspace, store, onboarded, None)
    waste = next(a for a in store.list_actions() if a["action_type"] == "ad_waste")
    assert waste["context"]["before"]["spend"] == 370.0
    store.set_action_status(waste["id"], "executed")
    # same export → no change yet
    assert run_worker("measure", workspace, store, onboarded, None).details["measured"] == 0
    assert store.latest_outcome(waste["id"])["status"] == "no_effect"
    # next export: campaign c2 paused after the customer acted → spend dropped
    (workspace.exports / "ads-google.csv").write_text(ADS_CSV.replace("c2,Generic Dental,enabled,cr2,Generic A,lead,0,100,190", "c2,Generic Dental,paused,cr2,Generic A,lead,0,100,0"))
    assert run_worker("measure", workspace, store, onboarded, None).details["measured"] == 1
    o = store.latest_outcome(waste["id"])
    assert o["metric"] == "campaign_spend_delta" and o["before_value"] == 370.0 and o["after_value"] == 180.0
