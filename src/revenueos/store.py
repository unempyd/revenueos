"""RevenueOS business state — one SQLite file, no ORM.

Schema lineage (kept close to the originals so upstream code keeps working):
  * `actions`, `runs`             ← pulse-cmo  src/pulse/store/actions.py   (the TODAY list)
  * `leads`, `email_drafts`,
    `email_sends`, `lead_events`,
    `unsubscribes`                ← ai-sales-agent migrations/0001_init_schema.sql, translated
                                     Postgres→SQLite (UUID→TEXT, enums→CHECK, no pgvector).
The original Postgres DDL ships verbatim in vendor/sales_agent/postgres_schema.sql for
deployments that outgrow SQLite.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .qualify import QUALIFIED_STATUSES

ACTION_STATUSES = ("pending", "approved", "executed", "ignored", "failed")
ACTION_TYPES = (
    "prospect",            # qualified prospects found
    "follow_up",           # follow-ups ready (drafted outreach awaiting approval / reply follow-ups)
    "campaign_attention",  # campaigns needing attention
    "seo_opportunity",
    "content_opportunity",
    "ad_waste",            # an ad wasting money
    "market_signal",       # HN / web conversations worth joining
    "correction",          # learning-loop candidates
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                 -- worker name
    workflow TEXT,                      -- automation name (from the orchestrator) or 'manual'
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,               -- 'running' | 'done' | 'failed'
    summary TEXT,
    log TEXT                            -- json array of events
);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    action_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    context TEXT,                       -- json: worker-specific payload (skill, lead_id, draft_id, ...)
    detail_md TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    executed_at TEXT,
    source_url TEXT,
    dedupe_key TEXT UNIQUE,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status, action_type);

CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT,
    business_name TEXT NOT NULL,
    first_name TEXT,
    last_name TEXT,
    title TEXT,
    address TEXT,
    city TEXT,
    region TEXT,
    country TEXT,
    phone TEXT,
    website_url TEXT,
    linkedin_url TEXT,
    instagram_handle TEXT,
    contact_email TEXT,
    contact_email_source TEXT,
    contact_email_verified INTEGER NOT NULL DEFAULT 0,
    reason TEXT,                        -- why this lead qualifies (OpenOutreach's `reason` column)
    score INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN ('new','enriched','scored','drafted','sent','opened','replied','booked','paused','dead')),
    deal_value REAL,
    paused_at TEXT,
    paused_reason TEXT,
    notes TEXT,
    UNIQUE (source, source_id)
);

CREATE TABLE IF NOT EXISTS email_drafts (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    lead_id TEXT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    recipe_key TEXT NOT NULL,
    subject_variant TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    model TEXT NOT NULL,
    approval_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (approval_state IN ('pending','approved','rejected','edited','superseded')),
    approved_by TEXT,
    approved_at TEXT,
    edited_subject TEXT,
    edited_body TEXT
);

CREATE TABLE IF NOT EXISTS email_sends (
    id TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL,
    draft_id TEXT NOT NULL REFERENCES email_drafts(id),
    lead_id TEXT NOT NULL REFERENCES leads(id),
    to_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_message_id TEXT,
    follow_up_seq INTEGER NOT NULL DEFAULT 0,
    opened_first_at TEXT,
    replied_at TEXT,
    bounced INTEGER NOT NULL DEFAULT 0,
    unsubscribed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lead_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    lead_id TEXT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    payload TEXT
);

CREATE TABLE IF NOT EXISTS unsubscribes (
    email TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    via TEXT NOT NULL CHECK (via IN ('reply_stop','unsub_link','manual'))
);

CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER NOT NULL REFERENCES actions(id),
    measured_at TEXT NOT NULL,
    status TEXT NOT NULL,               -- 'pending' | 'in_progress' | 'measured' | 'no_effect' | 'unmeasurable'
    metric TEXT,                        -- e.g. 'replies', 'meta_description_fixed', 'campaign_spend_delta'
    before_value REAL,
    after_value REAL,
    before_json TEXT,
    after_json TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_outcomes_action ON outcomes(action_id, id);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    name TEXT NOT NULL,
    value REAL NOT NULL,
    dims TEXT                            -- json
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── runs ────────────────────────────────────────────────────────────────
    def start_run(self, kind: str, workflow: str = "manual") -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO runs (kind, workflow, started_at, status, log) VALUES (?,?,?,?,?)",
                (kind, workflow, now(), "running", "[]"),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, ok: bool, summary: str, log: list[dict[str, Any]] | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, status=?, summary=?, log=? WHERE id=?",
                (now(), "done" if ok else "failed", summary, json.dumps(log or []), run_id),
            )

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]

    # ── actions (the TODAY list) ────────────────────────────────────────────
    def create_action(
        self,
        action_type: str,
        title: str,
        content: str,
        *,
        run_id: int | None = None,
        context: dict[str, Any] | None = None,
        detail_md: str | None = None,
        source_url: str | None = None,
        dedupe_key: str | None = None,
    ) -> int | None:
        """Returns the new id, or None when dedupe_key already exists (idempotent workers)."""
        if action_type not in ACTION_TYPES:
            raise ValueError(f"unknown action_type {action_type!r}; expected one of {ACTION_TYPES}")
        with self._conn() as c:
            if dedupe_key and c.execute("SELECT 1 FROM actions WHERE dedupe_key=?", (dedupe_key,)).fetchone():
                return None
            cur = c.execute(
                "INSERT INTO actions (run_id, action_type, title, content, context, detail_md, status, created_at, source_url, dedupe_key)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, action_type, title, content, json.dumps(context or {}), detail_md, "pending", now(), source_url, dedupe_key),
            )
            return int(cur.lastrowid)

    def get_action(self, action_id: int) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            return self._hydrate_action(row) if row else None

    def list_actions(self, status: str | None = "pending", action_type: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM actions", []
        clauses = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if action_type:
            clauses.append("action_type=?")
            params.append(action_type)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            return [self._hydrate_action(r) for r in c.execute(sql, params)]

    def set_action_status(self, action_id: int, status: str) -> None:
        if status not in ACTION_STATUSES:
            raise ValueError(f"unknown status {status!r}")
        col = "executed_at" if status in ("executed", "failed") else "decided_at"
        with self._conn() as c:
            c.execute(f"UPDATE actions SET status=?, {col}=? WHERE id=?", (status, now(), action_id))

    def counts_by_type(self, status: str = "pending") -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute("SELECT action_type, COUNT(*) n FROM actions WHERE status=? GROUP BY action_type", (status,))
            return {r["action_type"]: r["n"] for r in rows}

    @staticmethod
    def _hydrate_action(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        try:
            d["context"] = json.loads(d.get("context") or "{}")
        except json.JSONDecodeError:
            d["context"] = {}
        return d

    # ── leads / pipeline ────────────────────────────────────────────────────
    def upsert_lead(self, source: str, source_id: str | None, business_name: str, **fields: Any) -> str:
        """Idempotent on (source, source_id) like ai-sales-agent's LeadRepo.upsert."""
        with self._conn() as c:
            existing = None
            if source_id:
                existing = c.execute("SELECT id, business_name FROM leads WHERE source=? AND source_id=?", (source, source_id)).fetchone()
            cols = {k: v for k, v in fields.items() if v is not None}
            if existing:
                if business_name and business_name != existing["business_name"]:
                    cols["business_name"] = business_name  # e.g. '{acmeAI}' → 'acmeAI' after the gate's cleaning
                if cols:
                    sets = ", ".join(f"{k}=?" for k in cols)
                    c.execute(f"UPDATE leads SET {sets}, updated_at=? WHERE id=?", (*cols.values(), now(), existing["id"]))
                return str(existing["id"])
            lead_id = str(uuid.uuid4())
            cols.update({"id": lead_id, "created_at": now(), "updated_at": now(), "source": source,
                         "source_id": source_id, "business_name": business_name})
            c.execute(f"INSERT INTO leads ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(cols.values()))
            c.execute("INSERT INTO lead_events (at, lead_id, kind, payload) VALUES (?,?,?,?)", (now(), lead_id, "created", None))
            return lead_id

    def upsert_lead_fields(self, lead_id: str, **fields: Any) -> None:
        cols = {k: v for k, v in fields.items() if v is not None}
        if not cols:
            return
        with self._conn() as c:
            sets = ", ".join(f"{k}=?" for k in cols)
            c.execute(f"UPDATE leads SET {sets}, updated_at=? WHERE id=?", (*cols.values(), now(), lead_id))

    def get_lead(self, lead_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
            return dict(row) if row else None

    def list_leads(self, status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        with self._conn() as c:
            if status:
                rows = c.execute("SELECT * FROM leads WHERE status=? ORDER BY score DESC, created_at DESC LIMIT ?", (status, limit))
            else:
                rows = c.execute("SELECT * FROM leads ORDER BY score DESC, created_at DESC LIMIT ?", (limit,))
            return [dict(r) for r in rows]

    def transition_lead(self, lead_id: str, status: str, payload: dict[str, Any] | None = None) -> None:
        """Invariant (same standard as outcomes): a lead without a contact email can never hold a
        qualified status — 'scored' or anything downstream of it."""
        with self._conn() as c:
            if status in QUALIFIED_STATUSES:
                row = c.execute("SELECT contact_email FROM leads WHERE id=?", (lead_id,)).fetchone()
                if not row or not (row["contact_email"] or "").strip():
                    raise ValueError(f"lead {lead_id} has no contact email and cannot be {status!r}")
            c.execute("UPDATE leads SET status=?, updated_at=? WHERE id=?", (status, now(), lead_id))
            c.execute("INSERT INTO lead_events (at, lead_id, kind, payload) VALUES (?,?,?,?)",
                      (now(), lead_id, f"status:{status}", json.dumps(payload) if payload else None))

    def pipeline_value(self) -> float:
        with self._conn() as c:
            row = c.execute("SELECT COALESCE(SUM(deal_value),0) v FROM leads WHERE status NOT IN ('dead','paused')").fetchone()
            return float(row["v"])

    # ── drafts / sends (human-approval state machine) ───────────────────────
    def set_qualification(self, lead_id: str, qualified: bool, reasons: list[str], score: int = 0,
                          signals: dict[str, Any] | None = None) -> None:
        """Record the gate's verdict. Qualified → 'scored'; not qualified → 'new' (and any pending
        draft or prospect/follow-up action for the lead is withdrawn). Leads already sent/replied/
        booked keep their status — history is never rewritten."""
        lead = self.get_lead(lead_id)
        if not lead:
            raise KeyError(lead_id)
        notes = {"qualification": {"qualified": qualified, "reasons": reasons, "at": now()}, **({"signals": signals} if signals else {})}
        with self._conn() as c:
            c.execute("UPDATE leads SET score=?, notes=?, updated_at=? WHERE id=?", (score, json.dumps(notes), now(), lead_id))
        if lead["status"] in ("sent", "opened", "replied", "booked", "paused", "dead"):
            return
        if qualified:
            if lead["status"] != "drafted":
                self.transition_lead(lead_id, "scored", {"reasons": reasons})
            return
        with self._conn() as c:
            c.execute("UPDATE email_drafts SET approval_state='rejected', approved_by='gate', approved_at=? "
                      "WHERE lead_id=? AND approval_state IN ('pending','approved','edited')", (now(), lead_id))
            for a in c.execute("SELECT id, context FROM actions WHERE status='pending' AND action_type IN ('prospect','follow_up')"):
                ctx = json.loads(a["context"]) if a["context"] else {}
                if ctx.get("lead_id") == lead_id:
                    c.execute("UPDATE actions SET status='ignored', decided_at=? WHERE id=?", (now(), a["id"]))
        if lead["status"] != "new":
            self.transition_lead(lead_id, "new", {"withdrawn": reasons})

    def lead_funnel(self) -> dict[str, int]:
        """found · contactable · qualified — three separate numbers, none stands in for another."""
        marks = ",".join("?" * len(QUALIFIED_STATUSES))
        with self._conn() as c:
            found = c.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
            contactable = c.execute("SELECT COUNT(*) n FROM leads WHERE contact_email IS NOT NULL AND TRIM(contact_email) != ''").fetchone()["n"]
            qualified = c.execute(
                f"SELECT COUNT(*) n FROM leads WHERE status IN ({marks}) AND contact_email IS NOT NULL AND TRIM(contact_email) != ''",
                tuple(sorted(QUALIFIED_STATUSES))).fetchone()["n"]
        return {"found": found, "contactable": contactable, "qualified": qualified}

    def create_draft(self, lead_id: str, recipe_key: str, subject_variant: str, subject: str, body: str, model: str) -> str:
        draft_id = str(uuid.uuid4())
        with self._conn() as c:
            c.execute("UPDATE email_drafts SET approval_state='superseded' WHERE lead_id=? AND approval_state='pending'", (lead_id,))
            c.execute(
                "INSERT INTO email_drafts (id, created_at, lead_id, recipe_key, subject_variant, subject, body, model) VALUES (?,?,?,?,?,?,?,?)",
                (draft_id, now(), lead_id, recipe_key, subject_variant, subject, body, model),
            )
        self.transition_lead(lead_id, "drafted", {"draft_id": draft_id})
        return draft_id

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM email_drafts WHERE id=?", (draft_id,)).fetchone()
            return dict(row) if row else None

    def set_draft_approval(self, draft_id: str, state: str, by: str = "cli") -> None:
        with self._conn() as c:
            c.execute("UPDATE email_drafts SET approval_state=?, approved_by=?, approved_at=? WHERE id=?", (state, by, now(), draft_id))

    def record_send(self, draft_id: str, lead_id: str, to_email: str, subject: str, body: str, provider: str,
                    provider_message_id: str | None = None) -> str:
        send_id = str(uuid.uuid4())
        with self._conn() as c:
            c.execute(
                "INSERT INTO email_sends (id, sent_at, draft_id, lead_id, to_email, subject, body, provider, provider_message_id)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (send_id, now(), draft_id, lead_id, to_email, subject, body, provider, provider_message_id),
            )
        self.transition_lead(lead_id, "sent", {"send_id": send_id})
        return send_id

    def recent_sends(self, limit: int = 2000) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM email_sends ORDER BY sent_at DESC LIMIT ?", (limit,))]

    def mark_send(self, send_id: str, *, replied: bool = False, bounced: bool = False, unsubscribed: bool = False, opened: bool = False) -> None:
        sets, params = [], []
        if replied:
            sets.append("replied_at=COALESCE(replied_at, ?)")
            params.append(now())
        if opened:
            sets.append("opened_first_at=COALESCE(opened_first_at, ?)")
            params.append(now())
        if bounced:
            sets.append("bounced=1")
        if unsubscribed:
            sets.append("unsubscribed=1")
        if not sets:
            return
        with self._conn() as c:
            c.execute(f"UPDATE email_sends SET {', '.join(sets)} WHERE id=?", (*params, send_id))

    def daily_send_count(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) n FROM email_sends WHERE sent_at >= date('now')").fetchone()["n"])

    def add_unsubscribe(self, email: str, via: str = "manual") -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO unsubscribes (email, at, via) VALUES (?,?,?)", (email.lower(), now(), via))

    def is_unsubscribed(self, email: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM unsubscribes WHERE email=?", (email.lower(),)).fetchone() is not None

    # ── outcomes (what measurable result occurred) ──────────────────────────
    def record_outcome(self, action_id: int, status: str, *, metric: str | None = None, before: Any = None, after: Any = None,
                       before_value: float | None = None, after_value: float | None = None, note: str | None = None) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO outcomes (action_id, measured_at, status, metric, before_value, after_value, before_json, after_json, note)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (action_id, now(), status, metric, before_value, after_value,
                 json.dumps(before, default=str) if before is not None else None,
                 json.dumps(after, default=str) if after is not None else None, note),
            )
            return int(cur.lastrowid)

    def latest_outcome(self, action_id: int) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM outcomes WHERE action_id=? ORDER BY id DESC LIMIT 1", (action_id,)).fetchone()
            return dict(row) if row else None

    def executed_actions(self, limit: int = 200) -> list[dict[str, Any]]:
        """Executed actions newest first, each with its latest outcome attached (or None)."""
        out = []
        for a in self.list_actions("executed", limit=limit):
            a["outcome"] = self.latest_outcome(a["id"])
            out.append(a)
        return out

    def results_summary(self) -> dict[str, Any]:
        with self._conn() as c:
            # withdrawn/superseded rows (dedupe_key suffixed by the gate or a redraft) were never findings for the customer
            found = c.execute("SELECT COUNT(*) n FROM actions WHERE dedupe_key IS NULL OR "
                              "(dedupe_key NOT LIKE '%:withdrawn-%' AND dedupe_key NOT LIKE '%:superseded-%')").fetchone()["n"]
            executed = c.execute("SELECT COUNT(*) n FROM actions WHERE status='executed'").fetchone()["n"]
            measured = c.execute(
                "SELECT COUNT(DISTINCT action_id) n FROM outcomes WHERE status='measured'").fetchone()["n"]
            sends = c.execute("SELECT COUNT(*) n, SUM(replied_at IS NOT NULL) r, SUM(bounced) b FROM email_sends").fetchone()
            booked = c.execute("SELECT COUNT(*) n FROM leads WHERE status='booked'").fetchone()["n"]
        return {"found": found, "executed": executed, "measured": measured, "emails_sent": sends["n"] or 0,
                "replies": sends["r"] or 0, "bounces": sends["b"] or 0, "booked": booked, "pipeline_value": self.pipeline_value()}

    # ── metrics ─────────────────────────────────────────────────────────────
    def record_metric(self, name: str, value: float, **dims: Any) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO metrics (at, name, value, dims) VALUES (?,?,?,?)", (now(), name, value, json.dumps(dims)))

    def latest_metrics(self) -> dict[str, float]:
        with self._conn() as c:
            rows = c.execute("SELECT name, value FROM metrics WHERE id IN (SELECT MAX(id) FROM metrics GROUP BY name)")
            return {r["name"]: r["value"] for r in rows}
