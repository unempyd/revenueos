"""CREATIVE — render a short advertisement from a structured brief, locally and deterministically.

This is a motion pipeline, not a slide renderer.  A brief describes a *timeline*: scenes with
keyframed properties, real easing curves, masked word reveals, counters, staggered entrances,
push-ins and hard cuts.  A headless browser is driven frame by frame — the page exposes
``window.seek(t)`` and the renderer calls it once per frame at a fixed timestep — so every frame is
genuine CSS motion at 30fps, and ffmpeg only muxes the finished frame sequence.

A scene may be a **video clip or a product screenshot**, trimmed and cropped by ffmpeg to the exact
pixels it will occupy, then composed in the page with typography over or beside it.  That is the
difference between an advertisement for software and a card with words on it.

What this is not
----------------
There is no image generation, no stock footage, no licensed music and no voice-over.  macOS `say`
was evaluated for narration and rejected: the installed voices (Samantha, Daniel and the novelty
set — none of the enhanced voices are present) read ad copy with the affect of a route
announcement and cannot be directed for emphasis, so the asset ships **silent**, which is also how
the overwhelming majority of feed advertising is watched.  Music, if it is ever added, has to be a
licensed track the business owns; RevenueOS will not synthesise one.  ffmpeg and the browser are
separate processes, which is also a clean licence boundary — no AGPL code or concept is used here.

Deterministic by construction
-----------------------------
No network, no model, no wall clock, no randomness.  The page never reads ``Date.now`` or
``requestAnimationFrame``: the renderer owns the clock and passes it in.  Media frames are
pre-extracted by ffmpeg so a video element is never seeked or played.  ffmpeg is called with
``-fflags +bitexact -flags:v +bitexact -map_metadata -1`` so no encoder string or creation time
leaks into the container.  The same brief renders byte-identical files on the same machine.

Contract (what a model must emit, and what ``parse_brief`` enforces)::

    {"title": "...",                   # 1-90 chars, names the asset
     "eyebrow": "...",                 # optional, <= 40 chars, a quiet label on the first scene
     "aspect": "landscape",            # landscape (1920x1080) | portrait (1080x1350)
     "scenes": [                       # 2-12 scenes, 3-90 seconds in total
       {"heading": "...",              # <= 72 chars
        "body": ["...", "..."],        # optional, <= 3 lines, <= 110 chars each
        "mono": "before 0 -> after 1", # optional, <= 52 chars, set in the mono face
        "count": {"to": 12, "label": "workers", "prefix": "", "suffix": ""},   # optional
        "media": {"source": "website/media/clip.mp4",   # optional, must exist in the workspace
                  "start": 2.0,        # trim in-point, seconds (video only)
                  "crop": [x, y, w, h]},                # optional, source pixels
        "caption": "...",              # optional, <= 64 chars, sits under the media card
        "transition": "cut",           # how this scene begins: cut | dissolve
        "align": "left",               # left | center
        "rule": false,                 # a hairline that wipes in under the heading
        "accent": false,               # at most ONE scene in the whole film may claim the accent
        "exit": "hold",                # hold | lift
        "seconds": 3.6}                # 0.8 - 12.0
     ]}

Anything outside those bounds is refused with one sentence rather than rendered as an overflowing
frame.  Failures never raise: ``render_video`` returns a ``RenderResult`` whose ``error`` is a
sentence naming what to install or fix.
"""
from __future__ import annotations

import base64
import glob
import html as _html
import json
import math
import os
import secrets
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# ── palette ────────────────────────────────────────────────────────────────────────────────────
# The tokens in website/design.css, so an asset RevenueOS renders reads as the same product as the
# site and the panel. Radii are restricted to 28 (card) and 980 (pill), as the design system says.
SANS = ('-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text",Inter,system-ui,'
        '"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif')
MONO = 'ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,"DejaVu Sans Mono",monospace'


@dataclass(frozen=True)
class Palette:
    black: str = "#000000"
    carbon: str = "#111111"
    obsidian: str = "#1d1d1f"
    ink: str = "#f5f5f7"
    muted: str = "#86868b"
    accent: str = "#0071e3"
    sans: str = SANS
    mono: str = MONO


DEFAULT_PALETTE = Palette()

# ── the brief ──────────────────────────────────────────────────────────────────────────────────
ASPECTS: dict[str, tuple[int, int]] = {"landscape": (1920, 1080), "portrait": (1080, 1350)}

MAX_TITLE = 90
MAX_EYEBROW = 40
MAX_HEADING = 72
MAX_BODY_LINES = 3
MAX_BODY_CHARS = 110
MAX_MONO = 52
MAX_CAPTION = 64
MAX_COUNT_LABEL = 48
MIN_SCENES, MAX_SCENES = 2, 12
MIN_SECONDS, MAX_SECONDS = 0.8, 12.0
MIN_TOTAL_SECONDS, MAX_TOTAL_SECONDS = 3.0, 90.0
FPS = 30

TRANSITIONS = ("cut", "dissolve")
ALIGNMENTS = ("left", "center")
EXITS = ("hold", "lift")
MEDIA_SUFFIXES = {".mp4": "video", ".mov": "video", ".webm": "video", ".m4v": "video",
                  ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image"}

DISSOLVE = 0.34   # seconds; a dissolve is a deliberate exception, a cut is the default
LIFT = 0.80       # seconds of exit drift — a lift, never a dip to black


class BriefError(ValueError):
    """A brief that cannot be rendered honestly. The message is one plain sentence for a human."""


@dataclass(frozen=True)
class Media:
    """A real clip or still from the workspace, trimmed and cropped to the pixels it will occupy."""

    source: str
    kind: str = "video"                       # video | image
    start: float = 0.0                        # trim in-point in seconds (video only)
    crop: tuple[int, int, int, int] | None = None   # x, y, w, h in source pixels

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"source": self.source}
        if self.start:
            out["start"] = self.start
        if self.crop:
            out["crop"] = list(self.crop)
        return out


@dataclass(frozen=True)
class Count:
    """A number that counts up on screen. Only ever a number the business context supports."""

    to: float
    label: str = ""
    prefix: str = ""
    suffix: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"to": self.to, "label": self.label, "prefix": self.prefix, "suffix": self.suffix}


@dataclass(frozen=True)
class Scene:
    heading: str = ""
    body: tuple[str, ...] = ()
    mono: str | None = None
    eyebrow: str = ""
    caption: str = ""
    count: Count | None = None
    media: Media | None = None
    seconds: float = 4.0
    transition: str = "cut"
    align: str = "left"
    rule: bool = False
    accent: bool = False
    overlay: bool = False
    exit: str = "hold"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"heading": self.heading, "seconds": self.seconds}
        if self.body:
            out["body"] = list(self.body)
        for key in ("mono", "eyebrow", "caption"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        if self.count:
            out["count"] = self.count.as_dict()
        if self.media:
            out["media"] = self.media.as_dict()
        if self.transition != "cut":
            out["transition"] = self.transition
        if self.align != "left":
            out["align"] = self.align
        for flag in ("rule", "accent", "overlay"):
            if getattr(self, flag):
                out[flag] = True
        if self.exit != "hold":
            out["exit"] = self.exit
        return out


@dataclass(frozen=True)
class Brief:
    title: str
    scenes: tuple[Scene, ...]
    eyebrow: str = ""
    aspect: str = "landscape"
    palette: Palette = DEFAULT_PALETTE

    @property
    def size(self) -> tuple[int, int]:
        return ASPECTS[self.aspect]

    @property
    def seconds(self) -> float:
        return round(sum(s.seconds for s in self.scenes), 3)

    @property
    def frames(self) -> int:
        return max(1, round(self.seconds * FPS))

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "eyebrow": self.eyebrow, "aspect": self.aspect,
                "scenes": [s.as_dict() for s in self.scenes]}


