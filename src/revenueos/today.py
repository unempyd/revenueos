"""TODAY — the whole customer-facing surface: what RevenueOS found, what it did, what happened.

    leads: 40 found · 18 contactable · 12 qualified
    12 qualified prospects found
     3 follow-ups ready
     ...
    $X pipeline generated
    Approve / Execute / Ignore

    RESULTS
    ✓ SEO: missing description — /pricing        meta_description_fixed 0 → 1
    … Send to maria@… — quick question           replied: no reply yet (sent 2026-09-12)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .store import Store

LINES: list[tuple[str, str, str]] = [
    ("prospect", "qualified prospect found", "qualified prospects found"),
    ("follow_up", "follow-up ready", "follow-ups ready"),
    ("campaign_attention", "campaign needs attention", "campaigns need attention"),
    ("seo_opportunity", "SEO opportunity found", "SEO opportunities found"),
    ("content_opportunity", "content opportunity", "content opportunities"),
    ("ad_waste", "ad wasting money", "ads wasting money"),
    ("market_signal", "conversation to join", "conversations to join"),
]

STATUS_MARK = {"measured": "✓", "no_effect": "○", "pending": "…", "unmeasurable": "–", None: "…"}


@dataclass
class Brief:
    counts: dict[str, int]
    pipeline_value: float
    actions: list[dict[str, Any]]
    metrics: dict[str, float]
    results: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    funnel: dict[str, int] = field(default_factory=dict)
    approved: list[dict[str, Any]] = field(default_factory=list)  # decided yes, not executed yet

    def funnel_line(self) -> str | None:
        """'leads: 30 found · 9 contactable · 0 qualified' — three numbers, reported separately, so
        the "qualified prospects" line below can only ever mean what it says."""
        f = self.funnel
        if not f or not f.get("found"):
            return None
        return f"leads: {f['found']} found · {f.get('contactable', 0)} contactable · {f.get('qualified', 0)} qualified"

    def lines(self) -> list[str]:
        out = []
        fl = self.funnel_line()
        if fl:
            out.append(fl)
        for atype, singular, plural in LINES:
            n = self.counts.get(atype, 0)
            if n:
                out.append(f"{n:>3} {singular if n == 1 else plural}")
        out.append(f"${self.pipeline_value:,.0f} pipeline generated")
        return out

    def result_lines(self, limit: int = 20) -> list[str]:
        out = []
        for a in self.results[:limit]:
            o = a.get("outcome") or {}
            mark = STATUS_MARK.get(o.get("status"))
            if o.get("metric") is not None and o.get("after_value") is not None:
                what = f"{o['metric']} {o.get('before_value') or 0:g} → {o['after_value']:g}" + (f" — {o['note']}" if o.get("note") else "")
            else:
                what = o.get("note") or "awaiting measurement"
            out.append(f"{mark} [{a['id']:>4}] {a['title'][:58]:<58} {what}")
        return out

    def render_text(self, company: str) -> str:
        head = [f"TODAY — {company}", ""]
        body = self.lines()
        if not self.actions and not self.approved:
            body += ["", "Nothing pending. Run `revenueos run all` or wait for the orchestrator."]
        else:
            if self.actions:
                body.append("")
                for a in self.actions[:40]:
                    body.append(f"[{a['id']:>4}] {a['action_type']:<19} {a['title'][:90]}")
                body += ["", "Approve / Execute / Ignore:  revenueos approve <id> | revenueos execute <id> | revenueos ignore <id>"]
            if self.approved:
                body += ["", f"APPROVED, waiting to run ({len(self.approved)}) — revenueos execute <id>"]
                for a in self.approved[:40]:
                    body.append(f"[{a['id']:>4}] {a['action_type']:<19} {a['title'][:90]}")
        if self.results:
            s = self.summary
            body += ["", "RESULTS", f"{s.get('executed', 0)} action(s) executed, {s.get('measured', 0)} with a measured result; "
                     f"{s.get('emails_sent', 0)} emails sent, {s.get('replies', 0)} replies, {s.get('booked', 0)} booked."]
            body += self.result_lines()
        ext = {k.removeprefix("growth_"): v for k, v in self.metrics.items() if k.startswith("growth_")}
        if ext:
            body += ["", "EXTERNAL (real numbers, last growth run)",
                     "  " + "  ·  ".join(f"{k} {v:g}" for k, v in sorted(ext.items()))]
        return "\n".join(head + body)


def rank_next(store: Store, limit: int = 5) -> list[dict[str, Any]]:
    """The next highest-value pending actions, ranked from measured history.

    Score = prior value of the action type (what it can lead to) × the measured win rate of that
    type in this workspace (measured outcomes ÷ executed, defaulting to 0.5 when nothing has
    been executed yet) × freshness. Types that have produced measured wins rise; types whose
    executions produced nothing fall. This is deliberately simple and fully explainable."""
    prior = {"follow_up": 5.0, "ad_waste": 4.0, "campaign_attention": 3.0, "prospect": 2.5, "seo_opportunity": 2.0,
             "content_opportunity": 1.5, "market_signal": 1.0, "correction": 0.5}
    executed = store.executed_actions(limit=1000)
    stats: dict[str, list[int]] = {}
    for a in executed:
        o = a.get("outcome") or {}
        s = stats.setdefault(a["action_type"], [0, 0])
        s[0] += 1
        s[1] += 1 if o.get("status") == "measured" else 0
    ranked = []
    for a in store.list_actions("pending"):
        n, wins = stats.get(a["action_type"], [0, 0])
        win_rate = (wins / n) if n else 0.5
        # a send without a reachable address, or without a configured mailbox, is externally blocked
        ctx_ = a.get("context") or {}
        mailbox = bool(os.environ.get("SMTP_PASSWORD")) or os.environ.get("REVENUEOS_DRY_RUN") == "1"
        blocked = ctx_.get("executor") == "send_email" and (not ctx_.get("to") or not mailbox)
        if blocked:
            a = {**a, "blocked": "no mailbox configured (SMTP_PASSWORD + smtp.host) — draft only"}
        score = prior.get(a["action_type"], 1.0) * (0.25 + win_rate) * (0.2 if blocked else 1.0)
        ranked.append({**a, "score": round(score, 3), "win_rate": round(win_rate, 2), "executed_of_type": n})
    ranked.sort(key=lambda r: (-r["score"], r["id"]))
    return ranked[:limit]


def build_brief(store: Store) -> Brief:
    return Brief(
        counts=store.counts_by_type("pending"),
        pipeline_value=store.pipeline_value(),
        actions=store.list_actions("pending"),
        metrics=store.latest_metrics(),
        results=store.executed_actions(),
        summary=store.results_summary(),
        funnel=store.lead_funnel(),
        approved=store.list_actions("approved"),
    )
