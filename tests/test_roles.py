"""Roles: a specialised subagent is one spec + one model call + one recorded row.

Every test here injects a fake LLM exposing exactly the surface `llm.py` exposes
(`ask(system, user, max_tokens=...)`), so the contract, the recursion and the honesty rules
are exercised without a provider.
"""
from __future__ import annotations

import json
import re

import pytest

from revenueos.roles import MAX_DEPTH, ROLES, run_role, spec_path

VALID = {
    "summary": "Two of the three competitors dropped their entry price this quarter.",
    "findings": [
        {"claim": "weave.com lists $99/month entry", "evidence": "https://weave.com/pricing", "confidence": "high"},
        {"claim": "nexhealth.com does not publish a price", "evidence": "not observed", "confidence": "medium"},
    ],
    "cannot_determine": ["what either charges above 10 chairs — both pricing pages stop at 10"],
}


class FakeLLM:
    """The same surface llm.LLM exposes to a worker: ask(system, user, max_tokens=...)."""

    def __init__(self, answer):
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def ask(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
        self.calls.append((system, user))
        return self.answer(system, user) if callable(self.answer) else self.answer


def _role_in(system: str) -> str:
    m = re.search(r"# Role: (\w+)", system)
    return m.group(1) if m else "?"


def test_every_role_has_a_spec_with_purpose_and_a_contract(workspace):
    for role in ROLES:
        text = spec_path(workspace, role).read_text(encoding="utf-8")
        assert text.startswith(f"# Role: {role}")
        assert "## Purpose" in text and "## Inputs" in text and "## Output JSON contract" in text
        assert "not observed" in text  # the evidence rule is stated in every spec


def test_valid_answer_is_recorded_as_a_run(workspace, onboarded, store):
    llm = FakeLLM(json.dumps(VALID))
    result = run_role("research", "What do competitors charge?", workspace, store, onboarded, llm)

    assert result.ok and result.error is None
    assert result.output["summary"] == VALID["summary"]
    assert result.evidence == ["https://weave.com/pricing", "not observed"]
    row = store.get_agent_run(result.run_id)
    assert row["role"] == "research" and row["ok"] is True and row["depth"] == 0 and row["parent_run_id"] is None
    assert row["output"]["findings"][0]["claim"] == VALID["findings"][0]["claim"]
    assert row["started_at"] and row["finished_at"]
    assert store.list_agent_runs(role="research")[0]["id"] == result.run_id
    # the spec and the business canon both reach the model
    system, user = llm.calls[0]
    assert "# Role: research" in system and "Acme Scheduling" in system
    assert "What do competitors charge?" in user


def test_answer_that_is_not_the_contract_fails_and_keeps_the_raw_text(workspace, onboarded, store):
    llm = FakeLLM("Sure! Here is what I think: competitors are probably cheaper.")
    result = run_role("research", "What do competitors charge?", workspace, store, onboarded, llm)

    assert result.ok is False and result.output is None
    assert "no JSON object" in result.error
    assert result.raw.startswith("Sure!")
    row = store.get_agent_run(result.run_id)
    assert row["ok"] is False and row["output"]["raw"].startswith("Sure!") and row["evidence"] == []
    assert store.list_actions("pending") == []  # nothing is proposed off a failed parse


def test_a_claim_without_evidence_is_not_a_finding(workspace, onboarded, store):
    llm = FakeLLM(json.dumps({"summary": "s", "findings": [{"claim": "we will 10x revenue", "evidence": ""}]}))
    result = run_role("marketing", "Write the hero line", workspace, store, onboarded, llm)

    assert result.ok is False and "no evidence" in result.error
    assert store.list_actions("pending") == []


def test_no_model_is_honest_and_still_recorded(workspace, onboarded, store):
    result = run_role("sales", "What is the constraint?", workspace, store, onboarded, None)

    assert result.ok is False and result.error == "no model configured"
    assert result.output is None and result.proposed_action_ids == []
    row = store.get_agent_run(result.run_id)
    assert row["ok"] is False and row["error"] == "no model configured" and row["output"] is None


def test_model_failure_is_reported_not_guessed_around(workspace, onboarded, store):
    class Broken:
        def ask(self, system, user, *, max_tokens=4096):
            from revenueos.llm import LLMUnavailable

            raise LLMUnavailable("claude -p failed (1): not logged in")

    result = run_role("sales", "What is the constraint?", workspace, store, onboarded, Broken())
    assert result.ok is False and "not logged in" in result.error
    assert store.get_agent_run(result.run_id)["ok"] is False


def test_proposed_actions_become_pending_actions_and_dedupe(workspace, onboarded, store):
    answer = json.dumps({
        "summary": "One page is missing a description.",
        "findings": [{"claim": "/pricing has no meta description", "evidence": "https://acme-scheduling.example/pricing"}],
        "proposed_actions": [
            {"action_type": "seo_opportunity", "title": "Write a meta description for /pricing",
             "why": "the page ranks but has no description", "context": {"executor": "run_skill", "skill": "seo/meta"}},
            {"action_type": "world_domination", "title": "Nope", "why": "nope", "context": {"executor": "run_skill"}},
            {"action_type": "seo_opportunity", "title": "No executor here", "why": "because", "context": {}},
        ],
    })
    llm = FakeLLM(answer)
    first = run_role("research", "Audit the site", workspace, store, onboarded, llm)

    assert len(first.proposed_action_ids) == 1
    action = store.get_action(first.proposed_action_ids[0])
    assert action["status"] == "pending" and action["action_type"] == "seo_opportunity"
    assert action["context"]["executor"] == "run_skill" and action["context"]["proposed_by"] == "role:research"
    assert action["context"]["agent_run_id"] == first.run_id
    skipped = store.get_agent_run(first.run_id)["output"]["skipped_proposals"]
    assert any("world_domination" in s for s in skipped) and any("no executor" in s for s in skipped)

    second = run_role("research", "Audit the site", workspace, store, onboarded, llm)
    assert second.ok and second.proposed_action_ids == []  # dedupe_key: proposed once, offered once
    assert len(store.list_actions("pending")) == 1


def test_subtasks_recurse_in_parallel_and_stop_at_the_depth_cap(workspace, onboarded, store):
    def answer(system: str, _user: str) -> str:
        role = _role_in(system)
        return json.dumps({
            "summary": f"{role} did its part",
            "findings": [{"claim": f"{role} claim", "evidence": "not observed"}],
            "subtasks": [{"role": "marketing", "task": "narrow it down"}, {"role": "sales", "task": "price it"}],
        })

    result = run_role("research", "Whole market", workspace, store, onboarded, FakeLLM(answer))

    assert result.ok and len(result.subresults) == 2
    assert all(len(s.subresults) == 2 for s in result.subresults)          # depth 1 still recurses
    leaves = [g for s in result.subresults for g in s.subresults]
    assert all(g.depth == MAX_DEPTH and g.subresults == [] for g in leaves)  # depth 2 is the floor
    assert all("subtasks_skipped" in (g.output or {}) for g in leaves)

    rows = store.list_agent_runs(limit=50)
    assert len(rows) == 7 and max(r["depth"] for r in rows) == MAX_DEPTH
    assert {r["parent_run_id"] for r in rows if r["depth"] == 1} == {result.run_id}
    assert set(store.list_agent_runs(parent_run_id=result.run_id, limit=10)[0].keys()) >= {"role", "task", "depth"}


def test_measurement_role_is_handed_the_store_when_no_inputs_are_given(workspace, onboarded, store):
    aid = store.create_action("seo_opportunity", "Fix /pricing", "no description", context={"executor": "run_skill"})
    store.set_action_status(aid, "executed")
    store.record_outcome(aid, "measured", metric="meta_description_fixed", before_value=0, after_value=1)
    llm = FakeLLM(json.dumps({"summary": "one fix landed",
                              "findings": [{"claim": "the description exists now", "evidence": f"action #{aid}"}]}))

    result = run_role("measurement", "Evaluate everything", workspace, store, onboarded, llm)

    assert result.ok
    _system, user = llm.calls[0]
    assert "meta_description_fixed" in user and f'"id": {aid}' in user
    assert store.get_agent_run(result.run_id)["inputs"]["summary"]["measured"] == 1


def test_unknown_role_is_refused(workspace, onboarded, store):
    with pytest.raises(ValueError, match="unknown role"):
        run_role("ceo", "do everything", workspace, store, onboarded, None)