def _clean(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise BriefError(f"{what} must be text, not {type(value).__name__}")
    text = " ".join(value.replace(" ", " ").split())
    if any(ord(ch) < 32 for ch in text):
        raise BriefError(f"{what} contains a control character")
    return text


def _bounded(value: Any, what: str, limit: int) -> str:
    text = _clean(value, what)
    if len(text) > limit:
        raise BriefError(f"{what} is {len(text)} characters; keep it to {limit}")
    return text


def _seconds(value: Any, where: str) -> float:
    try:
        seconds = round(float(value), 2)
    except (TypeError, ValueError):
        raise BriefError(f"{where} has a duration that is not a number ({value!r})") from None
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise BriefError(f"{where} lasts {seconds:g}s; a scene must be between "
                         f"{MIN_SECONDS:g} and {MAX_SECONDS:g} seconds")
    return seconds


def _choice(value: Any, where: str, what: str, allowed: tuple[str, ...], default: str) -> str:
    text = _clean(value if value is not None else default, f"the {what} of {where}").lower()
    if text not in allowed:
        raise BriefError(f"{where} has an unknown {what} {text!r}; use one of {', '.join(allowed)}")
    return text


def _parse_count(raw: Any, where: str) -> Count | None:
    if raw in (None, {}, ""):
        return None
    if not isinstance(raw, dict):
        raise BriefError(f"the count of {where} must be an object with 'to' and 'label'")
    try:
        to = float(raw.get("to"))
    except (TypeError, ValueError):
        raise BriefError(f"the count of {where} has no numeric 'to' value") from None
    if not math.isfinite(to) or not 0 <= to <= 1e12:
        raise BriefError(f"the count of {where} is {to!r}; it must be a real number from 0 upwards")
    return Count(to=round(to, 2), label=_bounded(raw.get("label") or "", f"the count label of {where}",
                                                 MAX_COUNT_LABEL),
                 prefix=_bounded(raw.get("prefix") or "", f"the count prefix of {where}", 4),
                 suffix=_bounded(raw.get("suffix") or "", f"the count suffix of {where}", 8))


def _parse_media(raw: Any, where: str, media_root: Path | None) -> Media | None:
    if raw is None or raw == "":
        return None  # an empty object is not "no media", it is an unfinished shot: it is refused
    if isinstance(raw, str):
        raw = {"source": raw}
    if not isinstance(raw, dict):
        raise BriefError(f"the media of {where} must be an object with a 'source' path")
    source = _clean(raw.get("source") or "", f"the media source of {where}")
    if not source:
        raise BriefError(f"the media of {where} has no 'source'")
    path = Path(source)
    if path.is_absolute() or ".." in path.parts:
        raise BriefError(f"the media source of {where} must be a path inside the workspace, "
                         f"not {source!r}")
    kind = MEDIA_SUFFIXES.get(path.suffix.lower())
    if not kind:
        raise BriefError(f"the media source of {where} is {path.suffix or 'extensionless'}; use one "
                         f"of {', '.join(sorted(MEDIA_SUFFIXES))}")
    try:
        start = round(float(raw.get("start") or 0.0), 3)
    except (TypeError, ValueError):
        raise BriefError(f"the media start of {where} is not a number") from None
    if start < 0:
        raise BriefError(f"the media start of {where} is negative")
    crop_raw = raw.get("crop")
    crop: tuple[int, int, int, int] | None = None
    if crop_raw:
        if not isinstance(crop_raw, (list, tuple)) or len(crop_raw) != 4:
            raise BriefError(f"the media crop of {where} must be [x, y, width, height] in source pixels")
        try:
            values = [int(v) for v in crop_raw]
        except (TypeError, ValueError):
            raise BriefError(f"the media crop of {where} must be four whole numbers") from None
        if min(values) < 0 or values[2] < 16 or values[3] < 16:
            raise BriefError(f"the media crop of {where} is smaller than 16px or has a negative edge")
        crop = (values[0], values[1], values[2], values[3])
    if media_root is not None and not (media_root / path).is_file():
        raise BriefError(f"the media of {where} is not in the workspace: {source}")
    return Media(source=source, kind=kind, start=start, crop=crop)


def parse_brief(data: Any, *, palette: Palette | None = None, media_root: Path | str | None = None) -> Brief:
    """Validate a brief written by a model (or by hand) into a `Brief`.

    Raises `BriefError` with one sentence saying exactly what is wrong. Never repairs silently:
    a heading that does not fit is a bad brief, not a smaller font. When `media_root` is given,
    every media path is checked to exist inside it, so a hallucinated clip is refused here rather
    than becoming a black rectangle in the finished film.
    """
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise BriefError(f"the brief is not valid JSON ({exc.msg} at line {exc.lineno})") from None
    if not isinstance(data, dict):
        raise BriefError("the brief must be a JSON object with 'title' and 'scenes', "
                         f"not {type(data).__name__}")

    root = Path(media_root) if media_root is not None else None
    title = _bounded(data.get("title", ""), "the brief title", MAX_TITLE)
    if not title:
        raise BriefError("the brief has no title")
    eyebrow = _bounded(data.get("eyebrow") or "", "the eyebrow", MAX_EYEBROW)

    aspect = _clean(data.get("aspect") or "landscape", "the aspect").lower()
    if aspect not in ASPECTS:
        raise BriefError(f"unknown aspect {aspect!r}; use one of {', '.join(sorted(ASPECTS))}")

    raw_scenes = data.get("scenes")
    if not isinstance(raw_scenes, list):
        raise BriefError("the brief has no 'scenes' list")
    if not MIN_SCENES <= len(raw_scenes) <= MAX_SCENES:
        raise BriefError(f"the brief has {len(raw_scenes)} scene(s); a film needs between "
                         f"{MIN_SCENES} and {MAX_SCENES}")

    scenes: list[Scene] = []
    for i, raw in enumerate(raw_scenes, start=1):
        where = f"scene {i}"
        if not isinstance(raw, dict):
            raise BriefError(f"{where} is not an object")
        heading = _bounded(raw.get("heading") or "", f"the heading of {where}", MAX_HEADING)
        raw_body = raw.get("body") or []
        if isinstance(raw_body, str):
            raw_body = [raw_body]
        if not isinstance(raw_body, list):
            raise BriefError(f"the body of {where} must be a list of lines")
        body = [line for line in (_clean(ln, f"a body line of {where}") for ln in raw_body) if line]
        if len(body) > MAX_BODY_LINES:
            raise BriefError(f"{where} has {len(body)} body lines; keep it to {MAX_BODY_LINES}")
        for line in body:
            if len(line) > MAX_BODY_CHARS:
                raise BriefError(f"a body line of {where} is {len(line)} characters; "
                                 f"keep it to {MAX_BODY_CHARS}")
        count = _parse_count(raw.get("count"), where)
        media = _parse_media(raw.get("media"), where, root)
        scene = Scene(
            heading=heading,
            body=tuple(body),
            mono=_bounded(raw.get("mono") or "", f"the mono line of {where}", MAX_MONO) or None,
            eyebrow=_bounded(raw.get("eyebrow") or "", f"the eyebrow of {where}", MAX_EYEBROW),
            caption=_bounded(raw.get("caption") or "", f"the caption of {where}", MAX_CAPTION),
            count=count,
            media=media,
            seconds=_seconds(raw.get("seconds", 4.0), where),
            transition=_choice(raw.get("transition"), where, "transition", TRANSITIONS, "cut"),
            align=_choice(raw.get("align"), where, "alignment", ALIGNMENTS, "left"),
            rule=bool(raw.get("rule")),
            accent=bool(raw.get("accent")),
            overlay=bool(raw.get("overlay")),
            exit=_choice(raw.get("exit"), where, "exit", EXITS, "hold"),
        )
        if not (scene.heading or scene.count or scene.media):
            raise BriefError(f"{where} has nothing in it; give it a heading, a count or some media")
        if scene.caption and not scene.media:
            raise BriefError(f"{where} has a caption but no media for it to caption")
        if scene.caption and scene.overlay:
            # Over a full-frame plate there is no empty band to caption into: the line lands on
            # whatever the recording happens to be showing and both become unreadable. Seen in a
            # real render before this check existed. The heading is the caption in overlay mode.
            raise BriefError(
                f"{where} cannot have a caption in overlay mode: the type sits on the footage, so a "
                "caption would land on top of it. Put it in the heading, or drop overlay.")
        if scene.overlay and not scene.media:
            raise BriefError(f"{where} asks for overlaid type but has no media to overlay it on")
        if i == 1 and scene.transition == "dissolve":
            raise BriefError("scene 1 cannot dissolve; there is nothing before it to dissolve from")
        scenes.append(scene)

    accents = [i for i, s in enumerate(scenes, start=1) if s.accent]
    if len(accents) > 1:
        raise BriefError(f"scenes {', '.join(str(i) for i in accents)} all claim the accent colour; "
                         "exactly one moment in a film may be accented")

    brief = Brief(title=title, scenes=tuple(scenes), eyebrow=eyebrow, aspect=aspect,
                  palette=palette or DEFAULT_PALETTE)
    if brief.seconds > MAX_TOTAL_SECONDS:
        raise BriefError(f"the brief runs {brief.seconds:g}s in total; keep it under "
                         f"{MAX_TOTAL_SECONDS:g} seconds")
    if brief.seconds < MIN_TOTAL_SECONDS:
        raise BriefError(f"the brief runs {brief.seconds:g}s in total; an advertisement needs at "
                         f"least {MIN_TOTAL_SECONDS:g} seconds")
    return brief


# ── layout ─────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Metrics:
    pad: int
    headings: tuple[int, ...]
    heading_lines: int
    body: int
    mono: int
    eyebrow: int
    caption: int
    count: int
    gap: int
    card: int          # the widest a media card may be
    card_height: int   # the tallest a media card may be


_METRICS: dict[str, _Metrics] = {
    "landscape": _Metrics(pad=104, headings=(104, 86, 72, 60), heading_lines=2, body=34, mono=30,
                          eyebrow=25, caption=25, count=368, gap=34, card=1600, card_height=980),
    "portrait": _Metrics(pad=76, headings=(86, 74, 62, 52), heading_lines=3, body=30, mono=26,
                         eyebrow=21, caption=22, count=268, gap=30, card=928, card_height=1120),
}
# Mean advance width of the SF Pro / system face at weight 700, as a fraction of the em. Used only
# to choose a heading size that fits the allowed number of lines; it is a bound, not a typesetter.
_EM_RATIO = 0.48
_CARD_RADIUS = 28
_PUSH = 1.055   # how far a push-in travels across a scene
# Real pixels are the point of using real footage, so a plate is never blown up far. 1.3x of a
# clean screen recording still reads as video; past that it reads as a mistake.
_MAX_UPSCALE = 1.3
_OVERLAY_MARGIN = 56   # how much of the ground stays visible around a full-frame plate


def _overlays(scene: Scene, aspect: str) -> bool:
    """Type sits *on* the plate only where the plate can fill the frame. A landscape clip covers a
    landscape frame; in portrait it cannot, so the scene falls back to type above the plate rather
    than type stranded on black below it."""
    return bool(scene.overlay and scene.media and aspect == "landscape")


def _heading_box(text: str, content_width: int, m: _Metrics, max_lines: int) -> tuple[int, int]:
    """The largest size at which `text` fits in `max_lines` of `content_width`, and the height it
    then takes. A bound, not a typesetter: it only has to keep the composition inside the frame."""
    for size in (*m.headings, m.headings[-1]):
        per = max(1.0, content_width / (_EM_RATIO * size))
        lines = max(1, math.ceil(len(text) / per))
        if lines <= max_lines or size == m.headings[-1]:
            return size, int(size * 1.05 * min(lines, max_lines))
    return m.headings[-1], int(m.headings[-1] * 1.05)


def _card_box(media_w: int, media_h: int, m: _Metrics, head_h: int, caption: bool,
              width: int, height: int, overlay: bool) -> tuple[int, int]:
    """The on-screen size of a media card. Under an overlay the type sits *on* the plate, so the
    plate takes the frame; otherwise it takes whatever the type has left."""
    if overlay:
        limit_w, limit_h = width - 2 * _OVERLAY_MARGIN, height - 2 * _OVERLAY_MARGIN
    else:
        gap = int(m.gap * 0.62)
        room = height - 2 * m.pad - (head_h + m.gap if head_h else 0) \
            - (int(m.caption * 1.4) + gap if caption else 0)
        limit_w, limit_h = m.card, min(m.card_height, max(160, room))
    scale = min(limit_w / media_w, limit_h / media_h, _MAX_UPSCALE)
    return max(64, int(media_w * scale)) // 2 * 2, max(48, int(media_h * scale)) // 2 * 2


# ── the page ───────────────────────────────────────────────────────────────────────────────────
# Timings, in seconds from the start of a scene. Nothing enters at the same moment as anything
# else: the eye is led down the frame.
_T_EYEBROW = (0.00, 0.52)
_T_HEADING = (0.10, 0.78)
_WORD_STEP = 0.045
_T_BODY_GAP, _T_BODY_DUR, _BODY_STEP = 0.16, 0.60, 0.09
_T_MONO_GAP, _T_MONO_DUR = 0.12, 0.62
_T_RULE = (0.26, 0.70)
_T_COUNT = (0.16, 1.25)
_T_CAPTION = (0.44, 0.55)


def _esc(text: str) -> str:
    return _html.escape(text, quote=True)


def _anim(kind: str, delay: float, dur: float, *, ease: str = "expo", **extra: Any) -> str:
    bits = [f'data-anim="{kind}"', f'data-delay="{delay:.3f}"', f'data-dur="{dur:.3f}"',
            f'data-ease="{ease}"']
    bits += [f'data-{k.replace("_", "-")}="{v}"' for k, v in extra.items()]
    return " ".join(bits)


def _words_html(text: str, start: float) -> tuple[str, float]:
    """A heading split into per-word masks. Returns the markup and when the last word starts."""
    words = text.split(" ")
    spans = []
    for i, word in enumerate(words):
        delay = start + i * _WORD_STEP
        spans.append(f'<span class="wm"><span class="wi" '
                     f'{_anim("rise", delay, _T_HEADING[1], y="118%")}>{_esc(word)}</span></span>')
    return " ".join(spans), start + max(0, len(words) - 1) * _WORD_STEP


def _scene_markup(brief: Brief, index: int, plan: dict[str, Any]) -> str:
    """One scene as an absolutely positioned layer. Pure function of the brief and its media plan."""
    scene = brief.scenes[index]
    m = _METRICS[brief.aspect]
    eyebrow = scene.eyebrow or (brief.eyebrow if index == 0 else "")
    # A dissolve is between two *pictures*. Holding every entrance back until the mix has finished
    # is what stops two headings crossing each other in the middle of it.
    off = DISSOLVE if (scene.transition == "dissolve" and index) else 0.0
    parts: list[str] = []

    if eyebrow:
        parts.append(f'<div class="eyebrow" '
                     f'{_anim("rise", off + _T_EYEBROW[0], _T_EYEBROW[1], y="14px", ease="soft")}>'
                     f'{_esc(eyebrow)}</div>')

    heading_end = _T_HEADING[0]
    if scene.count:
        c = scene.count
        parts.append(f'<div class="count" {_anim("count", off + _T_COUNT[0], _T_COUNT[1], to=c.to)}'
                     f' data-prefix="{_esc(c.prefix)}" data-suffix="{_esc(c.suffix)}">'
                     f'{_esc(c.prefix)}0{_esc(c.suffix)}</div>')
        if c.label:
            parts.append(f'<div class="count-label" '
                         f'{_anim("rise", off + 0.34, 0.60, y="16px", ease="soft")}>'
                         f'{_esc(c.label)}</div>')
        heading_end = 0.40

    if scene.heading:
        words, heading_end = _words_html(scene.heading,
                                         off + (_T_HEADING[0] if not scene.count else 0.52))
        parts.append(f'<h1>{words}</h1>')

    if scene.rule:
        parts.append(f'<div class="rule" {_anim("wipe", off + _T_RULE[0], _T_RULE[1], ease="inout")}>'
                     f'</div>')

    body_end = heading_end
    if scene.body:
        lines = []
        for i, line in enumerate(scene.body):
            delay = max(heading_end, off) + _T_BODY_GAP + i * _BODY_STEP
            body_end = delay
            lines.append(f'<p {_anim("rise", delay, _T_BODY_DUR, y="18px", ease="soft")}>'
                         f'{_esc(line)}</p>')
        parts.append(f'<div class="body">{"".join(lines)}</div>')

    if scene.mono:
        parts.append(f'<div class="chip" '
                     f'{_anim("wipe", body_end + _T_MONO_GAP, _T_MONO_DUR, ease="inout")}>'
                     f'<span>{_esc(scene.mono)}</span></div>')

    stage = f'<div class="stage">{"".join(parts)}</div>' if parts else ""

    overlay = _overlays(scene, brief.aspect)
    caption = (f'<div class="caption" '
               f'{_anim("rise", off + _T_CAPTION[0], _T_CAPTION[1], y="12px", ease="soft")}>'
               f'{_esc(scene.caption)}</div>') if scene.caption and scene.media else ""
    if overlay and caption:  # over a plate the caption leads the type, it does not trail it
        stage = f'<div class="stage">{caption}{"".join(parts)}</div>'
        caption = ""
    card = ""
    if scene.media and plan:
        card = (f'<div class="media">'
                f'<div class="card" style="width:{plan["display_w"]}px;height:{plan["display_h"]}px" '
                f'{_anim("push", 0.0, scene.seconds, ease="soft", scale_from=1.0, scale_to=_PUSH)}>'
                f'<img class="plate" data-media="{plan["dir"]}" data-frames="{plan["frames"]}" alt="">'
                f'</div>{caption}</div>')

    exit_at = f' data-exit="{max(0.0, scene.seconds - LIFT):.3f}"' if scene.exit == "lift" else ""
    classes = f'layer a-{scene.align}{" accent" if scene.accent else ""}' \
              f'{" with-media" if card else ""}{" overlay" if overlay and card else ""}'
    # under an overlay the plate is the ground and the type is painted on it, so it is laid first
    inner = f"{card}{stage}" if (overlay and card) else f"{stage}{card}"
    return (f'<section class="{classes}" style="--h:{plan.get("heading_px", m.headings[1])}px"'
            f' data-start="{plan.get("start", 0.0):.3f}" data-end="{plan.get("end", 0.0):.3f}"'
            f' data-dissolve="{DISSOLVE if scene.transition == "dissolve" and index else 0.0:.3f}"'
            f'{exit_at}>{inner}</section>')


def _css(brief: Brief) -> str:
    width, height = brief.size
    m = _METRICS[brief.aspect]
    p = brief.palette
    content = width - 2 * m.pad
    return f"""
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:{width}px;height:{height}px;overflow:hidden;background:{p.black}}}
body{{font-family:{p.sans};-webkit-font-smoothing:antialiased;color:{p.ink}}}
.layer{{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:center;
  padding:{m.pad}px;background:{p.black};will-change:opacity,transform;opacity:0;display:none}}
/* one restrained wash so the frame is not a flat rectangle; no shadows, per the design system */
.layer::before{{content:"";position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(118% 78% at 10% -6%, {p.carbon} 0%, {p.black} 60%)}}
.stage{{position:relative;display:flex;flex-direction:column;max-width:{content}px;
  gap:{m.gap}px;align-items:flex-start}}
.a-center .stage{{align-items:center;text-align:center;margin:0 auto}}
.a-center{{align-items:center}}
.with-media .stage{{flex:0 0 auto}}
.with-media{{gap:{m.gap}px}}
.a-left .media{{margin-right:auto;margin-left:0}}
.eyebrow{{font-size:{m.eyebrow}px;font-weight:600;letter-spacing:.005em;color:{p.muted};
  will-change:transform,opacity}}
h1{{font-size:var(--h);font-weight:700;line-height:1.05;letter-spacing:-.021em;color:{p.ink};
  max-width:{content}px}}
.accent h1{{color:{p.accent}}}
.wm{{display:inline-block;overflow:hidden;vertical-align:bottom;
  padding-bottom:.18em;margin-bottom:-.18em}}
.wi{{display:inline-block;will-change:transform,opacity}}
.rule{{height:2px;width:{int(content * 0.34)}px;background:{p.obsidian};align-self:stretch;
  max-width:{int(content * 0.34)}px;will-change:clip-path}}
.accent .rule{{background:{p.accent}}}
.body{{display:flex;flex-direction:column;gap:{max(8, m.body // 3)}px;
  font-size:{m.body}px;font-weight:400;line-height:1.4;letter-spacing:-.012em;color:{p.muted};
  max-width:{int(content * 0.78)}px}}
.a-center .body{{margin:0 auto}}
.body p{{will-change:transform,opacity}}
.chip{{background:{p.obsidian};border-radius:{_CARD_RADIUS}px;padding:{m.mono - 6}px {int(m.mono * 1.4)}px;
  font-family:{p.mono};font-size:{m.mono}px;letter-spacing:0;color:{p.ink};white-space:nowrap;
  will-change:clip-path}}
.count{{font-size:{m.count}px;font-weight:700;line-height:.92;letter-spacing:-.045em;
  font-variant-numeric:tabular-nums;color:{p.ink}}}
.accent .count{{color:{p.accent}}}
.count-label{{font-size:{int(m.body * 1.12)}px;color:{p.ink};letter-spacing:-.014em;
  will-change:transform,opacity}}
.media{{position:relative;display:flex;flex-direction:column;align-items:center;
  gap:{int(m.gap * 0.62)}px;margin:0 auto}}
.card{{position:relative;overflow:hidden;border-radius:{_CARD_RADIUS}px;background:{p.carbon};
  border:1px solid rgba(255,255,255,.10);will-change:transform}}
.card .plate{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}}
.caption{{font-family:{p.mono};font-size:{m.caption}px;letter-spacing:0;color:{p.muted};
  will-change:transform,opacity}}
/* an overlay: the plate is the ground, a scrim carries the type, and nothing is centred over a UI.
   The scrim has to clear the CAPTION as well as the heading — it leads the type over a plate — or
   the caption lands on whatever the recording happens to be showing and both become unreadable. */
.overlay{{padding:0;justify-content:center}}
.overlay .media{{position:absolute;inset:0;justify-content:center;margin:0}}
.overlay::after{{content:"";position:absolute;inset:0;z-index:1;pointer-events:none;
  background:linear-gradient(to top,rgba(0,0,0,.97) 0%,rgba(0,0,0,.95) 24%,
    rgba(0,0,0,.86) 38%,rgba(0,0,0,.5) 52%,rgba(0,0,0,0) 68%)}}
.overlay .stage{{position:absolute;z-index:2;left:{m.pad}px;right:{m.pad}px;bottom:{m.pad}px;
  align-items:flex-start;text-align:left;max-width:{content}px;gap:{int(m.gap * 0.55)}px;margin:0}}
.overlay .caption{{order:-1}}
"""


# The animator. It never reads a clock: the renderer owns time and calls seek(t) per frame.
_RUNTIME = r"""
(function () {
  var CURVES = {
    expo:  [0.16, 1.00, 0.30, 1.00],
    soft:  [0.33, 1.00, 0.68, 1.00],
    inout: [0.65, 0.00, 0.35, 1.00],
    linear: null
  };
  function bezier(p) {
    if (!p) { return function (x) { return x; }; }
    var x1 = p[0], y1 = p[1], x2 = p[2], y2 = p[3];
    var cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx;
    var cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
    function fx(t) { return ((ax * t + bx) * t + cx) * t; }
    function dx(t) { return (3 * ax * t + 2 * bx) * t + cx; }
    return function (x) {
      if (x <= 0) return 0;
      if (x >= 1) return 1;
      var t = x, i, e, d;
      for (i = 0; i < 8; i++) {
        e = fx(t) - x;
        if (Math.abs(e) < 1e-9) break;
        d = dx(t);
        if (Math.abs(d) < 1e-9) break;
        t -= e / d;
      }
      if (t < 0 || t > 1 || Math.abs(fx(t) - x) > 1e-7) {
        var lo = 0, hi = 1, mid;
        for (i = 0; i < 40; i++) {
          mid = (lo + hi) / 2;
          if (fx(mid) < x) lo = mid; else hi = mid;
        }
        t = (lo + hi) / 2;
      }
      return ((ay * t + by) * t + cy) * t;
    };
  }
  var EASE = {};
  for (var key in CURVES) EASE[key] = bezier(CURVES[key]);
  function clamp01(v) { return v < 0 ? 0 : (v > 1 ? 1 : v); }
  function pad5(n) { n = String(n); while (n.length < 5) n = "0" + n; return n; }
  function commas(n) {
    var s = String(n), out = "", i, c = 0;
    for (i = s.length - 1; i >= 0; i--) {
      out = s.charAt(i) + out;
      if (++c % 3 === 0 && i > 0) out = "," + out;
    }
    return out;
  }

  var FPS = window.__FPS__;
  var layers = [].slice.call(document.querySelectorAll(".layer"));
  layers.forEach(function (layer) {
    layer.__items = [].slice.call(layer.querySelectorAll("[data-anim]"));
    layer.__plates = [].slice.call(layer.querySelectorAll("[data-media]"));
    layer.__start = parseFloat(layer.dataset.start);
    layer.__end = parseFloat(layer.dataset.end);
    layer.__dissolve = parseFloat(layer.dataset.dissolve || "0");
    layer.__exit = layer.dataset.exit === undefined ? -1 : parseFloat(layer.dataset.exit);
  });

  function progress(el, u) {
    var delay = parseFloat(el.dataset.delay || "0");
    var dur = parseFloat(el.dataset.dur || "0.6");
    var ease = EASE[el.dataset.ease || "expo"] || EASE.expo;
    if (dur <= 0) return u >= delay ? 1 : 0;
    return ease(clamp01((u - delay) / dur));
  }

  function apply(el, u) {
    var p = progress(el, u), kind = el.dataset.anim;
    if (kind === "rise") {
      el.style.opacity = clamp01(p * 1.7);
      el.style.transform = "translate3d(0," + ((1 - p) * parseFloat(el.dataset.y)) +
        (el.dataset.y.indexOf("%") > -1 ? "%" : "px") + ",0)";
    } else if (kind === "fade") {
      el.style.opacity = p;
    } else if (kind === "wipe") {
      el.style.clipPath = "inset(0 " + ((1 - p) * 100).toFixed(3) + "% 0 0)";
      el.style.opacity = clamp01(p * 8);
    } else if (kind === "push") {
      var a = parseFloat(el.dataset.scaleFrom), b = parseFloat(el.dataset.scaleTo);
      el.style.transform = "scale(" + (a + (b - a) * p).toFixed(5) + ")";
    } else if (kind === "count") {
      var to = parseFloat(el.dataset.to);
      var shown = to >= 100 ? Math.round(to * p) : Math.round(to * p * 10) / 10;
      var text = (Math.abs(to % 1) < 1e-9) ? commas(Math.round(to * p)) : shown.toFixed(1);
      el.textContent = (el.dataset.prefix || "") + text + (el.dataset.suffix || "");
    }
  }

  var pending = [];
  function plate(img, u) {
    var frames = parseInt(img.dataset.frames, 10);
    var i = Math.floor(u * FPS);
    if (i < 0) i = 0;
    if (i > frames - 1) i = frames - 1;
    var src = img.dataset.media + "/" + pad5(i + 1) + ".jpg";
    if (img.__src !== src) {
      img.__src = src;
      img.setAttribute("src", src);
      pending.push(img.decode().catch(function () { return null; }));
    }
  }

  window.seek = function (t) {
    pending = [];
    var i, cur = 0;
    for (i = 0; i < layers.length; i++) {
      if (t >= layers[i].__start - 1e-6) cur = i;
    }
    var vis = [];
    for (i = 0; i < layers.length; i++) vis.push(0);
    vis[cur] = 1;
    var active = layers[cur], d = active.__dissolve;
    if (d > 0 && cur > 0 && t < active.__start + d) {
      var p = EASE.inout(clamp01((t - active.__start) / d));
      vis[cur] = Math.sqrt(p);
      vis[cur - 1] = Math.sqrt(1 - p);
    }
    for (i = 0; i < layers.length; i++) {
      var layer = layers[i];
      if (vis[i] <= 0) {
        if (layer.style.display !== "none") layer.style.display = "none";
        continue;
      }
      var u = t - layer.__start, alpha = vis[i], lift = 0;
      if (layer.__exit >= 0 && u > layer.__exit) {
        var q = EASE.soft(clamp01((u - layer.__exit) / """ + f"{LIFT}" + r"""));
        lift = -q * 26;
      }
      layer.style.display = "flex";
      layer.style.opacity = alpha.toFixed(5);
      layer.style.transform = lift ? "translate3d(0," + lift.toFixed(3) + "px,0)" : "none";
      for (var j = 0; j < layer.__items.length; j++) apply(layer.__items[j], u);
      for (var k = 0; k < layer.__plates.length; k++) plate(layer.__plates[k], u);
    }
    // A decoded image is not yet a painted one. Two animation frames guarantee the compositor has
    // drawn this seek before the renderer screenshots it; without this, a frame that swaps a plate
    // occasionally captures the previous one, and the render stops being reproducible.
    return Promise.all(pending).then(function () {
      return new Promise(function (done) {
        requestAnimationFrame(function () { requestAnimationFrame(function () { done(1); }); });
      });
    });
  };
  window.__ready = document.fonts.ready.then(function () { return window.seek(0); });
})();
"""


def timeline_html(brief: Brief, plans: list[dict[str, Any]] | None = None) -> str:
    """The whole film as one seekable document. Pure function of the brief and its media plans."""
    if plans is None:
        plans = _plans(brief)
    layers = "".join(_scene_markup(brief, i, plans[i]) for i in range(len(brief.scenes)))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<title>{_esc(brief.title)}</title><style>{_css(brief)}</style></head><body>'
            f'{layers}<script>window.__FPS__={FPS};{_RUNTIME}</script></body></html>')


