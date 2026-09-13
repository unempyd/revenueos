"""Objectives and operator messages: the durable state behind "RevenueOS is working on my business"."""
from __future__ import annotations

import argparse
import json

import pytest

from revenueos.cli import cmd_messages, cmd_objective, main
from revenueos.objectives import ensure_objective, objective_block, render_objective
from revenueos.today import build_brief


def _ns(**kw) -> argparse.Namespace:
    base = {"root": None, "verb": "list", "title": None, "id": None, "text": None,
            "strategy": None, "next": None, "kind": "evidence"}
    return argparse.Namespace(**{**base, **kw})


def _msg_ns(**kw) -> argparse.Namespace:
    base = {"root": None, "to": "operator", "unread": False, "mark_read": False, "limit": 50, "json": False}
    return argparse.Namespace(**{**base, **kw})


def test_objective_lifecycle_through_the_cli(workspace, store, capsys):
    assert cmd_objective(_ns(verb="add", title="Ship RevenueOS to 10 paying businesses",
                             strategy="Adelaide businesses with live ad spend")) == 0
    oid = store.list_objectives("active")[0]["id"]
    assert capsys.readouterr().out.strip() == f"objective [{oid}] Ship RevenueOS to 10 paying businesses"

    # the same title twice is not two objectives
    assert cmd_objective(_ns(verb="add", title="Ship RevenueOS to 10 paying businesses")) == 1
    assert len(store.list_objectives()) == 1

    assert cmd_objective(_ns(verb="set", id=oid, strategy="observed-defect outreach", next="approve action 1")) == 0
    o = store.get_objective(oid)
    assert o["strategy"] == "observed-defect outreach" and o["next_action"] == "approve action 1"

    assert cmd_objective(_ns(verb="note", id=oid, kind="evidence", text="3 Adelaide clinics run Google Ads")) == 0
    assert cmd_objective(_ns(verb="note", id=oid, kind="heartbeat", text="not allowed by hand")) == 2
    kinds = [e["kind"] for e in store.list_objective_events(oid)]
    assert kinds == ["evidence"]

    assert cmd_objective(_ns(verb="show", id=oid)) == 0
    shown = capsys.readouterr().out
    assert "Ship RevenueOS to 10 paying businesses" in shown and "3 Adelaide clinics run Google Ads" in shown

    for verb, expected in (("pause", "paused"), ("resume", "active"), ("done", "done")):
        assert cmd_objective(_ns(verb=verb, id=oid)) == 0
        assert store.get_objective(oid)["status"] == expected
    assert store.list_objectives("active") == []

    assert cmd_objective(_ns(verb="show", id=9999)) == 2


def test_objective_add_and_list_via_argv(workspace, store, capsys):
    assert main(["objective", "add", "Generate real revenue", "--strategy", "pay after the first measured result"]) == 0
    capsys.readouterr()
    assert main(["objective", "list"]) == 0
    out = capsys.readouterr().out
    assert "Generate real revenue" in out and "active" in out
    assert store.list_objectives("active")[0]["strategy"] == "pay after the first measured result"


def test_today_shows_the_objective_and_how_to_add_one(workspace, store, onboarded):
    brief = build_brief(store)
    assert brief.objective is None
    text = brief.render_text(onboarded.company_name)
    assert "OBJECTIVE" in text and "revenueos objective add" in text

    oid = store.create_objective("Fill 20 empty chairs a month", strategy="rebooking outreach")
    store.update_objective(oid, next_action="approve action 7")
    store.add_objective_event(oid, "heartbeat", "3 pending · next: approve action 7")
    brief = build_brief(store)
    assert brief.objective["id"] == oid and brief.objective["heartbeat"].startswith("3 pending")
    text = brief.render_text(onboarded.company_name)
    assert "Fill 20 empty chairs a month" in text
    assert "next: approve action 7" in text and "last heartbeat" in text


def test_render_objective_without_a_heartbeat_says_so():
    lines = render_objective({"id": 1, "title": "T", "status": "active", "strategy": None,
                              "next_action": None, "heartbeat_at": None, "heartbeat": None, "others": 0})
    assert any("last heartbeat: none yet" in ln for ln in lines)


def test_ensure_objective_is_idempotent_and_fills_a_missing_strategy(store):
    first = ensure_objective(store, "One objective", strategy=None)
    assert ensure_objective(store, "  one objective  ") is None
    assert ensure_objective(store, "One objective", strategy="added later") is None
    assert store.get_objective(first)["strategy"] == "added later"
    assert ensure_objective(store, "   ") is None
    assert len(store.list_objectives()) == 1


def test_init_creates_the_objective_from_the_questionnaire(workspace, store, tmp_path, capsys):
    from tests.conftest import ANSWERS

    answers = {**ANSWERS, "objective": "Book 12 demos in 90 days"}
    p = tmp_path / "answers.json"
    p.write_text(json.dumps(answers))
    assert main(["init", "--answers", str(p)]) == 0
    capsys.readouterr()
    assert [o["title"] for o in store.list_objectives("active")] == ["Book 12 demos in 90 days"]
    # re-running onboarding does not stack a second copy
    assert main(["init", "--answers", str(p)]) == 0
    assert len(store.list_objectives()) == 1
    assert objective_block(store)["title"] == "Book 12 demos in 90 days"


def test_messages_store_and_cli(workspace, store, capsys):
    mid = store.post_message("heartbeat", "operator", "2 approved actions waiting", "Execute them: revenueos execute 4", {"key": "a"})
    store.post_message("heartbeat", "strategist", "not for the operator", "")
    assert [m["id"] for m in store.list_messages("operator")] == [mid]
    assert store.list_messages("operator", unread_only=True)[0]["ref"] == {"key": "a"}

    assert cmd_messages(_msg_ns(unread=True, mark_read=True)) == 0
    out = capsys.readouterr().out
    assert "2 approved actions waiting" in out and "revenueos execute 4" in out and "marked 1 message(s) read" in out
    assert store.list_messages("operator", unread_only=True) == []
    assert store.list_messages("operator")[0]["read_at"]

    assert cmd_messages(_msg_ns(unread=True)) == 0
    assert "no messages unread" in capsys.readouterr().out
    assert cmd_messages(_msg_ns(json=True)) == 0
    assert json.loads(capsys.readouterr().out)[0]["subject"] == "2 approved actions waiting"


def test_unknown_status_and_kind_are_refused(store):
    oid = store.create_objective("T")
    with pytest.raises(ValueError):
        store.set_objective_status(oid, "finished")
    with pytest.raises(ValueError):
        store.add_objective_event(oid, "vibes", "nope")
