"""The learning loop — what RevenueOS learned, and how a role's spec changes because of it.

Two files, both append-only, both plain markdown a human can read and edit:

  * `learning-loop/LESSONS.md`     — one dated block per measured result, newest first, never
    deleted. The Attempt / Result / Evidence lines are read off the store (action type,
    executor, metric, before → after, status, note); only Why / Lesson are the model's, and
    when there is no model they say so. `ctx.prompt_summary()` injects the recent ones next to
    the corrections, so every worker prompt carries them.
  * `learning-loop/REFINEMENTS.md` — one line per change to a role spec, with the evidence that
    justified it and the snapshot taken before it.

A refinement is deliberately small: a dated bullet under `## Refinements`, or — with a model —
one rewritten section, and never more than ~20 changed lines. The spec's first section
(`## Purpose`, which carries the hard rules) is immutable: an edit that touches it is refused.
Every accepted refinement is snapshotted first, so `rollback` restores the previous spec
byte-for-byte.

Nothing here invents a metric. A lesson with no measured movement says `not observed`.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .paths import Workspace
from .roles import ROLES, spec_path
from .store import Store

MAX_DIFF_LINES = 20
NO_MODEL_WHY = "not analysed (no model)"
NO_MODEL_LESSON = "pending analysis"
LESSON_STATUSES = ("measured", "no_effect", "unmeasurable")

LESSONS_HEADER = """# LESSONS.md — what RevenueOS learned from measured results

One block per executed action that has been measured. Append new lessons to the TOP. Never
delete one: a lesson that turned out to be wrong is corrected by a newer lesson above it.
Attempt, Result and Evidence are read from the store and are never edited by hand; Why and
Lesson are the analysis. `ctx.prompt_summary()` injects the recent blocks into every prompt.

"""

REFINEMENTS_HEADER = """# REFINEMENTS.md — every change to a role spec, and why

One line per refinement, oldest first. Each names the evidence that justified the change and
the snapshot taken immediately before it (`revenueos refine <role> --rollback` restores it).
The `## Purpose` section of a spec is immutable and is never refined.