def _plans(brief: Brief, sizes: dict[int, tuple[int, int]] | None = None) -> list[dict[str, Any]]:
    """Per-scene timing, plus the on-screen box of any media. `sizes` carries the real pixel size of
    each scene's source, read by ffprobe; without it a 16:10 default is assumed (layout only)."""
    width, height = brief.size
    m = _METRICS[brief.aspect]
    content = width - 2 * m.pad
    plans: list[dict[str, Any]] = []
    at = 0.0
    for i, scene in enumerate(brief.scenes):
        plan: dict[str, Any] = {"start": round(at, 3), "end": round(at + scene.seconds, 3)}
        head_px, head_h = _heading_box(scene.heading, content, m, m.heading_lines) \
            if scene.heading else (m.headings[1], 0)
        plan["heading_px"] = head_px
        if scene.media:
            src_w, src_h = (sizes or {}).get(i, (1600, 1000))
            if scene.media.crop:
                src_w, src_h = scene.media.crop[2], scene.media.crop[3]
            disp_w, disp_h = _card_box(src_w, src_h, m, head_h, bool(scene.caption),
                                       width, height, _overlays(scene, brief.aspect))
            plan.update(display_w=disp_w, display_h=disp_h,
                        # extracted a little larger than shown so the push-in downsamples, never up
                        plate_w=int(disp_w * _PUSH) // 2 * 2, plate_h=int(disp_h * _PUSH) // 2 * 2,
                        frames=max(1, round(scene.seconds * FPS)), dir="")
        plans.append(plan)
        at += scene.seconds
    return plans


