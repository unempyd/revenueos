"""Workers are the department. Each one turns vendored upstream code into TODAY actions.

Contract (the same shape the orchestrator's headless runner expects back as JSON):
    result = worker.run(ws, store, ctx, llm, run_id)   -> WorkerResult
Every worker must be safe to run unattended, idempotent (use dedupe keys), and must never
send, publish or spend money — that only happens in `execute_action`, after a human
approved the action (Approve / Execute / Ignore).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..context import BusinessContext
from ..llm import LLM
from ..paths import Workspace
from ..store import Store


@dataclass
class WorkerResult:
    ok: bool
    summary: str
    actions_created: int = 0
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "summary": self.summary, "actions_created": self.actions_created,
                "error": self.error, **({"details": self.details} if self.details else {})}


class Worker(Protocol):
    name: str
    description: str
    upstream: str  # which vendored code this worker is built on

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult: ...


def all_workers() -> dict[str, Worker]:
    from .ads_audit import AdsAuditWorker
    from .content import ContentWorker
    from .discover import DiscoverWorker
    from .growth import GrowthWorker
    from .heartbeat import HeartbeatWorker
    from .inbox import InboxWorker
    from .intake import IntakeWorker
    from .live import AdsLiveWorker, AnalyticsWorker, BillingWorker
    from .measure import MeasureWorker
    from .monitor import MonitorWorker
    from .outreach import OutreachWorker
    from .seo import SeoWorker

    workers: list[Worker] = [DiscoverWorker(), OutreachWorker(), InboxWorker(), IntakeWorker(), SeoWorker(), AdsAuditWorker(), ContentWorker(), MonitorWorker(),
                             MeasureWorker(), GrowthWorker(), BillingWorker(), AnalyticsWorker(), AdsLiveWorker(),
                             HeartbeatWorker()]
    return {w.name: w for w in workers}


def run_worker(name: str, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, workflow: str = "manual") -> WorkerResult:
    worker = all_workers().get(name)
    if worker is None:
        return WorkerResult(ok=False, summary="", error=f"unknown worker {name!r}; known: {sorted(all_workers())}")
    run_id = store.start_run(name, workflow)
    try:
        result = worker.run(ws, store, ctx, llm, run_id)
    except Exception as exc:  # the orchestrator classifies transient vs. real failures from this text
        result = WorkerResult(ok=False, summary="", error=f"{type(exc).__name__}: {exc}")
    store.finish_run(run_id, result.ok, result.summary or (result.error or ""))
    return result


def refused(outcome: str) -> bool:
    """An executor that could not or would not act says so in a sentence beginning "not …" ("not sent: …",
    "not deployed: …", "not paused: …", "not booked: …"). Such an action was NOT executed and must keep its
    status; the approval surfaces check this before stamping `executed`."""
    return (outcome or "").strip().lower().startswith("not ")


def execute_action(ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, action: dict[str, Any]) -> str:
    """Perform an approved action. Dispatch on the executor the creating worker recorded."""
    executor = (action.get("context") or {}).get("executor")
    from .content import execute_content
    from .executors import EXECUTORS
    from .intake import execute_record_correction
    from .outreach import execute_send

    table = {"send_email": execute_send, "run_skill": execute_content,
             "record_correction": execute_record_correction, **EXECUTORS}
    fn = table.get(executor)
    if fn is None:
        return f"action {action['id']} has no automatic executor ({executor!r}); do it by hand and mark it executed."
    return fn(ws, store, ctx, llm, action)
