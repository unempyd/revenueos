"""Shared text helpers — strip reasoning artifacts from model output.

Many open-weight models (MiniMax, GLM-5, DeepSeek-R1) wrap a reasoning step in
``<think>...</think>`` (or similar) before the user-visible answer. Those tags
leak into the UI if we treat the raw content as the final answer. This module
provides a single function the LLM layer + tool layer call to scrub them.
"""

from __future__ import annotations

import json
import re

# Catches:
#   <think>...</think>          (standard, possibly multi-line)
#   <thinking>...</thinking>
#   <reasoning>...</reasoning>
#   <reflection>...</reflection>
# Lazy match, multi-line, case-insensitive.
_THINK_RE = re.compile(
    r"<(think|thinking|reasoning|reflection)\b[^>]*>.*?</\1>",
    flags=re.IGNORECASE | re.DOTALL,
)

# Some models emit only an opening tag if the response was cut off, or only
# the closing tag if the reasoning section preceded the visible answer at the
# very top of the message. Strip the leading orphan closer.
_ORPHAN_CLOSE_RE = re.compile(
    r"^\s*</(?:think|thinking|reasoning|reflection)>\s*",
    flags=re.IGNORECASE,
)

# Any opening reasoning tag with no matching close (the previous regex pass
# would've removed paired ones). Treat everything from the opener onward as
# unfinished reasoning that should not surface.
_ORPHAN_OPEN_RE = re.compile(
    r"<(?:think|thinking|reasoning|reflection)\b[^>]*>",
    flags=re.IGNORECASE,
)


# Phrases that signal the model is narrating its reasoning rather than
# producing the actual artifact. Always followed by more reasoning until
# the model finally lands on the deliverable. Match at start of a line.
_PREAMBLE_PHRASES = (
    "the user wants",
    "the user asked",
    "the user is asking",
    "the user has asked",
    "let me think",
    "let me draft",
    "let me write",
    "let me craft",
    "okay,",
    "okay so",
    "alright,",
    "sure,",
    "sure!",
    "here's",
    "here is",
    "i'll draft",
    "i'll write",
    "i'll create",
    "i'll generate",
    "i will draft",
    "i need to",
    "first, let me",
    "first, i",
    "now, let me",
    "now i",
)


def strip_draft_preamble(text: str) -> str:
    """Drop conversational preambles from a draft so only the artifact remains.

    Some reasoning-tuned models emit a few sentences of internal narration
    before the actual tweet / article / reply, even when told not to.
    We scan for a fenced or quoted block first; otherwise we drop leading
    lines that start with known meta phrases until we hit real content.

    Idempotent. Returns the original text if nothing matches.
    """
    if not text:
        return text
    src = text.strip()

    # If the model wrapped the artifact in triple-backticks or triple-quotes,
    # honor that as the canonical extraction.
    fenced = re.search(r"```(?:[a-z]+)?\s*\n(.*?)```", src, flags=re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    triple = re.search(r'"""\s*(.*?)\s*"""', src, flags=re.DOTALL)
    if triple:
        return triple.group(1).strip()

    # Otherwise drop leading paragraphs that read as meta-narration. We
    # treat double-newline as a paragraph boundary and walk forward.
    paragraphs = re.split(r"\n\s*\n", src)
    while paragraphs:
        first_lower = paragraphs[0].strip().lower()
        if any(first_lower.startswith(p) for p in _PREAMBLE_PHRASES):
            paragraphs.pop(0)
            continue
        # A line that ends with a colon and looks like a hand-off ("Here's the
        # tweet:", "Final draft:", etc.) — drop it but keep the rest.
        if first_lower.endswith(":") and len(first_lower) < 60:
            paragraphs.pop(0)
            continue
        break

    return "\n\n".join(paragraphs).strip() if paragraphs else src


def strip_reasoning(text: str) -> str:
    """Remove reasoning-block tags and their contents from model output.

    Idempotent. Safe to call on empty strings and content with no tags.
    """
    if not text:
        return text
    out = _THINK_RE.sub("", text)
    # If a stray closer remains (model cut off mid-reasoning), drop it.
    out = _ORPHAN_CLOSE_RE.sub("", out)
    # If a stray opener remains (model truncated mid-thought), drop the
    # opener and everything that follows — that content was reasoning.
    m = _ORPHAN_OPEN_RE.search(out)
    if m:
        out = out[: m.start()]
    # collapse the blank lines we may have just created
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


# CJK / fullwidth ranges. Cheap open models (MiniMax especially) occasionally
# emit a stray Chinese word inside an otherwise-English answer ("rate-limit替代").
_CJK_RE = re.compile(
    r"[　-〿぀-ヿ㐀-䶿一-鿿＀-￯]"
)


_LATIN_RE = re.compile(r"[A-Za-z]")


def strip_stray_cjk(text: str) -> str:
    """Drop stray CJK characters from predominantly-Latin text — a model glitch
    (MiniMax: "rate-limit替代"), not a genuinely CJK document, which we leave
    untouched. Heuristic: strip only when Latin letters far outnumber CJK."""
    if not text:
        return text
    hits = _CJK_RE.findall(text)
    if not hits:
        return text
    latin = len(_LATIN_RE.findall(text))
    if latin <= 4 * len(hits):  # not predominantly Latin -> probably real CJK
        return text
    out = _CJK_RE.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", out)


def parse_json_lenient(raw: str):
    """Parse JSON from model output, tolerating the usual LLM breakage.

    Cheap models routinely emit JSON with unquoted keys, missing quotes, or
    trailing junk. We try strict json first (fast path), then fall back to
    json-repair so a flaky generation degrades to a usable object instead of
    silently becoming an empty default. Returns the object or None.
    """
    if not raw:
        return None
    s = strip_reasoning(raw).strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    m = re.search(r"\{.*\}|\[.*\]", s, flags=re.DOTALL)
    candidate = m.group(0) if m else s
    for attempt in (candidate, s):
        try:
            return json.loads(attempt)
        except Exception:
            pass
    try:
        from json_repair import repair_json

        for attempt in (candidate, s):
            obj = repair_json(attempt, return_objects=True)
            if obj not in (None, "", [], {}):
                return obj
    except Exception:
        pass
    return None