# ── media: ffmpeg turns a clip or a still into exactly the frames the page will show ────────────
_MISSING_FFMPEG = (
    "ffmpeg was not found, so nothing can be assembled — install it (macOS: `brew install ffmpeg`; "
    "Debian/Ubuntu: `sudo apt install ffmpeg`), or set REVENUEOS_FFMPEG to its path."
)
_BITEXACT = ["-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1"]


def ffmpeg_path() -> str | None:
    env = os.environ.get("REVENUEOS_FFMPEG")
    if env:
        return env if Path(env).is_file() else None
    return shutil.which("ffmpeg")


def ffprobe_path() -> str | None:
    env = os.environ.get("REVENUEOS_FFPROBE")
    if env:
        return env if Path(env).is_file() else None
    ffmpeg = ffmpeg_path()
    if ffmpeg:
        sibling = Path(ffmpeg).with_name("ffprobe")
        if sibling.is_file():
            return str(sibling)
    return shutil.which("ffprobe")


def _run(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False,
                              stdin=subprocess.DEVNULL)
    except OSError as exc:  # a configured path that is not an executable is a sentence, not an errno
        raise RuntimeError(f"{Path(cmd[0]).name} could not be run at {cmd[0]} "
                           f"({exc.strerror or exc})") from None


def media_size(path: Path) -> tuple[int, int] | None:
    """The real pixel size of a clip or still, as ffprobe reads it."""
    ffprobe = ffprobe_path()
    if not ffprobe or not path.is_file():
        return None
    proc = _run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height", "-of", "csv=p=0:s=x", str(path)], timeout=60)
    try:
        w, h = proc.stdout.strip().splitlines()[0].split("x")
        return int(w), int(h)
    except (IndexError, ValueError):
        return None


