"""Keyword and search-term evidence for the Google Ads control audit.

Fourteen of the 97 Google controls (search-term recency, negative-keyword governance, account-level
negatives, quality score, zero-impression keywords, keyword-to-ad relevance, zero-conversion keywords,
cross-campaign duplication, PMax negatives …) can only be decided from keyword, search-term and
negative-keyword data; a campaign-level snapshot leaves them `unknown`. This module turns either input
into the same evidence sections on the AccountSnapshot:

  * a connected account — `connections.google.ads_keywords / ads_search_terms / ads_negative_keywords`
  * report exports dropped next to the campaign export —
      data/exports/ads-google.keywords.csv       (Google Ads → Keywords → Search keywords → download)
      data/exports/ads-google.search-terms.csv   (Google Ads → Keywords → Search terms → download)
    The UI export's title lines, "Total" rows, thousands separators, "--" and UTF-16 tab files are handled.

`attach` bounds the rows the model sees (largest spenders first) and records `evidence_coverage`, so a
verdict can say exactly how much of the account it looked at. `search_term_waste` is the one
deterministic finding: terms with spend, no conversions and no exclusion; the next read of the same
terms measures it (`measure.measure_search_terms`).
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from ..vendor.claude_ads_core.contracts import validate_contract

ROW_LIMIT = 150
_KEY_ALIASES = {
    "keyword": ("keyword", "search keyword", "keyword text"),
    "text": ("search term", "search terms", "query"),
    "match_type": ("match type", "keyword match type", "match_type"),
    "status": ("keyword status", "status", "added/excluded", "added / excluded"),
    "campaign": ("campaign", "campaign name"),
    "ad_group": ("ad group", "ad group name"),
    "quality_score": ("quality score", "qual. score", "qs"),
    "cost": ("cost", "spend"),
    "clicks": ("clicks",),
    "impressions": ("impr.", "impressions", "impr"),
    "conversions": ("conversions", "conv.", "conv"),
}
_HEADER_MARKERS = ("keyword", "search term", "campaign")


def _num(v: Any) -> float:
    s = str(v if v is not None else "").strip().replace(",", "").replace("%", "")
    if s in ("", "--", "-", "—"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _match(v: Any) -> str:
    s = str(v or "").strip().lower()
    for m in ("exact", "phrase", "broad"):
        if s.startswith(m) or f" {m}" in s:
            return m
    return s or "unknown"


def _term_status(v: Any) -> str:
    s = str(v or "").strip().lower().replace("_", " ").replace("&", "and")
    if "added" in s and "exclu" in s:
        return "added_excluded"
    if "exclu" in s:
        return "excluded"
    if "added" in s:
        return "added"
    return "none"


def _read_table(path: Path) -> list[dict[str, str]]:
    raw = path.read_bytes()
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if any(m in ln.lower() for m in _HEADER_MARKERS) and (("," in ln) or ("\t" in ln))), 0)
    body = "\n".join(lines[start:])
    delim = "\t" if body.splitlines()[0].count("\t") > body.splitlines()[0].count(",") else ","
    rows = []
    for row in csv.DictReader(io.StringIO(body), delimiter=delim):
        vals = [str(v or "").strip() for v in row.values()]
        if not any(vals) or vals[0].lower().startswith("total"):
            continue
        rows.append({(k or "").strip().lower(): (v or "").strip() for k, v in row.items()})
    return rows


def _col(row: dict[str, str], key: str) -> str:
    for alias in _KEY_ALIASES[key]:
        if alias in row:
            return row[alias]
    return ""


def read_keywords_csv(path: Path) -> list[dict[str, Any]]:
    out = []
    for r in _read_table(path):
        text = _col(r, "keyword")
        if not text:
            continue
        qs = _num(_col(r, "quality_score"))
        out.append({"keyword_id": None, "text": text, "match_type": _match(_col(r, "match_type")), "status": (_col(r, "status") or "").lower() or "unknown",
                    "quality_score": int(qs) if qs else None, "campaign_id": None, "campaign_name": _col(r, "campaign") or None,
                    "ad_group_id": None, "ad_group_name": _col(r, "ad_group") or None, "cost": _num(_col(r, "cost")),
                    "clicks": int(_num(_col(r, "clicks"))), "impressions": int(_num(_col(r, "impressions"))), "conversions": _num(_col(r, "conversions"))})
    return out


def read_search_terms_csv(path: Path) -> list[dict[str, Any]]:
    out = []
    for r in _read_table(path):
        text = _col(r, "text")
        if not text:
            continue
        out.append({"text": text, "match_type": _match(_col(r, "match_type")), "status": _term_status(_col(r, "status")),
                    "campaign_id": None, "campaign_name": _col(r, "campaign") or None, "ad_group_id": None, "ad_group_name": _col(r, "ad_group") or None,
                    "cost": _num(_col(r, "cost")), "clicks": int(_num(_col(r, "clicks"))), "impressions": int(_num(_col(r, "impressions"))),
                    "conversions": _num(_col(r, "conversions"))})
    return out


def sidecars(exports: Path, platform: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The two report exports next to ads-<platform>.csv, if the owner dropped them."""
    kw = exports / f"ads-{platform}.keywords.csv"
    st = exports / f"ads-{platform}.search-terms.csv"
    return (read_keywords_csv(kw) if kw.exists() else []), (read_search_terms_csv(st) if st.exists() else [])


