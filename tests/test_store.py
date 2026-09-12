from revenueos.store import ACTION_TYPES, Store


def test_actions_roundtrip_and_dedupe(store: Store):
    run = store.start_run("discover")
    a = store.create_action("prospect", "Jane at Acme", "why", run_id=run, dedupe_key="prospect:1", context={"lead_id": "x"})
    assert a is not None
    assert store.create_action("prospect", "Jane at Acme", "why", dedupe_key="prospect:1") is None
    got = store.get_action(a)
    assert got["status"] == "pending" and got["context"] == {"lead_id": "x"}
    assert store.counts_by_type() == {"prospect": 1}
    store.set_action_status(a, "approved")
    assert store.list_actions("pending") == []
    assert store.list_actions("approved")[0]["id"] == a
    store.finish_run(run, True, "done")
    assert store.list_runs()[0]["status"] == "done"


def test_unknown_action_type_rejected(store: Store):
    import pytest

    with pytest.raises(ValueError):
        store.create_action("nonsense", "t", "c")
    assert "prospect" in ACTION_TYPES


def test_lead_state_machine(store: Store):
    lead = store.upsert_lead("csv", "a@b.co", "B Co", contact_email="a@b.co", reason="fits")
    assert store.upsert_lead("csv", "a@b.co", "B Co", title="Owner") == lead  # idempotent on (source, source_id)
    assert store.get_lead(lead)["title"] == "Owner"
    draft = store.create_draft(lead, "r/hook", "v0", "hello B Co", "body", "template-v1")
    assert store.get_lead(lead)["status"] == "drafted"
    # a new draft supersedes the pending one
    draft2 = store.create_draft(lead, "r/hook", "v1", "hello again", "body2", "template-v1")
    assert store.get_draft(draft)["approval_state"] == "superseded"
    store.set_draft_approval(draft2, "approved")
    send = store.record_send(draft2, lead, "a@b.co", "hello again", "body2", "dry-run")
    assert store.get_lead(lead)["status"] == "sent"
    assert store.daily_send_count() == 1
    assert send
    store.add_unsubscribe("A@B.co", via="reply_stop")
    assert store.is_unsubscribed("a@b.co")


def test_pipeline_value_and_metrics(store: Store):
    store.upsert_lead("csv", "1", "One", deal_value=1000.0)
    store.upsert_lead("csv", "2", "Two", deal_value=500.0)
    dead = store.upsert_lead("csv", "3", "Three", deal_value=99.0)
    store.transition_lead(dead, "dead")
    assert store.pipeline_value() == 1500.0
    store.record_metric("ad_spend", 12.5, platform="google")
    store.record_metric("ad_spend", 20.0, platform="google")
    assert store.latest_metrics() == {"ad_spend": 20.0}