def _extract(ffmpeg: str, media: Media, source: Path, plan: dict[str, Any], out: Path) -> int:
    """Write exactly `plan['frames']` JPEGs of the scene's media at the size it will be shown."""
    out.mkdir(parents=True, exist_ok=True)
    chain = []
    if media.crop:
        x, y, w, h = media.crop
        chain.append(f"crop={w}:{h}:{x}:{y}")
    chain.append(f"scale={plan['plate_w']}:{plan['plate_h']}:flags=lanczos")
    frames = int(plan["frames"])
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if media.kind == "video":
        # -ss after -i is frame-accurate (and the clips here are short), which matters because the
        # brief names a moment in the footage, not a keyframe near it.
        cmd += ["-i", str(source), "-ss", f"{media.start:.3f}"]
        chain.insert(0, f"fps={FPS}")
    else:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-i", str(source)]
    cmd += ["-vf", ",".join(chain), "-frames:v", str(frames), "-q:v", "2", "-pix_fmt", "yuvj420p",
            *_BITEXACT, str(out / "%05d.jpg")]
    proc = _run(cmd, timeout=600)
    written = len(list(out.glob("*.jpg")))
    if proc.returncode != 0 or written == 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(f"ffmpeg could not read {media.source}" + (f": {tail[-1]}" if tail else ""))
    if written < frames:  # a clip that runs out simply freezes on its last frame
        last = out / f"{written:05d}.jpg"
        for i in range(written + 1, frames + 1):
            shutil.copyfile(last, out / f"{i:05d}.jpg")
    return frames


