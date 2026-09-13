"""A draft's one factual claim about the live site ("phone shown but not tappable", "no
LocalBusiness schema") is re-verified against the live homepage before it can send — once
while drafting, once again right before send — because the site can change (or the
discover-time read can simply have been wrong) between discovery and a human's approval.
A human-written qualification note ("kind": "note") is never re-verified: it is not a claim
about the live page.
"""
from __future__ import annotations

from revenueos.workers import execute_action, outreach, run_worker

PHONE_LEAD_CSV = """email,first_name,last_name,company,title,website,linkedin_url,reason,lead_id,qualified_at
owner@hogsbreath-test.example,Pat,Owner,Hogsbreath Test Cafe,Owner,https://hogsbreath-test.example,,"ad/analytics tags on the site: Google Ads tag; phone shown but not tappable",L1,2026-09-10
"""

CLAIM_HOLDS = {"ok": True, "phones": ["0438 187 373"], "tel_link": False, "local_schema": False}
CLAIM_FAILS_NO_PHONE = {"ok": True, "phones": [], "tel_link": False, "local_schema": False}
FETCH_FAILED = {"ok": False, "error": "ConnectError"}


def _make_pending_action(workspace, store, onboarded, monkeypatch):
    """Discover + draft one 'phone' claim, with homepage_signals patched so the pre-draft
    freshness check passes and a draft is actually created."""
    (workspace.exports / "leads.csv").write_text(PHONE_LEAD_CSV)
    run_worker("discover", workspace, store, onboarded, None)
    monkeypatch.setattr(outreach, "homepage_signals", lambda site, timeout=15.0: CLAIM_HOLDS)
    r = run_worker("outreach", workspace, store, onboarded, None)
    assert r.ok and r.actions_created == 1, r
    action = next(a for a in store.list_actions("pending", "follow_up") if a["context"]["to"] == "owner@hogsbreath-test.example")
    assert action["context"]["kind"] == "phone"
    assert action["context"]["claim_verified_at"]  # verified once already, at draft time
    return action


def test_claim_holds_sends_and_records_verification(workspace, store, onboarded, monkeypatch):
    action = _make_pending_action(workspace, store, onboarded, monkeypatch)
    monkeypatch.setenv("REVENUEOS_DRY_RUN", "1")
    monkeypatch.setattr(outreach, "homepage_signals", lambda site, timeout=15.0: CLAIM_HOLDS)

    outcome = execute_action(workspace, store, onboarded, None, action)

    assert outcome.startswith("sent via dry-run"), outcome
    sends = store.recent_sends()
    assert len(sends) == 1 and sends[0]["to_email"] == "owner@hogsbreath-test.example"
    refreshed = store.get_action(action["id"])
    assert refreshed["context"]["claim_verified_at"]
    assert refreshed["context"]["claim"]


def test_claim_fails_withdraws_the_draft_and_does_not_send(workspace, store, onboarded, monkeypatch):
    action = _make_pending_action(workspace, store, onboarded, monkeypatch)
    monkeypatch.setenv("REVENUEOS_DRY_RUN", "1")
    # Between drafting and send, the site changed: no phone number is shown at all any more.
    monkeypatch.setattr(outreach, "homepage_signals", lambda site, timeout=15.0: CLAIM_FAILS_NO_PHONE)

    outcome = execute_action(workspace, store, onboarded, None, action)

    assert outcome.startswith("not sent:"), outcome
    assert "draft withdrawn" in outcome
    assert not store.recent_sends()
    draft = store.get_draft(action["context"]["draft_id"])
    assert draft["approval_state"] == "rejected"
    refreshed = store.get_action(action["id"])
    assert refreshed["status"] != "executed"
    assert refreshed["status"] == "ignored"
    assert refreshed["context"].get("claim_recheck_failed_at")


def test_fetch_failure_leaves_the_action_retryable(workspace, store, onboarded, monkeypatch):
    action = _make_pending_action(workspace, store, onboarded, monkeypatch)
    monkeypatch.setenv("REVENUEOS_DRY_RUN", "1")
    monkeypatch.setattr(outreach, "homepage_signals", lambda site, timeout=15.0: FETCH_FAILED)

    outcome = execute_action(workspace, store, onboarded, None, action)

    assert outcome.startswith("not sent: could not re-check")
    assert "try again later" in outcome
    assert not store.recent_sends()
    draft = store.get_draft(action["context"]["draft_id"])
    assert draft["approval_state"] != "rejected"
    refreshed = store.get_action(action["id"])
    assert refreshed["status"] not in ("executed", "ignored")


def test_worker_skips_a_candidate_whose_claim_no_longer_holds(workspace, store, onboarded, monkeypatch):
    (workspace.exports / "leads.csv").write_text(PHONE_LEAD_CSV)
    run_worker("discover", workspace, store, onboarded, None)
    monkeypatch.setattr(outreach, "homepage_signals", lambda site, timeout=15.0: CLAIM_FAILS_NO_PHONE)

    r = run_worker("outreach", workspace, store, onboarded, None)

    assert r.ok and r.actions_created == 0, r
    assert r.details["stale"] == 1
    assert "1 skipped (claim no longer observed)" in r.summary
    assert store.list_actions("pending", "follow_up") == []


def test_note_kind_is_never_re_verified(workspace, store, onboarded, monkeypatch):
    """A human-written qualification note is not a claim about the live page: no network call,
    sends exactly as before."""
    from tests.test_workers import LEADS_CSV

    (workspace.exports / "leads.csv").write_text(LEADS_CSV)
    run_worker("discover", workspace, store, onboarded, None)

    def _boom(site, timeout=15.0):
        raise AssertionError("homepage_signals must not be called for a 'note' kind draft")

    monkeypatch.setattr(outreach, "homepage_signals", _boom)
    r = run_worker("outreach", workspace, store, onboarded, None)
    assert r.ok and r.actions_created == 2, r
    action = next(a for a in store.list_actions("pending", "follow_up") if a["context"]["to"] == "maria@brightsmile.example")
    assert action["context"]["kind"] == "note"

    monkeypatch.setenv("REVENUEOS_DRY_RUN", "1")
    outcome = execute_action(workspace, store, onboarded, None, action)
    assert outcome.startswith("sent via dry-run")


def test_cli_execute_does_not_stamp_a_refused_send_as_executed(workspace, store, onboarded, monkeypatch, capsys):
    """The executor said "not sent: …": the CLI must not mark the action executed (it used to)."""
    from revenueos.cli import main

    action = _make_pending_action(workspace, store, onboarded, monkeypatch)
    store.set_action_status(action["id"], "approved")
    monkeypatch.setenv("REVENUEOS_DRY_RUN", "1")
    monkeypatch.setattr(outreach, "homepage_signals", lambda site, timeout=15.0: FETCH_FAILED)

    rc = main(["--root", str(workspace.root), "execute", str(action["id"])])

    assert rc == 1
    assert "not executed" in capsys.readouterr().out
    assert store.get_action(action["id"])["status"] == "approved"
    assert not store.recent_sends()