"""

FIELDS = ("Attempt", "Result", "Evidence", "What worked", "What failed", "Why", "Lesson", "Apply when")


class RefinementRefused(RuntimeError):
    """A proposed spec edit that RevenueOS will not apply (immutable section, or too large)."""


@dataclass
class Refinement:
    role: str
    snapshot: Path
    diff_lines: int
    summary: str
    evidence: str
    mode: str  # "append" | "model"

    def as_json(self) -> dict[str, Any]:
        return {"role": self.role, "snapshot": str(self.snapshot), "diff_lines": self.diff_lines,
                "summary": self.summary, "evidence": self.evidence, "mode": self.mode}


# ── paths ───────────────────────────────────────────────────────────────────
def lessons_path(ws: Workspace) -> Path:
    return ws.learning_loop / "LESSONS.md"


def refinements_path(ws: Workspace) -> Path:
    return ws.learning_loop / "REFINEMENTS.md"


def snapshots_dir(ws: Workspace) -> Path:
    return ws.learning_loop / "snapshots"


# ── lessons ─────────────────────────────────────────────────────────────────
def add_lesson(ws: Workspace, title: str, *, attempt: str, result: str, evidence: str,
               what_worked: str = "not observed", what_failed: str = "not observed",
               why: str = NO_MODEL_WHY, lesson: str = NO_MODEL_LESSON, apply_when: str = "") -> Path:
    """Append-to-top, same block shape as CORRECTIONS.md. Nothing is ever removed."""
    path = lessons_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else LESSONS_HEADER
    values = (attempt, result, evidence, what_worked, what_failed, why, lesson, apply_when or "not stated")
    block = f"## {datetime.now(UTC):%Y-%m-%d} — {title.strip()}\n" + "".join(
        f"{name}: {_one_line(value)}\n" for name, value in zip(FIELDS, values, strict=True)) + "\n"
    head, sep, tail = existing.partition("\n## ")
    path.write_text(head.rstrip("\n") + "\n\n" + block + (sep + tail if sep else ""), encoding="utf-8")
    return path


def recent_lessons(ws: Workspace, days: int = 60, max_chars: int = 2500) -> str:
    """The lesson blocks from the last `days`, newest first — the same window rule corrections use."""
    path = lessons_path(ws)
    if not path.exists():
        return ""
    blocks = re.split(r"(?m)^## (?=\d{4}-\d{2}-\d{2})", path.read_text(encoding="utf-8"))[1:]
    cutoff = datetime.now(UTC).timestamp() - days * 86400
    keep = []
    for b in blocks:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", b)
        if m and datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=UTC).timestamp() >= cutoff:
            keep.append("## " + b.strip())
    return "\n\n".join(keep)[:max_chars]


def has_lesson_for(ws: Workspace, action_id: int) -> bool:
    path = lessons_path(ws)
    return bool(path.exists() and re.search(rf"action #{action_id}\b", path.read_text(encoding="utf-8")))


def _movement(outcome: dict[str, Any]) -> str:
    """'meta_description_fixed 0 → 1', or the metric alone, or nothing. Never a computed number."""
    metric = (outcome.get("metric") or "").strip()
    before, after = outcome.get("before_value"), outcome.get("after_value")
    if metric and before is not None and after is not None:
        return f"{metric} {_num(before)} → {_num(after)}"
    return metric


def lessons_from_outcomes(ws: Workspace, store: Store, llm: Any = None) -> int:
    """One lesson per measured/no_effect/unmeasurable outcome on an executed action, once.
    Idempotent: an action already named in LESSONS.md is skipped. Returns how many were written."""
    written = 0
    for action in reversed(store.executed_actions(limit=500)):  # oldest first, so the newest ends up on top
        outcome = action.get("outcome")
        if not outcome or outcome.get("status") not in LESSON_STATUSES:
            continue
        if has_lesson_for(ws, action["id"]):
            continue
        facts = _lesson_facts(action, outcome)
        analysis = _analyse(facts, llm)
        add_lesson(ws, facts["title"], attempt=facts["attempt"], result=facts["result"], evidence=facts["evidence"],
                   what_worked=facts["what_worked"], what_failed=facts["what_failed"], **analysis)
        written += 1
    return written


def _lesson_facts(action: dict[str, Any], outcome: dict[str, Any]) -> dict[str, str]:
    executor = (action.get("context") or {}).get("executor") or "unknown executor"
    status = outcome.get("status") or "unknown"
    movement = _movement(outcome)
    note = (outcome.get("note") or "").strip()
    result = f"{status}" + (f" — {movement}" if movement else "") + (f" — {note}" if note else "")
    evidence = f"action #{action['id']} · measured {outcome.get('measured_at') or 'at an unrecorded time'}"
    if movement:
        evidence += f" · {movement}"
    if status == "measured":
        worked, failed = (movement or "a change was recorded without a named metric"), "not observed"
    elif status == "no_effect":
        worked = "not observed"
        failed = (f"{movement} — no movement" if movement else "the action produced no measurable change")
    else:
        worked = "not observed"
        failed = note or "no evidence available to measure this action"
    return {"title": f"{action['action_type']} — {action['title']}"[:90], "attempt": f"{action['action_type']} via {executor}: {action['title']}",
            "result": result, "evidence": evidence, "what_worked": worked, "what_failed": failed,
            "action_type": action["action_type"]}


ANALYSE_SYSTEM = """You write the analysis half of one RevenueOS lesson.

You are given the facts of one executed action and what was measured afterwards. The facts are
final — you may not restate them differently, and you may NOT introduce any number, metric,
date, customer or claim that is not in them. If the facts do not explain why the result
happened, say exactly that.

Answer with one JSON object and nothing else:
{"why": "one sentence: the most likely reason this result occurred, or 'cannot determine from the record'",
 "lesson": "one sentence, imperative: what RevenueOS should do differently or keep doing",
 "apply_when": "the trigger condition for that lesson"}"""


def _analyse(facts: dict[str, str], llm: Any) -> dict[str, str]:
    fallback = {"why": NO_MODEL_WHY, "lesson": NO_MODEL_LESSON,
                "apply_when": f"deciding another {facts['action_type']} action"}
    if llm is None:
        return fallback
    user = "\n".join(f"{k}: {facts[k]}" for k in ("attempt", "result", "evidence", "what_worked", "what_failed"))
    try:
        raw = llm.ask(ANALYSE_SYSTEM, user, max_tokens=600)
        match = re.search(r"\{.*\}", raw or "", re.S)
        data = json.loads(match.group(0)) if match else {}
    except Exception:
        return {**fallback, "why": "not analysed (model unavailable)"}
    if not isinstance(data, dict) or not str(data.get("why") or "").strip():
        return {**fallback, "why": "not analysed (model answer did not parse)"}
    return {"why": _one_line(data.get("why")), "lesson": _one_line(data.get("lesson")) or NO_MODEL_LESSON,
            "apply_when": _one_line(data.get("apply_when")) or fallback["apply_when"]}


# ── refinement ──────────────────────────────────────────────────────────────
def immutable_head(text: str) -> str:
    """The preamble plus the first section (`## Purpose` and its hard rules) — never editable."""
    parts = re.split(r"(?m)^(?=## )", text)
    head = "".join(parts[:2]) if len(parts) > 1 else text
    return "\n".join(line.rstrip() for line in head.strip().splitlines())


def diff_size(before: str, after: str) -> int:
    return sum(1 for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
               if line[:1] in "+-" and not line.startswith(("+++", "---")))


REFINE_SYSTEM = """You revise one RevenueOS role spec, minimally.