# ── the browser, driven frame by frame over CDP ─────────────────────────────────────────────────
_CHROMIUM_GLOBS = (
    "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
    "~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
    "~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux*/chrome-headless-shell",
    "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome",
    "/ms-playwright/chromium_headless_shell-*/chrome-linux*/chrome-headless-shell",
    "/ms-playwright/chromium-*/chrome-linux*/chrome",
)
_CHROMIUM_NAMES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome")
_MISSING_BROWSER = (
    "no headless browser was found to render the frames — install one with "
    "`npx playwright install chromium`, or point REVENUEOS_BROWSER at a Chromium/Chrome binary."
)


@dataclass(frozen=True)
class Browser:
    """How this machine renders a frame: a Chromium binary driven directly over the DevTools
    protocol. No Node, no Playwright package, no npm install — only the browser itself."""

    kind: str = "chromium"
    chromium: str | None = None

    def describe(self) -> str:
        return f"chromium/cdp ({self.chromium})"


def _chromium_binary() -> str | None:
    env = os.environ.get("REVENUEOS_BROWSER")
    if env and Path(env).expanduser().is_file():
        return str(Path(env).expanduser())
    for pattern in _CHROMIUM_GLOBS:
        hits = sorted(glob.glob(os.path.expanduser(pattern)))
        if hits:
            return hits[-1]  # the newest revision
    for name in _CHROMIUM_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def find_browser(workspace: Path | None = None) -> Browser | None:
    """The Chromium binary this machine can drive, or None when it can render nothing."""
    binary = _chromium_binary()
    return Browser(kind="chromium", chromium=binary) if binary else None


class _Socket:
    """The smallest WebSocket client that can carry DevTools traffic. Stdlib only."""

    def __init__(self, url: str, timeout: float = 120.0) -> None:
        if not url.startswith("ws://"):
            raise RuntimeError(f"the browser offered an unusable debugger address ({url})")
        hostport, _, path = url[5:].partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("the browser closed the debugger connection during the handshake")
            buf += chunk
        head, _, self.buf = buf.partition(b"\r\n\r\n")
        if b" 101" not in head.split(b"\r\n")[0]:
            raise RuntimeError("the browser refused the debugger connection")

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise RuntimeError("the browser closed the debugger connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text: str) -> None:
        data = text.encode()
        mask = secrets.token_bytes(4)
        n = len(data)
        if n < 126:
            head = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> str:
        parts: list[bytes] = []
        while True:
            b0, b1 = self._read(2)
            fin, opcode, length = b0 & 0x80, b0 & 0x0F, b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            payload = self._read(length)
            if opcode == 0x8:
                raise RuntimeError("the browser closed the debugger connection")
            if opcode == 0x9:  # ping — answered with an empty pong so the socket stays open
                self.sock.sendall(struct.pack("!BB", 0x8A, 0x80) + secrets.token_bytes(4))
                continue
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class _Chromium:
    """A headless Chromium held open for the whole render, with a deterministic seek/shoot loop."""

    def __init__(self, binary: str, width: int, height: int) -> None:
        self.profile = Path(tempfile.mkdtemp(prefix="revenueos-chrome-"))
        args = [binary, "--remote-debugging-port=0", f"--user-data-dir={self.profile}",
                "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                "--hide-scrollbars", "--force-color-profile=srgb", "--font-render-hinting=none",
                "--disable-lcd-text", "--allow-file-access-from-files", "--no-sandbox",
                "--disable-dev-shm-usage", "--disable-gpu", "--mute-audio",
                "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                "--run-all-compositor-stages-before-draw", f"--window-size={width},{height}",
                "about:blank"]
        if "headless-shell" not in Path(binary).name:
            args.insert(1, "--headless=new")
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         stdin=subprocess.DEVNULL)
        except OSError as exc:
            shutil.rmtree(self.profile, ignore_errors=True)
            raise RuntimeError(f"the headless browser could not be started at {binary} "
                               f"({exc.strerror or exc})") from None
        port = self._await_port()
        target = self._page_target(port)
        self.ws = _Socket(target)
        self._id = 0
        self.call("Page.enable")
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                  deviceScaleFactor=1, mobile=False)

    def _await_port(self) -> int:
        marker = self.profile / "DevToolsActivePort"
        for _ in range(400):
            if self.proc.poll() is not None:
                raise RuntimeError("the headless browser exited before it opened a debugger port")
            if marker.is_file():
                text = marker.read_text(encoding="utf-8", errors="replace")
                if "\n" in text:
                    return int(text.splitlines()[0])
            time.sleep(0.05)
        raise RuntimeError("the headless browser did not open a debugger port within 20 seconds")

    def _page_target(self, port: int) -> str:
        last = ""
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
                    for target in json.load(r):
                        if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                            return str(target["webSocketDebuggerUrl"])
            except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
                last = str(exc)
            time.sleep(0.05)
        raise RuntimeError(f"the headless browser opened no page to render into ({last})".strip())

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != self._id:
                continue
            if "error" in message:
                detail = (message["error"] or {}).get("message") or "unknown DevTools error"
                raise RuntimeError(f"the browser refused {method}: {detail}")
            result = message.get("result", {})
            if result.get("exceptionDetails"):
                text = (result["exceptionDetails"].get("exception") or {}).get("description") \
                    or result["exceptionDetails"].get("text") or "unknown"
                raise RuntimeError(f"the page raised while rendering: {str(text).splitlines()[0]}")
            return result

    def open(self, page: Path) -> None:
        self.call("Page.navigate", url=page.as_uri())
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                ready = self.call("Runtime.evaluate", expression="!!window.__ready", returnByValue=True)
            except RuntimeError:
                ready = {}
            if (ready.get("result") or {}).get("value") is True:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("the page never finished loading, so no frame could be rendered")
        self.call("Runtime.evaluate", expression="window.__ready", awaitPromise=True, returnByValue=True)

    def frame(self, t: float, out: Path) -> None:
        self.call("Runtime.evaluate", expression=f"window.seek({t:.6f})", awaitPromise=True,
                  returnByValue=True)
        shot = self.call("Page.captureScreenshot", format="jpeg", quality=95,
                         captureBeyondViewport=False)
        data = shot.get("data")
        if not data:
            raise RuntimeError(f"the browser wrote no frame at {t:.2f}s")
        out.write_bytes(base64.b64decode(data))

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001 — closing must never mask the real error
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


