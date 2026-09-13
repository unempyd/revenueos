"""The learning loop: lessons read off measured outcomes, and role specs refined in place.

Nothing here may invent a metric, and no refinement may touch a spec's hard rules.
"""
from __future__ import annotations

import pytest

from revenueos.learning import (
    MAX_DIFF_LINES,
    RefinementRefused,
    add_lesson,
    lessons_from_outcomes,
    lessons_path,
    list_snapshots,
    recent_lessons,
    refine,
    refinements_path,
    rollback,
)
from revenueos.roles import spec_path


class FakeLLM:
    """llm.py's surface: ask(system, user, max_tokens=...)."""

    def __init__(self, answer):
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def ask(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
        self.calls.append((system, user))
        return self.answer(system, user) if callable(self.answer) else self.answer


def _executed(store, action_type="seo_opportunity", executor="run_skill", title="Fix /pricing"):
    aid = store.create_action(action_type, title, "why it matters", context={"executor": executor},
                              dedupe_key=f"t:{title}")
    store.set_action_status(aid, "executed")
    return aid


# ── lessons ─────────────────────────────────────────────────────────────────
def test_one_lesson_per_measured_outcome_and_idempotent(workspace, store):
    fixed = _executed(store)
    store.record_outcome(fixed, "measured", metric="meta_description_fixed", before_value=0, after_value=1,
                         note="re-crawl confirmed the description")
    flat = _executed(store, "ad_waste", title="Pause the losing ad group")
    store.record_outcome(flat, "no_effect", metric="campaign_spend_delta", before_value=120.0, after_value=120.0)
    dark = _executed(store, "content_opportunity", title="Write the rebooking guide")
    store.record_outcome(dark, "unmeasurable", note="no analytics connected to count reads")
    pending = _executed(store, "follow_up", title="Send to maria")
    store.record_outcome(pending, "pending", note="awaiting measurement")

    assert lessons_from_outcomes(workspace, store) == 3        # the pending one is not a lesson
    assert lessons_from_outcomes(workspace, store) == 0        # dedupe by action id

    text = lessons_path(workspace).read_text(encoding="utf-8")
    assert text.count("\n## ") == 3
    assert f"action #{fixed}" in text and f"action #{pending}" not in text
    assert "meta_description_fixed 0 → 1" in text
    assert "Why: not analysed (no model)" in text and "Lesson: pending analysis" in text
    # the honest fields, not a computed one
    assert "Result: unmeasurable — no analytics connected to count reads" in text
    assert "What worked: not observed" in text


def test_newest_lesson_is_on_top(workspace, store):
    add_lesson(workspace, "first", attempt="a", result="r", evidence="action #1")
    add_lesson(workspace, "second", attempt="a", result="r", evidence="action #2")
    body = lessons_path(workspace).read_text(encoding="utf-8")
    assert body.index("— second") < body.index("— first")
    assert body.startswith("# LESSONS.md")


def test_the_model_writes_only_why_and_lesson(workspace, store):
    aid = _executed(store)
    store.record_outcome(aid, "measured", metric="meta_description_fixed", before_value=0, after_value=1)
    llm = FakeLLM('{"why": "the page template had no description field", '
                  '"lesson": "check the template before writing page copy", "apply_when": "any seo_opportunity on a templated page"}')

    assert lessons_from_outcomes(workspace, store, llm) == 1
    text = lessons_path(workspace).read_text(encoding="utf-8")
    assert "Why: the page template had no description field" in text
    assert "Lesson: check the template before writing page copy" in text
    assert f"Evidence: action #{aid}" in text              # still read off the store, not the model
    assert "meta_description_fixed 0 → 1" in text
    system, user = llm.calls[0]
    assert "may NOT introduce any number" in system and "meta_description_fixed" in user


def test_a_broken_model_answer_does_not_become_a_lesson_body(workspace, store):
    aid = _executed(store)
    store.record_outcome(aid, "measured", metric="pages_fixed", before_value=0, after_value=1)
    assert lessons_from_outcomes(workspace, store, FakeLLM("I'd say revenue went up 30%.")) == 1
    text = lessons_path(workspace).read_text(encoding="utf-8")
    assert "30%" not in text and "Why: not analysed (model answer did not parse)" in text


def test_prompt_summary_carries_the_lessons(workspace, onboarded, store):
    assert "<lessons>" not in onboarded.prompt_summary()
    add_lesson(workspace, "recall reminders beat discounts", attempt="sent both", result="measured — replies 1 → 4",
               evidence="action #7", lesson="lead with the recall reminder")
    summary = onboarded.prompt_summary(max_chars=20000)
    assert "<lessons>" in summary and "recall reminders beat discounts" in summary
    assert "</lessons>" in summary
    assert len(onboarded.prompt_summary(max_chars=300)) <= 300   # still bounded


def test_old_lessons_fall_out_of_the_window(workspace, store):
    path = lessons_path(workspace)
    path.write_text("# LESSONS.md\n\n## 2019-01-01 — ancient\nLesson: no\n\n", encoding="utf-8")
    assert recent_lessons(workspace) == ""


# ── refinement ──────────────────────────────────────────────────────────────
def test_refine_snapshots_logs_and_rollback_restores_byte_for_byte(workspace):
    path = spec_path(workspace, "research")
    before = path.read_bytes()

    ref = refine(workspace, "research", "action #12: the answer cited a page it was never given",
                 "when a URL is not in the inputs, the evidence is 'not observed'")

    assert ref.diff_lines <= MAX_DIFF_LINES and ref.mode == "append"
    assert ref.snapshot.exists() and ref.snapshot.read_bytes() == before
    after = path.read_text(encoding="utf-8")
    assert "## Refinements" in after and "not observed" in after
    assert "when a URL is not in the inputs" in after
    assert "_(none yet" not in after
    log = refinements_path(workspace).read_text(encoding="utf-8")
    assert "research" in log and ref.snapshot.name in log and "action #12" in log

    restored = rollback(workspace, "research")
    assert restored == ref.snapshot and path.read_bytes() == before
    assert "ROLLBACK" in refinements_path(workspace).read_text(encoding="utf-8")


def test_rollback_can_name_a_snapshot(workspace):
    path = spec_path(workspace, "sales")
    first = path.read_bytes()
    a = refine(workspace, "sales", "action #1", "one")
    refine(workspace, "sales", "action #2", "two")
    assert len(list_snapshots(workspace, "sales")) == 2
    rollback(workspace, "sales", a.snapshot.name)
    assert path.read_bytes() == first


def test_a_refinement_needs_evidence(workspace):
    with pytest.raises(RefinementRefused, match="needs evidence"):
        refine(workspace, "marketing", "  ", "tighten the copy rules")


def test_the_purpose_section_is_immutable(workspace):
    path = spec_path(workspace, "measurement")
    before = path.read_text(encoding="utf-8")
    tampered = before.replace("Never compute, estimate, extrapolate or round a metric that is not in the record.",
                              "Estimate the metric when it is missing.")
    assert tampered != before

    with pytest.raises(RefinementRefused, match="immutable"):
        refine(workspace, "measurement", "action #3", "loosen the metric rule", FakeLLM(tampered))
    assert path.read_text(encoding="utf-8") == before
    assert list_snapshots(workspace, "measurement") == []       # a refused edit leaves nothing behind
    assert not refinements_path(workspace).exists()


def test_a_rewrite_over_the_diff_cap_is_refused(workspace):
    path = spec_path(workspace, "marketing")
    before = path.read_text(encoding="utf-8")
    bloated = before.rstrip("\n") + "\n" + "\n".join(f"- extra rule {i}" for i in range(MAX_DIFF_LINES + 5)) + "\n"

    with pytest.raises(RefinementRefused, match="is not a refinement"):
        refine(workspace, "marketing", "action #4", "add a pile of rules", FakeLLM(bloated))
    assert path.read_text(encoding="utf-8") == before
    assert list_snapshots(workspace, "marketing") == []


def test_a_model_rewrite_within_the_cap_is_applied(workspace):
    path = spec_path(workspace, "marketing")
    before = path.read_text(encoding="utf-8")
    edited = before.replace("- `task` — the positioning, copy or channel question.",
                            "- `task` — the positioning, copy or channel question, with the page it concerns.")

    ref = refine(workspace, "marketing", "action #5: two drafts ignored the page they were for",
                 "name the page in the task", FakeLLM(edited))

    assert ref.mode == "model" and ref.diff_lines == 2
    assert "with the page it concerns" in path.read_text(encoding="utf-8")
    assert ref.snapshot.read_text(encoding="utf-8") == before


def test_an_unreachable_model_refines_nothing(workspace):
    class Broken:
        def ask(self, system, user, *, max_tokens=4096):
            raise RuntimeError("no provider")

    path = spec_path(workspace, "research")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(RefinementRefused, match="could not be reached"):
        refine(workspace, "research", "action #6", "tighten it", Broken())
    assert path.read_text(encoding="utf-8") == before
