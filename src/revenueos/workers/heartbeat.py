"""HEARTBEAT — the pulse that makes RevenueOS look continuously at work.

Every thirty minutes the orchestrator runs this worker (`data/automations.json` → `revenueos run
heartbeat --json`). It sends nothing, publishes nothing and spends nothing: it reads the state the
other workers already wrote — opportunities waiting for a decision, decisions waiting to run,
measured results, failed runs, the leads funnel, what is externally blocked — decides the single
next action from `today.rank_next` and the objective's own plan, appends the trail to every active
objective (`objective_events`) and leaves a note for the operator (`messages`) only when a human
is actually needed.

Everything it does is deterministic and works with `llm=None`. When a model *is* available and the
other engineer's `roles` module is installed, one role run per heartbeat may be dispatched for an
open question (an objective with no strategy); that is strictly optional and never blocks the run.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from ..context import BusinessContext
from ..llm import LLM
from ..paths import Workspace
from ..store import Store
from ..today import rank_next
from . import WorkerResult

STALE_APPROVAL_HOURS = 24
FAILURE_RUNS_SCANNED = 30

NO_MAILBOX = "no mailbox configured (SMTP_PASSWORD + smtp.host) — the draft is ready, the send is not"


def classify_error(text: str) -> str:
    """Three classes, because that is what changes what the operator should do about it."""
    t = (text or "").lower()
    if "llmunavailable" in t or "rate limit" in t or "rate_limit" in t or "429" in t or "overloaded" in t:
        return "model unavailable"
    if any(k in t for k in ("connecterror", "connectionerror", "connecttimeout", "readtimeout", "timeout",
                            "temporary failure in name resolution", "getaddrinfo", "dns", "ssl", "socket",
                            "network is unreachable", "remote end closed")):
        return "network"
    return "worker failure"


def _mailbox_ready() -> bool:
    return bool(os.environ.get("SMTP_PASSWORD")) or os.environ.get("REVENUEOS_DRY_RUN") == "1"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def read_state(store: Store) -> dict[str, Any]:
    """Everything the heartbeat knows, as plain JSON-serialisable data."""
    pending = store.list_actions("pending")
    approved = store.list_actions("approved")
    executed = store.executed_actions(limit=1000)
    now_ = datetime.now(UTC)

    blocked = []
    for a in pending:
        c = a.get("context") or {}
        if c.get("executor") == "send_email" and (not c.get("to") or not _mailbox_ready()):
            blocked.append({"action_id": a["id"], "title": a["title"],
                            "why": "no recipient address on the draft" if not c.get("to") else NO_MAILBOX})

    stale = [{"action_id": a["id"], "title": a["title"], "approved_at": a.get("decided_at")}
             for a in approved
             if (d := _parse(a.get("decided_at"))) is not None and now_ - d > timedelta(hours=STALE_APPROVAL_HOURS)]

    measured = [{"action_id": a["id"], "title": a["title"], "action_type": a["action_type"],
                 "metric": (a["outcome"] or {}).get("metric"),
                 "before_value": (a["outcome"] or {}).get("before_value"),
                 "after_value": (a["outcome"] or {}).get("after_value"),
                 "note": (a["outcome"] or {}).get("note")}
                for a in executed if (a.get("outcome") or {}).get("status") == "measured"]
    awaiting = [a["id"] for a in executed if (a.get("outcome") or {}).get("status") in (None, "pending", "in_progress")]

    failures = []
    for r in store.list_runs(limit=FAILURE_RUNS_SCANNED):
        if r.get("status") == "failed":
            text = r.get("summary") or ""
            failures.append({"worker": r.get("kind"), "at": r.get("finished_at") or r.get("started_at"),
                             "error": text[:300], "error_class": classify_error(text)})

    ranked = rank_next(store, limit=1)
    top = None
    if ranked:
        r = ranked[0]
        top = {"action_id": r["id"], "action_type": r["action_type"], "title": r["title"],
               "score": r["score"], "blocked": r.get("blocked")}

    return {
        "objectives": [{"id": o["id"], "title": o["title"], "status": o["status"],
                        "strategy": o.get("strategy"), "next_action": o.get("next_action")}
                       for o in store.list_objectives("active")],
        "pending_by_type": store.counts_by_type("pending"),
        "pending_total": len(pending),
        "approved_waiting": [{"action_id": a["id"], "title": a["title"]} for a in approved],
        "stale_approvals": stale,
        "measured": measured,
        "awaiting_measurement": awaiting,
        "results": store.results_summary(),
        "funnel": store.lead_funnel(),
        "failures": failures,
        "blocked_sends": blocked,
        "top_next": top,
    }


def decide_next(state: dict[str, Any], objective: dict[str, Any] | None) -> str:
    """One sentence: what RevenueOS says to do next, and what unblocks it if it cannot be done."""
    top = state.get("top_next")
    if state["approved_waiting"]:
        a = state["approved_waiting"][0]
        return f"execute the approved action [{a['action_id']}] {a['title'][:70]} — revenueos execute {a['action_id']}"
    if top:
        line = f"approve [{top['action_id']}] {top['title'][:70]} ({top['action_type'].replace('_', ' ')})"
        if top.get("blocked"):
            return f"{line} — blocked: {top['blocked']}"
        return line
    if objective and objective.get("next_action"):
        return str(objective["next_action"])
    if objective and not objective.get("strategy"):
        return f"decide the strategy for objective [{objective['id']}] — revenueos objective set {objective['id']} --strategy \"…\""
    return "nothing is waiting for a decision — run `revenueos run all` to look for new opportunities"


def summarise(state: dict[str, Any], next_line: str) -> str:
    bits = [f"{state['pending_total']} pending"]
    if state["approved_waiting"]:
        bits.append(f"{len(state['approved_waiting'])} approved waiting")
    bits.append(f"{len(state['measured'])} measured")
    if state["awaiting_measurement"]:
        bits.append(f"{len(state['awaiting_measurement'])} awaiting measurement")
    if state["failures"]:
        classes = sorted({f["error_class"] for f in state["failures"]})
        bits.append("failing: " + ", ".join(classes))
    bits.append(f"next: {next_line}")
    if state["blocked_sends"]:
        bits.append(f"blocked: {state['blocked_sends'][0]['why']}")
    return " · ".join(bits)


def _record_results(store: Store, objective_id: int, state: dict[str, Any]) -> int:
    """One 'result' event per measured outcome, deduped by ref action_id so re-runs add nothing."""
    seen = {e["ref"].get("action_id") for e in store.list_objective_events(objective_id, limit=1000, kind="result")}
    added = 0
    for m in state["measured"]:
        if m["action_id"] in seen:
            continue
        if m.get("metric") and m.get("after_value") is not None:
            what = f"{m['metric']} {m.get('before_value') or 0:g} → {m['after_value']:g}"
        else:
            what = m.get("note") or "measured"
        store.add_objective_event(objective_id, "result", f"[{m['action_id']}] {m['title'][:80]} — {what}",
                                  {"action_id": m["action_id"], "action_type": m["action_type"]})
        added += 1
    return added


def _record_failures(store: Store, objective_id: int, state: dict[str, Any]) -> list[str]:
    """One 'failure' event per distinct (worker, error class) per day. Returns the classes new today."""
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    seen = {(e["ref"].get("worker"), e["ref"].get("error_class"), e["ref"].get("day"))
            for e in store.list_objective_events(objective_id, limit=1000, kind="failure")}
    new_classes = []
    for f in state["failures"]:
        key = (f["worker"], f["error_class"], day)
        if key in seen:
            continue
        seen.add(key)
        store.add_objective_event(objective_id, "failure", f"{f['worker']}: {f['error_class']} — {f['error'][:160]}",
                                  {"worker": f["worker"], "error_class": f["error_class"], "day": day})
        new_classes.append(f"{f['worker']}: {f['error_class']}")
    return new_classes


def _notify(store: Store, state: dict[str, Any], next_line: str, new_failures: list[str]) -> int:
    """A note for the operator only when a human is needed. Deduped against unread notes with the
    same key, so a heartbeat every 30 minutes does not bury the inbox in copies."""
    unread_keys = {m["ref"].get("key") for m in store.list_messages("operator", unread_only=True, limit=200)}
    posted = 0

    def post(key: str, subject: str, body: str, ref: dict[str, Any]) -> None:
        nonlocal posted
        if key in unread_keys:
            return
        store.post_message("heartbeat", "operator", subject, body, {"key": key, **ref})
        unread_keys.add(key)
        posted += 1

    waiting = state["approved_waiting"]
    if waiting:
        ids = ", ".join(str(a["action_id"]) for a in waiting[:10])
        stale = len(state["stale_approvals"])
        post(f"approved-waiting:{len(waiting)}",
             f"{len(waiting)} approved action(s) waiting to run",
             f"You said yes; nothing has run yet. Execute them: revenueos execute <id> ({ids})."
             + (f"\n{stale} of them were approved more than {STALE_APPROVAL_HOURS}h ago." if stale else "")
             + f"\nNext: {next_line}",
             {"action_ids": [a["action_id"] for a in waiting[:10]]})
    if state["blocked_sends"]:
        b = state["blocked_sends"][0]
        post("blocked-send",
             f"{len(state['blocked_sends'])} outreach draft(s) cannot be sent",
             f"{b['why']}.\nAdd smtp.host to revenueos.yaml and SMTP_PASSWORD to the environment, "
             "or set REVENUEOS_DRY_RUN=1 to write the emails to data/outputs/ instead.",
             {"action_ids": [b["action_id"] for b in state["blocked_sends"][:10]]})
    for label in new_failures:
        worker, _, klass = label.partition(": ")
        post(f"failure:{worker}:{klass}:{datetime.now(UTC):%Y-%m-%d}",
             f"{worker} is failing ({klass})",
             f"The {worker} worker failed and RevenueOS classified it as {klass}. "
             "Run `revenueos run " + str(worker) + "` to see the error, or `revenueos doctor`.",
             {"worker": worker, "error_class": klass})
    return posted


def _maybe_role_run(ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None,
                    objective: dict[str, Any], run_id: int) -> str | None:
    """At most one role run per heartbeat, and only when `roles` is installed, a model is available
    and there is an open question worth it. Never a hard dependency — a missing or differently
    shaped `roles` module just means the heartbeat stays deterministic."""
    if llm is None or objective.get("strategy"):
        return None
    try:  # the roles module is owned by another part of the system and may not exist
        import inspect

        from .. import roles  # type: ignore[attr-defined]
    except Exception:
        return None
    fn = getattr(roles, "run_role", None)
    if not callable(fn):
        return None
    question = (f"Objective [{objective['id']}] \"{objective['title']}\" has no strategy. "
                "Propose one: the specific businesses or surfaces to work on, and the first measurable result to aim for.")
    pool = {"ws": ws, "workspace": ws, "store": store, "ctx": ctx, "context": ctx, "llm": llm, "run_id": run_id,
            "role": "strategist", "name": "strategist", "question": question, "prompt": question, "task": question,
            "objective_id": objective["id"]}
    try:
        params = inspect.signature(fn).parameters
        kwargs = {k: v for k, v in pool.items() if k in params}
        out = fn(**kwargs)
    except Exception:
        return None
    text = str(getattr(out, "summary", None) or out or "").strip()
    if not text:
        return None
    store.add_objective_event(objective["id"], "lesson", f"strategist: {text[:400]}", {"via": "roles.run_role"})
    return text[:200]


class HeartbeatWorker:
    name = "heartbeat"
    description = "Check objectives, opportunities, approvals, measurements and failures; decide the next action; tell the operator when a human is needed."
    upstream = "RevenueOS (Prime Agent's persistent goals + heartbeat, adapted to the store and the orchestrator)"

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult:
        state = read_state(store)
        objectives = state["objectives"]
        primary = objectives[0] if objectives else None
        next_line = decide_next(state, primary)

        results_added, failures_new, role_note = 0, [], None
        for o in objectives:
            results_added += _record_results(store, o["id"], state)
            for label in _record_failures(store, o["id"], state):
                if label not in failures_new:
                    failures_new.append(label)

        summary = summarise(state, next_line)

        for o in objectives:
            store.add_objective_event(o["id"], "heartbeat", summary, {"run_id": run_id})
            store.update_objective(o["id"], next_action=next_line)
        if primary:
            role_note = _maybe_role_run(ws, store, ctx, llm, primary, run_id)

        messages_posted = _notify(store, state, next_line, failures_new)

        state = {**state, "next_action": next_line, "result_events_added": results_added,
                 "failure_events_added": failures_new, "messages_posted": messages_posted,
                 "objectives_updated": [o["id"] for o in objectives],
                 "role_run": role_note}
        if not objectives:
            summary = summary + " · no objective set (revenueos objective add \"…\")"
        return WorkerResult(ok=True, summary=summary, actions_created=0, details=state)