def _render_frames(browser: Browser, brief: Brief, page: Path, frames_dir: Path) -> int:
    """One JPEG per frame of the timeline, at a fixed timestep the renderer owns."""
    width, height = brief.size
    frames_dir.mkdir(parents=True, exist_ok=True)
    total = brief.frames
    chrome = _Chromium(browser.chromium or "chromium", width, height)
    try:
        chrome.open(page)
        for i in range(total):
            chrome.frame(i / FPS, frames_dir / f"f{i + 1:06d}.jpg")
    finally:
        chrome.close()
    written = len(list(frames_dir.glob("f*.jpg")))
    if written != total:
        raise RuntimeError(f"the headless browser wrote no frame for {total - written} of {total} frames")
    return total


# ── assembly ───────────────────────────────────────────────────────────────────────────────────
def _assemble(ffmpeg: str, frames_dir: Path, out: Path, codec: str) -> None:
    cmd: list[str] = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                      "-framerate", str(FPS), "-start_number", "1", "-i", str(frames_dir / "f%06d.jpg"),
                      "-vf", "format=yuv420p", "-r", str(FPS), "-threads", "4"]
    # A frame is a dark surface with a soft wash across it, which is exactly what banding shows up
    # on, and the media plates carry real UI detail. crf 20/31 keeps a 30-second landscape asset
    # around 3 MB, which still attaches to an email.
    if codec == "mp4":
        cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p",
                "-profile:v", "high", "-movflags", "+faststart"]
    else:
        cmd += ["-c:v", "libvpx-vp9", "-crf", "31", "-b:v", "0", "-deadline", "good",
                "-cpu-used", "4", "-row-mt", "1", "-pix_fmt", "yuv420p"]
    cmd += [*_BITEXACT, str(out)]
    proc = _run(cmd, timeout=1800)
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)  # never leave a silent empty file behind
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(f"ffmpeg could not write {out.name}" + (f": {tail[-1]}" if tail else ""))


def _poster(ffmpeg: str, frame: Path, out: Path) -> None:
    proc = _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(frame),
                 "-frames:v", "1", "-q:v", "3", *_BITEXACT, str(out)], timeout=120)
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg could not write the poster frame")


def probe(path: Path | None) -> dict[str, Any]:
    """ffprobe's own reading of a rendered file: {width, height, codec, seconds, bytes}. `{}` when
    ffprobe or the file is absent — a missing probe is never reported as a measurement."""
    ffprobe = ffprobe_path()
    if not ffprobe or not path or not Path(path).is_file():
        return {}
    proc = _run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height,codec_name:format=duration", "-of", "json", str(path)], timeout=60)
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout)
        stream = (data.get("streams") or [{}])[0]
        return {"width": stream.get("width"), "height": stream.get("height"),
                "codec": stream.get("codec_name"),
                "seconds": round(float((data.get("format") or {}).get("duration") or 0.0), 3),
                "bytes": Path(path).stat().st_size}
    except (json.JSONDecodeError, ValueError, IndexError):
        return {}


# ── the public entry point ─────────────────────────────────────────────────────────────────────
@dataclass
class RenderResult:
    ok: bool
    error: str | None = None
    mp4: Path | None = None
    webm: Path | None = None
    poster: Path | None = None
    frames: list[Path] = field(default_factory=list)
    scenes: int = 0
    seconds: float = 0.0
    width: int = 0
    height: int = 0
    renderer: str = ""
    frame_count: int = 0

    def sentence(self) -> str:
        if not self.ok:
            return self.error or "the video could not be rendered"
        size = (self.mp4.stat().st_size / 1024 if self.mp4 and self.mp4.is_file() else 0.0)
        return (f"{self.width}x{self.height}, {self.seconds:g}s, {self.scenes} scene(s), "
                f"{self.frame_count} frames at {FPS}fps, {size:.0f} KB MP4")

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "error": self.error, "seconds": self.seconds, "width": self.width,
                "height": self.height, "scenes": self.scenes, "renderer": self.renderer,
                "frames": self.frame_count,
                "mp4": str(self.mp4) if self.mp4 else None,
                "webm": str(self.webm) if self.webm else None,
                "poster": str(self.poster) if self.poster else None}


def _prepare_media(ffmpeg: str, brief: Brief, workspace: Path | None,
                   work: Path) -> list[dict[str, Any]]:
    """Resolve, measure and extract every scene's media, and lay the timeline out around it."""
    needs_media = [i for i, s in enumerate(brief.scenes) if s.media]
    if needs_media and workspace is None:
        raise RuntimeError("this brief uses media from the workspace, but no workspace was given "
                           "to resolve it against")
    sources: dict[int, Path] = {}
    sizes: dict[int, tuple[int, int]] = {}
    for i in needs_media:
        media = brief.scenes[i].media
        assert media is not None
        root = Path(workspace).resolve()  # type: ignore[arg-type]
        path = (root / media.source).resolve()
        if not str(path).startswith(str(root) + os.sep) or not path.is_file():
            raise RuntimeError(f"scene {i + 1} asks for media that is not in the workspace: "
                               f"{media.source}")
        sources[i] = path
        size = media_size(path)
        if size is None:
            raise RuntimeError(f"scene {i + 1}: {media.source} could not be read as an image or clip")
        sizes[i] = size

    plans = _plans(brief, sizes)
    for i in needs_media:
        media = brief.scenes[i].media
        assert media is not None
        out = work / "media" / f"{i:03d}"
        plans[i]["frames"] = _extract(ffmpeg, media, sources[i], plans[i], out)
        plans[i]["dir"] = out.as_uri()
    return plans