def resolve_campaign_ids(rows: list[dict[str, Any]], campaigns: list[dict[str, Any]]) -> None:
    """Exports name campaigns; the snapshot keys them by id. Fill the id where the name matches exactly."""
    by_name = {str(c.get("name") or "").strip().lower(): str(c.get("campaign_id") or c.get("id")) for c in campaigns if c.get("name")}
    for r in rows:
        if not r.get("campaign_id") and r.get("campaign_name"):
            r["campaign_id"] = by_name.get(r["campaign_name"].strip().lower())


def search_term_waste(terms: list[dict[str, Any]], min_cost: float = 20.0) -> list[dict[str, Any]]:
    """Search terms that spent real money, converted nothing and are neither added nor excluded — the negatives to add."""
    rows = [t for t in terms if t["cost"] >= min_cost and t["conversions"] == 0 and t["status"] in ("none", "added")]
    return sorted(rows, key=lambda t: -t["cost"])


def coverage(keywords: list[dict[str, Any]], terms: list[dict[str, Any]], negatives: dict[str, Any] | None) -> dict[str, Any]:
    qs = [k["quality_score"] for k in keywords if k.get("quality_score")]
    unmanaged = [t for t in terms if t["status"] == "none"]
    return {
        "keywords_total": len(keywords), "keywords_zero_impressions": sum(1 for k in keywords if k["impressions"] == 0),
        "keywords_with_quality_score": len(qs), "keywords_quality_score_le_3": sum(1 for q in qs if q <= 3),
        "keyword_spend_without_conversions": round(sum(k["cost"] for k in keywords if k["conversions"] == 0), 2),
        "search_terms_total": len(terms), "search_terms_not_added_or_excluded": len(unmanaged),
        "search_term_spend_not_added_or_excluded": round(sum(t["cost"] for t in unmanaged), 2),
        "search_term_spend_without_conversions": round(sum(t["cost"] for t in terms if t["conversions"] == 0), 2),
        "negatives_campaign_level": len((negatives or {}).get("campaign", [])), "negative_lists": len((negatives or {}).get("lists", [])),
        "negative_list_attachments": len((negatives or {}).get("list_campaigns", [])),
    }


def attach(snapshot: dict[str, Any], *, keywords: list[dict[str, Any]] | None = None, search_terms: list[dict[str, Any]] | None = None,
           negatives: dict[str, Any] | None = None, limit: int = ROW_LIMIT) -> dict[str, Any]:
    """Add the evidence sections (bounded, biggest spenders first) and a coverage block; the snapshot stays contract-valid."""
    keywords = keywords or []
    search_terms = search_terms or []
    if not keywords and not search_terms and not negatives:
        return snapshot
    resolve_campaign_ids(keywords, snapshot.get("campaigns", []))
    resolve_campaign_ids(search_terms, snapshot.get("campaigns", []))
    cov = coverage(keywords, search_terms, negatives)
    snapshot["keywords"] = sorted(keywords, key=lambda k: (-k["cost"], -k["impressions"]))[:limit]
    snapshot["search_terms"] = sorted(search_terms, key=lambda t: (-t["cost"], -t["impressions"]))[:limit]
    if negatives:
        snapshot["negatives"] = {"campaign": negatives.get("campaign", [])[:limit * 2],
                                 "lists": [{**lst, "keywords": lst.get("keywords", [])[:limit]} for lst in negatives.get("lists", [])],
                                 "list_campaigns": negatives.get("list_campaigns", [])}
    cov.update({"keywords_listed": len(snapshot["keywords"]), "search_terms_listed": len(snapshot["search_terms"]),
                "note": "rows are the largest spenders first; totals above count every row read"})
    snapshot["evidence_coverage"] = cov
    validate_contract("account-snapshot", snapshot)
    return snapshot


def bounded_json(snapshot: dict[str, Any], limit: int = 40000) -> str:
    """The snapshot as JSON under `limit` characters: the largest list is halved until it fits, and the coverage block says so.
    Never a mid-document slice, so the model always sees valid JSON."""
    snap = json.loads(json.dumps(snapshot))
    text = json.dumps(snap)
    while len(text) > limit:
        lists = {k: len(json.dumps(v)) for k, v in snap.items() if isinstance(v, list) and len(v) > 1}
        if not lists:
            break
        key = max(lists, key=lists.get)
        snap[key] = snap[key][: len(snap[key]) // 2]
        snap.setdefault("evidence_coverage", {})[f"{key}_listed"] = len(snap[key])
        text = json.dumps(snap)
    return text if len(text) <= limit else json.dumps({k: v for k, v in snap.items() if not isinstance(v, list)})[:limit]


def terms_record(platform: str, window: dict[str, str], keywords: list[dict[str, Any]], search_terms: list[dict[str, Any]],
                 negatives: dict[str, Any] | None, source: str) -> dict[str, Any]:
    return {"schema_version": "1.0.0", "platform": platform, "source": source, "window": window,
            "keywords": keywords, "search_terms": search_terms, "negatives": negatives or {}}


def write_terms(exports: Path, record: dict[str, Any]) -> Path:
    path = exports / f"ads-{record['platform']}.terms.json"
    path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    return path


def read_terms(exports: Path, platform: str) -> dict[str, Any] | None:
    path = exports / f"ads-{platform}.terms.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
