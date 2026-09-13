"""Objectives — the one thing the business is trying to achieve, kept between runs.

A workspace normally holds exactly one active objective (`revenueos init` creates it from the
questionnaire's `objective` answer). Everything RevenueOS observes, does, measures or fails at
is appended to that objective as an event, so TODAY can say what is being worked towards, what
happened since yesterday, and what is next — without anybody reading a log file.

Storage is the existing SQLite store (`objectives`, `objective_events`); the scheduler is the
existing orchestrator (`data/automations.json` → `revenueos run heartbeat`). There is no new
daemon and no new product layer.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .paths import Workspace
from .store import OBJECTIVE_EVENT_KINDS, OBJECTIVE_STATUSES, Store

__all__ = [
    "OBJECTIVE_EVENT_KINDS",
    "OBJECTIVE_STATUSES",
    "active_objectives",
    "ensure_objective",
    "objective_block",
    "render_objective",
]

HOW_TO_ADD = 'No objective set. Add one: revenueos objective add "<what you want in the next 90 days>"'


def active_objectives(store: Store) -> list[dict[str, Any]]:
    return store.list_objectives("active")


def ensure_objective(store: Store, title: str, strategy: str | None = None) -> int | None:
    """Create the objective unless an active one with the same title already exists.

    Idempotent so `revenueos init` can be re-run (or the panel's Business form re-saved)
    without stacking duplicates.
    """
    title = (title or "").strip()
    if not title:
        return None
    for o in store.list_objectives("active"):
        if o["title"].strip().lower() == title.lower():
            if strategy and not o.get("strategy"):
                store.update_objective(o["id"], strategy=strategy)
            return None
    return store.create_objective(title, strategy=strategy)


def objective_block(store: Store) -> dict[str, Any] | None:
    """The compact shape TODAY renders: the first active objective plus its last heartbeat.

    Returns None when the workspace has no active objective — the caller then prints
    `HOW_TO_ADD` rather than inventing a goal for the business.
    """
    actives = active_objectives(store)
    if not actives:
        return None
    o = actives[0]
    hb = store.latest_heartbeat(o["id"])
    return {
        "id": o["id"],
        "title": o["title"],
        "status": o["status"],
        "strategy": o.get("strategy"),
        "next_action": o.get("next_action"),
        "heartbeat_at": hb["ts"] if hb else None,
        "heartbeat": hb["text"] if hb else None,
        "others": len(actives) - 1,
    }


def render_objective(block: dict[str, Any] | None) -> list[str]:
    """Text lines for `revenueos today` (the panel renders the same fields as HTML)."""
    if not block:
        return ["OBJECTIVE", f"  {HOW_TO_ADD}"]
    out = ["OBJECTIVE", f"  [{block['id']}] {block['title']}  ({block['status']})"]
    if block.get("strategy"):
        out.append(f"  strategy: {block['strategy']}")
    out.append(f"  next: {block.get('next_action') or 'not decided yet — the heartbeat sets this every 30 minutes'}")
    if block.get("heartbeat"):
        out.append(f"  last heartbeat {(block.get('heartbeat_at') or '')[:16].replace('T', ' ')}: {block['heartbeat']}")
    else:
        out.append("  last heartbeat: none yet — run `revenueos run heartbeat`")
    if block.get("others"):
        out.append(f"  (+{block['others']} more active objective(s) — revenueos objective list)")
    return out


# ── the governing mandate ─────────────────────────────────────────────────
MANDATE_FILES = ("REVENUEOS_OPERATOR_MANDATE.md", "MANDATE.md")


def read_mandate(ws: Workspace) -> dict[str, Any] | None:
    """The workspace's governing mandate, if a mandate file sits at its root.

    Returns the path, the text, a short digest, the first line as title and the one sentence that
    states the objective: the paragraph after "The primary responsibility of … is:" when present,
    else the first paragraph that is not a heading. Nothing is inferred beyond that."""
    for name in MANDATE_FILES:
        path = Path(ws.root) / name
        if path.is_file():
            raw = path.read_bytes()
            text = raw.decode("utf-8", errors="replace")
            lines = [ln.strip() for ln in text.splitlines()]
            title = next((ln.lstrip("# ").strip() for ln in lines if ln), "")
            paragraphs = [pg.strip() for pg in re.split(r"\n\s*\n", text) if pg.strip()]
            objective = ""
            for i, pg in enumerate(paragraphs):
                if re.search(r"primary responsibility .* is:\s*$", pg, re.I) and i + 1 < len(paragraphs):
                    objective = paragraphs[i + 1]
                    break
            if not objective:
                objective = next((pg for pg in paragraphs[1:] if not pg.startswith("#") and len(pg.split()) > 6), "")
            objective = re.sub(r"\s+", " ", objective).strip()
            return {"path": str(path), "text": text, "sha": hashlib.sha256(raw).hexdigest()[:12],
                    "title": title, "objective": objective}
    return None


def objective_from_mandate(store: Store, ws: Workspace) -> dict[str, Any] | None:
    """Bind the workspace's active objective to its mandate file.

    No active objective → create one from the mandate's objective sentence. An active objective →
    leave it; either way record one `evidence` event "mandate read …" per file digest, so a re-run
    changes nothing and a changed mandate is visible in the objective's trail."""
    m = read_mandate(ws)
    if not m:
        return None
    active = store.list_objectives("active")
    created = False
    if active:
        oid = active[0]["id"]
    else:
        title = (m["objective"] or m["title"])[:200]
        oid = store.create_objective(title, strategy=None)
        created = True
    tag = f"mandate read: {Path(m['path']).name} sha {m['sha']}"
    seen = any((e.get("ref") or {}).get("mandate_sha") == m["sha"]
               for e in store.list_objective_events(oid, limit=1000, kind="evidence"))
    if not seen:
        store.add_objective_event(oid, "evidence", f"{tag} — \"{m['title']}\"",
                                  {"mandate_sha": m["sha"], "path": m["path"], "chars": len(m["text"])})
    return {"objective_id": oid, "created": created, "path": m["path"], "sha": m["sha"], "title": m["title"], "recorded": not seen}