def render_video(brief: Brief | dict[str, Any], out_dir: Path | str, *, stem: str = "video",
                 formats: tuple[str, ...] = ("mp4", "webm"), keep_frames: bool = False,
                 workspace: Path | None = None) -> RenderResult:
    """Render `brief` into `out_dir` as `<stem>.mp4`, `<stem>.webm` and `<stem>-poster.jpg`.

    Never raises for an environment problem: a missing ffmpeg or browser comes back as
    `ok=False` with one sentence naming what to install, and no partial file is left behind.
    """
    try:
        if not isinstance(brief, Brief):
            brief = parse_brief(brief, media_root=workspace)
    except BriefError as exc:
        return RenderResult(ok=False, error=str(exc))

    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return RenderResult(ok=False, error=_MISSING_FFMPEG)
    browser = find_browser(workspace)
    if browser is None:
        return RenderResult(ok=False, error=_MISSING_BROWSER)

    out_dir = Path(out_dir)
    width, height = brief.size
    wanted = [f for f in ("mp4", "webm") if f in formats]
    written: dict[str, Path] = {}
    poster: Path | None = None
    kept: list[Path] = []
    work = Path(tempfile.mkdtemp(prefix="revenueos-creative-"))
    try:
        plans = _prepare_media(ffmpeg, brief, workspace, work)
        page = work / "film.html"
        page.write_text(timeline_html(brief, plans), encoding="utf-8")
        frames_dir = work / "frames"
        total = _render_frames(browser, brief, page, frames_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for codec in wanted:
            target = out_dir / f"{stem}.{codec}"
            _assemble(ffmpeg, frames_dir, target, codec)
            written[codec] = target
        poster = out_dir / f"{stem}-poster.jpg"
        # the last frame of scene 1: the opening composition with every entrance settled
        settled = max(1, min(total, round(brief.scenes[0].seconds * FPS)))
        _poster(ffmpeg, frames_dir / f"f{settled:06d}.jpg", poster)
        if keep_frames:
            kept_dir = out_dir / f"{stem}-frames"
            shutil.rmtree(kept_dir, ignore_errors=True)
            shutil.copytree(frames_dir, kept_dir)
            kept = sorted(kept_dir.glob("f*.jpg"))
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        for path in written.values():
            path.unlink(missing_ok=True)
        if poster:
            poster.unlink(missing_ok=True)
        message = str(exc) or f"{type(exc).__name__}"
        if isinstance(exc, subprocess.TimeoutExpired):
            message = "rendering timed out; the brief may be too long for this machine"
        return RenderResult(ok=False, error=message, renderer=browser.describe())
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return RenderResult(ok=True, mp4=written.get("mp4"), webm=written.get("webm"), poster=poster,
                        frames=kept, scenes=len(brief.scenes), seconds=brief.seconds,
                        width=width, height=height, renderer=browser.describe(),
                        frame_count=brief.frames)


# ── writing the brief (the only part a model does) ─────────────────────────────────────────────
# Channels where a short silent motion asset is the native format. A channel that is not here gets
# no video proposed: RevenueOS does not invent a use for an asset.
VIDEO_CHANNELS: dict[str, str] = {
    "youtube": "landscape",
    "instagram": "portrait",
    "tiktok": "portrait",
    "facebook": "portrait",
    "linkedin": "landscape",
    "x": "landscape",
    "twitter": "landscape",
    "social": "portrait",
}

# Where a workspace keeps footage and stills a film may use. Nothing outside these is offered to
# the model, and `parse_brief(media_root=...)` refuses anything the model invents anyway.
MEDIA_DIRS = ("data/media", "website/media", "website/screenshots")
MAX_MEDIA_OFFERED = 24


def available_media(workspace: Path | str | None) -> list[str]:
    """Every clip and still in the workspace a brief is allowed to cut to, as workspace-relative
    paths. Empty when there is no workspace or nothing in it: then the film is typography only."""
    if workspace is None:
        return []
    root = Path(workspace)
    found: list[str] = []
    for folder in MEDIA_DIRS:
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir()):
            if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES:
                found.append(str(path.relative_to(root)))
    return found[:MAX_MEDIA_OFFERED]


BRIEF_SYSTEM = f"""You are the creative director writing the shot list for a short, silent \
advertisement for the business described below. It is rendered deterministically as real motion \
graphics at {FPS}fps: type animates in word by word, numbers count up, media pushes in, and scenes \
hard-cut. There is no voice-over and no music, so the writing and the cutting carry the film.

Rules:
* Use ONLY facts present in the business context. Never invent a metric, a customer, a result, a \
price, an award or a claim. If you have no number, write a sentence instead of a number.
* One idea per scene. The heading is the idea; the body is the evidence or the consequence.
* Plain, specific, declarative language. No slogans, no exclamation marks, no emoji.
* Rhythm matters. Vary the dwell: a short scene lands a line, a long scene lets footage breathe. \
Never give every scene the same duration.
* `media` may ONLY name a file from the AVAILABLE MEDIA list below, verbatim. Cut to the product \
actually working wherever the list allows it — that is the strongest thing a software advertisement \
has. Never describe footage that is not on the list.
* `count` is for one honest number from the context, counted up on screen.
* `mono` is for a literal, checkable string only (a metric name, a before → after, a command).
* Exactly ONE scene in the whole film may set `"accent": true`. Spend it on the line you want \
remembered. `"transition": "dissolve"` is likewise an exception — cut everywhere else.
* The last scene is the end card: `"align": "center"`, `"rule": true`, the business name as the \
heading and one closing line in the body. No call to action asking for money, and no URL.

Return ONE JSON object and nothing else — no prose, no markdown fence:
{{"title": "<= {MAX_TITLE} chars, names the asset",
  "eyebrow": "<= {MAX_EYEBROW} chars, a quiet label on the opening scene",
  "aspect": "landscape" | "portrait",
  "scenes": [{{"heading": "<= {MAX_HEADING} chars",
              "body": ["<= {MAX_BODY_CHARS} chars", "at most {MAX_BODY_LINES} lines"],
              "mono": "<= {MAX_MONO} chars, optional",
              "count": {{"to": <number>, "label": "<= {MAX_COUNT_LABEL} chars"}},
              "media": {{"source": "<exact path from AVAILABLE MEDIA>", "start": <seconds into a clip>}},
              "caption": "<= {MAX_CAPTION} chars, sits under the media, optional",
              "transition": "cut" | "dissolve", "align": "left" | "center",
              "rule": false, "accent": false, "exit": "hold" | "lift",
              "seconds": {MIN_SECONDS} to {MAX_SECONDS}}}]}}

Between {MIN_SCENES} and {MAX_SCENES} scenes, {MIN_TOTAL_SECONDS:g} to {MAX_TOTAL_SECONDS:g} seconds \
in total. A brief that breaks any limit is rejected outright and nothing is rendered, so count the \
characters."""


def video_channel(channels: list[str]) -> tuple[str, str] | None:
    """The first of the business's channels where a short motion asset is the native format, with the
    aspect that channel wants. None when no configured channel calls for video."""
    for channel in channels or []:
        key = channel.lower().strip()
        if key in VIDEO_CHANNELS:
            return channel, VIDEO_CHANNELS[key]
    return None


def _first_json_object(text: str) -> str:
    """The first balanced {...} in a model's answer, so a stray fence or preamble is not fatal."""
    start = text.find("{")
    if start == -1:
        return ""
    depth, in_string, escaped = 0, False, False
    for i, ch in enumerate(text[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def write_brief(llm: Any, *, company: str, channel: str, aspect: str, context_summary: str,
                ask: str = "", workspace: Path | str | None = None) -> tuple[Brief | None, str]:
    """Ask the model for a brief and validate it. Returns `(brief, note)`; `brief` is None whenever
    there is no model or the answer does not satisfy the contract, and `note` says why in one
    sentence. A malformed brief is refused here, never rendered.
    """
    if llm is None:
        return None, "no LLM credential, so no video brief was written and nothing was proposed"
    from .llm import Message

    media = available_media(workspace)
    catalogue = ("\n\n=== AVAILABLE MEDIA (use these paths verbatim, or none) ===\n"
                 + "\n".join(media)) if media else \
                "\n\n=== AVAILABLE MEDIA ===\n(none — this film is typography only)"
    user = (f"=== BUSINESS CONTEXT ===\n{context_summary}{catalogue}\n\n=== TASK ===\n"
            f"Write the shot list for a short {aspect} advertisement for {company}, to be posted on "
            f"{channel}. "
            + (ask or "Say what the business does, who it is for, and why that is worth someone's "
                      "attention."))
    try:
        answer = llm.complete_sync([Message("system", BRIEF_SYSTEM), Message("user", user)],
                                   max_tokens=3000)
    except Exception as exc:  # an unavailable or refusing model must not break the worker
        return None, f"the model could not write a video brief ({type(exc).__name__}: {exc})"
    payload = _first_json_object(answer or "")
    if not payload:
        return None, "the model did not return a JSON video brief, so nothing was proposed"
    try:
        return parse_brief(payload, media_root=workspace), "brief written and validated"
    except BriefError as exc:
        return None, f"the video brief was refused: {exc}"


__all__ = ["Brief", "BriefError", "Count", "Media", "Palette", "RenderResult", "Scene",
           "available_media", "find_browser", "ffmpeg_path", "ffprobe_path", "media_size",
           "parse_brief", "probe", "render_video", "timeline_html", "video_channel", "write_brief",
           "DEFAULT_PALETTE", "MEDIA_DIRS", "VIDEO_CHANNELS", "FPS", "replace"]
