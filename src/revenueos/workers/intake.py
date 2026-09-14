"""INTAKE — read the documents the business hands over, and turn them into canon it can use.

A business does not want to retype its price list into a questionnaire; it wants to hand over
the PDF.  Drop any document into `data/inbox/documents/` and this worker reads it locally
(`revenueos.intake` — pypdf / python-docx / openpyxl / python-pptx / striprtf, no upload, no
external binary), keeps the extracted text in `data/documents/`, and records the file in the
`documents` table keyed by content hash, so re-running the worker on the same bytes does
nothing at all.

With a model credential it then compares the document against the business canon
(`ctx.prompt_summary()`) and proposes, for each place the document contradicts or fills a gap
in `company-context/`, **one pending `correction` action** quoting the document.  It never
writes to `company-context/`: only a human approving and executing the action does anything,
and even then the change lands in `learning-loop/CORRECTIONS.md` (the append-only correction
channel every worker prompt already reads), not in the canon files.

Without a model the worker still extracts, records and reports every document — and says so:
the reading is deterministic, only the interpretation needs a model.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..context import BusinessContext
from ..intake import Document, inbox_documents, read_document, sha256_file
from ..llm import LLM, Message
from ..paths import Workspace
from ..store import Store
from . import WorkerResult

# How much of a document the model is shown. Long documents are common (a 60-page business
# plan); the prompt stays bounded and says where it was cut.
PROMPT_DOC_CHARS = 24_000
MAX_PROPOSALS_PER_DOCUMENT = 5

SYSTEM = """You are the intake analyst inside RevenueOS, an autonomous revenue department.

A business has handed over one of its own documents. You are given (a) the business canon
RevenueOS currently holds and (b) the text extracted from that document.

Do two things, and nothing else:
1. Summarise, in at most 120 words, what this document tells RevenueOS about the business —
   facts a revenue department would act on (what is sold, at what price, to whom, against whom,
   with what claims or constraints). If it tells you nothing useful, say exactly that.
2. Propose corrections to the canon ONLY where the document contradicts what the canon says or
   fills a heading the canon has left as placeholder text. Every proposal must quote the
   document. Propose nothing you cannot quote. Zero proposals is a correct and common answer.

Rules:
- Never invent a fact, a price, a customer or a number that is not in the document.
- Only use a `file` and `heading` from the canon list you are given. Never invent either.
- `proposed` is the replacement body for that heading, in the same plain style as the canon.

