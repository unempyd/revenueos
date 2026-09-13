"""The heartbeat: deterministic, idempotent, and silent unless a human is actually needed."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from revenueos.workers import run_worker
from revenueos.workers.heartbeat import classify_error, read_state


def _run(workspace, store, ctx, llm=None):
    return run_worker("heartbeat", workspace, store, ctx, llm, "test")


def test_heartbeat_is_registered_and_runs_with_no_llm(workspace, store, onboarded):
    from revenueos.workers import all_workers

    assert "heartbeat" in all_workers()
    r = _run(workspace, store, onboarded)
    assert r.ok and r.summary and r.actions_created == 0
    assert "no objective set" in r.summary
    json.dumps(r.as_json())  # the orchestrator parses this line


def test_one_heartbeat_event_per_run_and_next_action_is_written(workspace, store, onboarded):
    oid = store.create_objective("Fill empty chairs")
    store.create_action("seo_opportunity", "Missing meta description on /pricing", "…", dedupe_key="seo:1")

    r1 = _run(workspace, store, onboarded)
    assert r1.ok
    beats = store.list_objective_events(oid, limit=50, kind="heartbeat")
    assert len(beats) == 1 and beats[0]["text"] == r1.summary
    assert "1 pending" in r1.summary and "next: approve" in r1.summary
    o = store.get_objective(oid)
    assert o["next_action"].startswith("approve [")

    r2 = _run(workspace, store, onboarded)
    assert len(store.list_objective_events(oid, limit=50, kind="heartbeat")) == 2
    assert r2.summary == r1.summary  # nothing changed, so the story did not change


def test_result_and_failure_events_are_idempotent_across_runs(workspace, store, onboarded):
    oid = store.create_objective("Prove a measurable result")
    aid = store.create_action("seo_opportunity", "Add a meta description to /pricing", "…", dedupe_key="seo:2")
    store.set_action_status(aid, "executed")
    store.record_outcome(aid, "measured", metric="meta_description_fixed", before_value=0, after_value=1)
    rid = store.start_run("seo", "test")
    store.finish_run(rid, False, "httpx.ConnectError: [Errno 8] nodename nor servname provided")

    _run(workspace, store, onboarded)
    results = store.list_objective_events(oid, limit=50, kind="result")
    failures = store.list_objective_events(oid, limit=50, kind="failure")
    assert len(results) == 1 and results[0]["ref"]["action_id"] == aid
    assert "meta_description_fixed 0 → 1" in results[0]["text"]
    assert len(failures) == 1 and failures[0]["ref"] == {"worker": "seo", "error_class": "network",
                                                         "day": datetime.now(UTC).strftime("%Y-%m-%d")}

    _run(workspace, store, onboarded)
    assert len(store.list_objective_events(oid, limit=50, kind="result")) == 1
    assert len(store.list_objective_events(oid, limit=50, kind="failure")) == 1


def test_blocked_send_is_detected_and_reported(workspace, store, onboarded, monkeypatch):
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("REVENUEOS_DRY_RUN", raising=False)
    store.create_objective("Book meetings")
    store.create_action("follow_up", "Send to maria@example.com", "…", dedupe_key="out:1",
                        context={"executor": "send_email", "to": "maria@example.com"})

    state = read_state(store)
    assert len(state["blocked_sends"]) == 1 and "no mailbox" in state["blocked_sends"][0]["why"]

    r = _run(workspace, store, onboarded)
    assert "blocked: no mailbox" in r.summary
    assert r.details["top_next"]["blocked"]
    inbox = store.list_messages("operator", unread_only=True)
    assert any("cannot be sent" in m["subject"] for m in inbox)

    monkeypatch.setenv("REVENUEOS_DRY_RUN", "1")
    assert read_state(store)["blocked_sends"] == []


def test_operator_is_messaged_once_while_an_approved_action_waits(workspace, store, onboarded):
    store.create_objective("Execute what was approved")
    aid = store.create_action("content_opportunity", "Write the rebooking guide", "…", dedupe_key="c:1",
                              context={"executor": "run_skill"})
    store.set_action_status(aid, "approved")

    r = _run(workspace, store, onboarded)
    assert f"execute the approved action [{aid}]" in r.summary
    unread = store.list_messages("operator", unread_only=True)
    assert len(unread) == 1 and unread[0]["subject"] == "1 approved action(s) waiting to run"
    assert unread[0]["from_agent"] == "heartbeat" and str(aid) in unread[0]["body"]

    _run(workspace, store, onboarded)  # the same unread note is not posted twice
    assert len(store.list_messages("operator", unread_only=True)) == 1

    store.mark_read(unread[0]["id"])
    _run(workspace, store, onboarded)  # once read, an unresolved condition reminds again
    assert len(store.list_messages("operator", unread_only=True)) == 1


def test_stale_approvals_are_flagged(workspace, store, onboarded):
    store.create_objective("Execute what was approved")
    aid = store.create_action("content_opportunity", "Write the guide", "…", dedupe_key="c:2")
    store.set_action_status(aid, "approved")
    old = (datetime.now(UTC) - timedelta(hours=48)).isoformat(timespec="seconds")
    with store._conn() as c:
        c.execute("UPDATE actions SET decided_at=? WHERE id=?", (old, aid))
    state = read_state(store)
    assert [s["action_id"] for s in state["stale_approvals"]] == [aid]
    r = _run(workspace, store, onboarded)
    assert "approved more than 24h ago" in store.list_messages("operator", unread_only=True)[0]["body"]
    assert r.ok


def test_nothing_pending_says_so_and_posts_no_message(workspace, store, onboarded):
    store.create_objective("Quiet day", strategy="wait for the next audit")
    r = _run(workspace, store, onboarded)
    assert "nothing is waiting for a decision" in r.summary
    assert store.list_messages("operator") == []


def test_error_classification():
    assert classify_error("LLMUnavailable: no credential") == "model unavailable"
    assert classify_error("RateLimitError: rate limit exceeded") == "model unavailable"
    assert classify_error("httpx.ConnectTimeout: timed out") == "network"
    assert classify_error("KeyError: 'campaign_id'") == "worker failure"
    assert classify_error("") == "worker failure"


def test_heartbeat_never_creates_actions_or_sends(workspace, store, onboarded):
    store.create_objective("Do no harm")
    store.create_action("ad_waste", "Pause the branded campaign", "…", dedupe_key="ads:1")
    before = len(store.list_actions(None, limit=1000))
    r = _run(workspace, store, onboarded)
    assert r.actions_created == 0
    assert len(store.list_actions(None, limit=1000)) == before
    assert store.recent_sends() == []


def test_details_are_json_serialisable_and_carry_the_state(workspace, store, onboarded):
    oid = store.create_objective("Everything in one payload")
    r = _run(workspace, store, onboarded)
    payload = json.loads(json.dumps(r.as_json()))
    d = payload["details"]
    assert d["objectives"][0]["id"] == oid
    assert set(d) >= {"pending_by_type", "approved_waiting", "measured", "failures", "blocked_sends",
                      "funnel", "results", "next_action", "messages_posted"}


# ── convert: the heartbeat notices the clock and drafts the offer ─────────────
def _measured_result(store, days_ago=3):
    """An executed action with a measured outcome `days_ago` days ago — the pay-on-result clock."""
    import pytest

    aid = store.create_action("seo_opportunity", "SEO: missing description — /pricing", "…", dedupe_key="seo:conv")
    store.set_action_status(aid, "executed")
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    with pytest.MonkeyPatch.context() as m:
        m.setattr("revenueos.store.now", lambda: ts)
        store.record_outcome(aid, "measured", metric="meta_description_fixed", before_value=0, after_value=1)
    return aid


def test_heartbeat_reports_no_offer_before_anything_is_measured(workspace, store, onboarded):
    r = _run(workspace, store, onboarded)
    assert r.details["offer"]["state"] == "none"
    assert r.details["offer"]["action_id"] is None
    assert "offer:" not in r.summary
    assert "tenant_offers" not in r.details


def test_heartbeat_drafts_the_offer_once_and_tells_the_operator(workspace, store, onboarded, monkeypatch):
    monkeypatch.delenv("REVENUEOS_PAYMENT_LINK_PRO", raising=False)
    store.create_objective("Fill empty chairs")
    _measured_result(store, days_ago=3)

    r1 = _run(workspace, store, onboarded)
    offer = r1.details["offer"]
    assert offer["state"] == "clock_running" and offer["days_left"] == 11
    assert offer["action_id"] is not None
    assert " · offer: clock_running" in r1.summary
    assert r1.actions_created == 1  # the draft, and nothing else

    notes = [m for m in store.list_messages("operator", unread_only=True) if "Pro offer drafted" in m["subject"]]
    assert len(notes) == 1
    assert notes[0]["subject"] == (f"Acme Scheduling: first measured result on {offer['first_result_at'][:10]}; "
                                  f"Pro offer drafted as action [{offer['action_id']}]")

    r2 = _run(workspace, store, onboarded)  # idempotent: no second draft, no second note
    assert r2.details["offer"]["action_id"] is None
    assert r2.actions_created == 0
    assert len([a for a in store.list_actions("pending") if (a["context"] or {}).get("kind") == "offer"]) == 1
    assert len([m for m in store.list_messages("operator") if "Pro offer drafted" in m["subject"]]) == 1
    json.dumps(r2.as_json())  # the orchestrator still parses the line


def test_heartbeat_measures_an_executed_offer_from_the_billing_workers_last_read(workspace, store, onboarded):
    from revenueos import convert

    _measured_result(store, days_ago=2)
    aid = convert.ensure_offer_action(workspace, store, onboarded, customer_email="owner@acme.example")
    store.set_action_status(aid, "executed")
    convert.write_customers_export(workspace, [{"email": "owner@acme.example", "customer": "cus_9", "subscription": "sub_9"}])

    r = _run(workspace, store, onboarded)
    assert r.details["offer"]["offers_measured"] == 1
    o = store.latest_outcome(aid)
    assert o["status"] == "measured" and o["metric"] == "subscribed" and o["after_value"] == 1.0