You are given the current spec, a piece of evidence, and the change that evidence justifies.
Rules:
- Rewrite AT MOST one section, and change as few lines as possible — under 20 changed lines in total.
- The first section (`## Purpose`, including its hard rules) is immutable. Reproduce it byte for byte.
- Keep every other heading, and the output JSON contract, intact unless the change is about it.
- Never weaken a rule about evidence, invented numbers, or "cannot determine".
Answer with the complete revised spec as markdown and nothing else — no fence, no commentary."""


def _append_refinement(text: str, change: str, evidence: str) -> str:
    bullet = f"- {datetime.now(UTC):%Y-%m-%d} — {_one_line(change)} (evidence: {_one_line(evidence)})"
    if not re.search(r"(?m)^## Refinements\s*$", text):
        return text.rstrip("\n") + f"\n\n## Refinements\n{bullet}\n"
    body = re.sub(r"(?m)^_\(none yet[^\n]*\n", "", text)  # the placeholder goes when the first real one lands
    return body.rstrip("\n") + f"\n{bullet}\n"


def refine(ws: Workspace, role: str, evidence: str, change: str, llm: Any = None) -> Refinement:
    """Apply one small, evidence-backed edit to a role spec, snapshotting it first."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
    if not str(evidence or "").strip():
        raise RefinementRefused("a refinement needs evidence; say what was observed")
    path = spec_path(ws, role)
    if not path.exists():
        raise FileNotFoundError(f"no spec for role {role!r} (expected {path})")
    before = path.read_text(encoding="utf-8")

    mode = "append"
    after = _append_refinement(before, change, evidence)
    if llm is not None:
        user = f"EVIDENCE\n{evidence}\n\nCHANGE\n{change}\n\nCURRENT SPEC\n{before}"
        try:
            proposed = (llm.ask(REFINE_SYSTEM, user, max_tokens=4000) or "").strip()
        except Exception as exc:
            raise RefinementRefused(f"the model could not be reached: {type(exc).__name__}: {exc}")
        if proposed:
            after, mode = proposed.rstrip("\n") + "\n", "model"

    if after == before:
        raise RefinementRefused("that change would not alter the spec")
    if immutable_head(after) != immutable_head(before):
        raise RefinementRefused(f"refused: the '## Purpose' section of {role}.md is immutable (it carries the hard rules)")
    lines = diff_size(before, after)
    if lines > MAX_DIFF_LINES:
        raise RefinementRefused(f"refused: {lines} changed lines is not a refinement (cap {MAX_DIFF_LINES}); make a smaller change")

    snapshot = _write_snapshot(ws, role, before)
    path.write_text(after, encoding="utf-8")
    _log_refinement(ws, f"{role} · evidence: {_one_line(evidence)} · change: {_one_line(change)} "
                        f"· {lines} changed line(s), {mode} · snapshot: {snapshot.name}")
    return Refinement(role=role, snapshot=snapshot, diff_lines=lines, summary=_one_line(change),
                      evidence=_one_line(evidence), mode=mode)


def list_snapshots(ws: Workspace, role: str | None = None) -> list[Path]:
    d = snapshots_dir(ws)
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.md") if role is None or p.name.endswith(f"-{role}.md"))


def rollback(ws: Workspace, role: str, snapshot: str | None = None) -> Path:
    """Restore the latest (or a named) snapshot of a role spec, byte for byte."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
    candidates = list_snapshots(ws, role)
    if snapshot:
        name = Path(snapshot).name
        chosen = next((p for p in candidates if p.name == name), None)
        if chosen is None:
            raise FileNotFoundError(f"no snapshot {name!r} for role {role!r}")
    else:
        if not candidates:
            raise FileNotFoundError(f"no snapshot to roll {role!r} back to")
        chosen = candidates[-1]
    path = spec_path(ws, role)
    path.write_bytes(chosen.read_bytes())
    _log_refinement(ws, f"{role} · ROLLBACK to snapshot: {chosen.name}")
    return chosen


def _write_snapshot(ws: Workspace, role: str, text: str) -> Path:
    d = snapshots_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = d / f"{stamp}-{role}.md"
    n = 1
    while path.exists():  # two refinements in the same second still each keep their own snapshot
        path = d / f"{stamp}-{n}-{role}.md"
        n += 1
    path.write_text(text, encoding="utf-8")
    return path


def _log_refinement(ws: Workspace, line: str) -> None:
    path = refinements_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else REFINEMENTS_HEADER
    path.write_text(existing.rstrip("\n") + f"\n- {datetime.now(UTC).isoformat(timespec='seconds')} · {line}\n", encoding="utf-8")


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _num(value: Any) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f.is_integer() else f"{f:g}"