Return ONLY minified JSON, no prose, no code fence:
{"summary":"...","proposals":[{"file":"offer.md","heading":"Core Offer","proposed":"...","why":"...","quote":"..."}]}
"""


def _canon_headings(ctx: BusinessContext) -> dict[str, list[str]]:
    """The headings that actually exist, per canon file — the whitelist a proposal is checked against."""
    out: dict[str, list[str]] = {}
    for name, text in ctx.files.items():
        headings = re.findall(r"(?m)^## (.+?)\s*$", text)
        if headings:
            out[name] = headings
    return out


def _extract_json(raw: str) -> dict[str, Any] | None:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", raw).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def analyse(ctx: BusinessContext, llm: LLM, doc: Document) -> dict[str, Any]:
    """Ask the model what this document says about the business. Returns
    {"summary": str, "proposals": [...]} with every proposal validated against the real canon;
    anything the model invented is dropped, not repaired."""
    headings = _canon_headings(ctx)
    body = doc.text[:PROMPT_DOC_CHARS]
    if len(doc.text) > PROMPT_DOC_CHARS:
        body += f"\n\n[only the first {PROMPT_DOC_CHARS:,} of {len(doc.text):,} characters are shown]"
    user = (
        f"=== BUSINESS CANON RevenueOS HOLDS ===\n{ctx.prompt_summary()}\n\n"
        f"=== CANON FILES AND HEADINGS YOU MAY PROPOSE AGAINST ===\n"
        + "\n".join(f"{f}: {', '.join(hs)}" for f, hs in sorted(headings.items()))
        + f"\n\n=== DOCUMENT: {doc.title} ({doc.describe()}) ===\n{body}"
    )
    parsed = _extract_json(llm.complete_sync([Message("system", SYSTEM), Message("user", user)], max_tokens=4000))
    if not parsed:
        return {"summary": "", "proposals": [], "error": "the model did not return the JSON this worker asked for"}
    good: list[dict[str, str]] = []
    for p in parsed.get("proposals") or []:
        if not isinstance(p, dict):
            continue
        file, heading = str(p.get("file") or "").strip(), str(p.get("heading") or "").strip()
        proposed, quote = str(p.get("proposed") or "").strip(), str(p.get("quote") or "").strip()
        if heading not in headings.get(file, []) or not proposed or not quote:
            continue  # a heading that does not exist, or a claim with nothing behind it
        good.append({"file": file, "heading": heading, "proposed": proposed, "quote": quote,
                     "why": str(p.get("why") or "").strip()})
        if len(good) >= MAX_PROPOSALS_PER_DOCUMENT:
            break
    return {"summary": str(parsed.get("summary") or "").strip(), "proposals": good}


def _slug(text: str, limit: int = 48) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or "document"


class IntakeWorker:
    name = "intake"
    description = "Read the business's own documents (PDF, Word, Excel, PowerPoint, CSV, RTF) and propose canon corrections."
    upstream = "RevenueOS's own, over pypdf / python-docx / openpyxl / python-pptx / striprtf"

    def run(self, ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None, run_id: int) -> WorkerResult:
        if not ctx.is_onboarded():
            return WorkerResult(ok=False, summary="", error="not onboarded — run `revenueos init` first")
        drop = ws.documents_inbox
        files = inbox_documents(drop)
        if not files:
            rel = drop.relative_to(ws.root) if drop.is_relative_to(ws.root) else drop
            return WorkerResult(ok=True, summary=f"no documents to read — drop a PDF, Word file, spreadsheet or deck into {rel}/.",
                                details={"inbox": str(rel), "documents": 0})

        created = read = skipped = failed = 0
        summarised = 0
        details: list[dict[str, Any]] = []
        for path in files:
            try:
                sha = sha256_file(path)
            except OSError as exc:
                failed += 1
                details.append({"file": path.name, "ok": False, "error": f"could not be read from disk ({exc})"})
                continue
            if store.get_document(sha):
                skipped += 1
                continue
            doc = read_document(path)
            text_rel: str | None = None
            if doc.ok and doc.text.strip():
                out = ws.documents / f"{sha[:8]}-{_slug(path.stem)}.md"
                out.write_text(doc.as_markdown(), encoding="utf-8")
                text_rel = str(out.relative_to(ws.root))
            store.record_document(path=str(path), sha256=sha, format=doc.format, pages=doc.pages,
                                  chars=doc.chars, title=doc.title, text_path=text_rel, error=doc.error)
            if not doc.ok:
                failed += 1
                details.append({"file": path.name, "ok": False, "error": doc.error})
                continue
            read += 1
            entry: dict[str, Any] = {"file": path.name, "ok": True, "format": doc.format, "pages": doc.pages,
                                     "chars": doc.chars, "tables": len(doc.tables), "text": text_rel}
            if doc.notes:
                entry["notes"] = doc.notes
            if llm is not None:
                result = analyse(ctx, llm, doc)
                if result.get("summary"):
                    store.set_document_summary(sha, result["summary"])
                    summarised += 1
                    entry["summary"] = result["summary"]
                if result.get("error"):
                    entry["summary_error"] = result["error"]
                for p in result.get("proposals", []):
                    aid = store.create_action(
                        "correction",
                        f"{p['file'].removesuffix('.md')} → {p['heading']}: correct it from {doc.title[:48]}",
                        f"{p['why'] or 'The document contradicts or fills this heading.'}\n\n"
                        f"From the document: “{p['quote'][:400]}”\n\n"
                        f"Proposed body for `{p['file']} → {p['heading']}`:\n{p['proposed']}",
                        run_id=run_id,
                        dedupe_key=f"intake:{sha[:12]}:{p['file']}:{p['heading']}",
                        context={"executor": "record_correction", "document_sha": sha, "document": path.name,
                                 "document_text": text_rel, "canon_file": p["file"], "canon_heading": p["heading"],
                                 "proposed": p["proposed"], "quote": p["quote"], "why": p["why"]},
                    )
                    created += 1 if aid else 0
            details.append(entry)

        parts = [f"{read} document(s) read"]
        if failed:
            parts.append(f"{failed} could not be read")
        if skipped:
            parts.append(f"{skipped} already read (unchanged)")
        if llm is None and read:
            parts.append("no summary or canon correction: that needs a model credential (set ANTHROPIC_API_KEY "
                         "or sign in to Claude Code)")
        elif read:
            parts.append(f"{summarised} summarised, {created} canon correction(s) proposed")
        return WorkerResult(ok=True, summary="; ".join(parts) + ".", actions_created=created,
                            details={"read": read, "failed": failed, "skipped": skipped,
                                     "summarised": summarised, "documents": details})


def execute_record_correction(ws: Workspace, store: Store, ctx: BusinessContext, llm: LLM | None,
                              action: dict[str, Any]) -> str:
    """Approved: write the proposed correction into learning-loop/CORRECTIONS.md, where every
    worker prompt picks it up for the next 30 days. `company-context/` is never edited by a
    worker — a human owns the canon files; this is the channel RevenueOS is allowed to write."""
    c = action.get("context") or {}
    file, heading = c.get("canon_file"), c.get("canon_heading")
    proposed = (c.get("proposed") or "").strip()
    if not file or not heading or not proposed:
        return "not recorded: this action carries no proposed correction."
    source = c.get("document") or "an intake document"
    ctx.add_correction(
        title=f"{file} → {heading} (from {source})",
        context=f"The business document {source} contradicts or fills `{file} → {heading}`. "
                f"It says: “{(c.get('quote') or '').strip()[:300]}”",
        correction=proposed,
        apply_when=f"any work that relies on {heading.lower()}",
        source=f"intake:{source}",
    )
    return (f"recorded in learning-loop/CORRECTIONS.md as the correction for {file} → {heading}; "
            f"every worker prompt reads it for the next 30 days. Edit company-context/{file} yourself "
            f"to make it permanent.")
