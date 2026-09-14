"""The creative department: a shot list in, a real advertisement out, honest refusals when it cannot.

The tests that render are skipped with a plain reason when this machine has no ffmpeg or no headless
browser, so the suite stays green without them. The refusal, validation, executor-gate and no-LLM
tests need neither and always run.

The claims worth testing are the ones a customer would be angry about if they were false: that the
file is real motion and not a slide show (900 frames for a 30-second brief, and frames that differ
from each other), that their own footage actually lands in the picture, that the same brief renders
the same bytes twice, and that nothing is rendered at all until a human approved it.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from revenueos.creative import (
    FPS,
    BriefError,
    available_media,
    ffmpeg_path,
    ffprobe_path,
    find_browser,
    parse_brief,
    probe,
    render_video,
    timeline_html,
    video_channel,
    write_brief,
)

TWO_SCENES = {
    "title": "Acme Recall",
    "eyebrow": "Acme Scheduling",
    "aspect": "landscape",
    "scenes": [
        {"heading": "Empty chairs cost money.", "body": ["No-shows eat 15% of chair time."],
         "seconds": 2.0, "exit": "lift"},
        {"heading": "Two-way SMS rebooking.", "body": ["Installs in an afternoon."],
         "mono": "no_shows -> rebooked", "seconds": 3.0},
    ],
}


def _renderable() -> str | None:
    """None when this machine can render; otherwise the reason it cannot."""
    if not ffmpeg_path():
        return "ffmpeg is not installed on this machine"
    if find_browser() is None:
        return "no headless browser is installed on this machine"
    return None


needs_renderer = pytest.mark.skipif(_renderable() is not None, reason=_renderable() or "")


def _plate(path: Path, colour: str = "red", size: str = "1200x600") -> Path:
    """A solid, unmistakable still standing in for a business's own footage."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ffmpeg_path(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c={colour}:s={size}", "-frames:v", "1", str(path)], check=True)
    return path


def _average_rgb(ffmpeg: str, frame: Path) -> tuple[int, int, int]:
    """The mean colour of one frame, straight out of ffmpeg — the cheapest honest way to ask
    whether something actually got drawn."""
    out = subprocess.run([ffmpeg, "-v", "error", "-i", str(frame), "-vf", "scale=1:1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    return out[0], out[1], out[2]


# ── the brief contract ─────────────────────────────────────────────────────────────────────────
def test_a_valid_brief_parses_into_scenes():
    brief = parse_brief(TWO_SCENES)
    assert [s.heading for s in brief.scenes] == ["Empty chairs cost money.", "Two-way SMS rebooking."]
    assert brief.seconds == 5.0
    assert brief.frames == 150  # 5 seconds of real frames, not 2 stills
    assert brief.size == (1920, 1080)
    assert parse_brief(json.dumps(TWO_SCENES)).as_dict() == brief.as_dict()  # JSON text is accepted


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda b: b.update(scenes=[b["scenes"][0]]), "1 scene"),
        (lambda b: b.update(scenes=b["scenes"] * 7), "14 scene"),
        (lambda b: b.update(title=""), "no title"),
        (lambda b: b["scenes"][0].update(heading="x" * 90), "90 characters"),
        (lambda b: b["scenes"][0].update(body=["y" * 200]), "200 characters"),
        (lambda b: b["scenes"][0].update(body=["a", "b", "c", "d"]), "4 body lines"),
        (lambda b: b["scenes"][0].update(seconds=45), "45s"),
        (lambda b: b["scenes"][0].update(seconds=0.2), "0.2s"),
        (lambda b: b["scenes"][0].update(seconds="soon"), "not a number"),
        (lambda b: b["scenes"][0].update(mono="m" * 80), "80 characters"),
        (lambda b: b["scenes"][0].update(caption="a caption"), "no media"),
        (lambda b: b["scenes"][0].update(overlay=True), "no media"),
        (lambda b: b["scenes"][0].update(heading="", body=[]), "nothing in it"),
        (lambda b: b["scenes"][0].update(transition="dissolve"), "scene 1 cannot dissolve"),
        (lambda b: b["scenes"][1].update(transition="swirl"), "unknown transition"),
        (lambda b: b["scenes"][1].update(align="diagonal"), "unknown alignment"),
        (lambda b: b["scenes"][1].update(exit="explode"), "unknown exit"),
        (lambda b: [s.update(accent=True) for s in b["scenes"]], "claim the accent colour"),
        (lambda b: b["scenes"][0].update(count={"label": "leads"}), "no numeric 'to'"),
        (lambda b: b["scenes"][0].update(count={"to": "many"}), "no numeric 'to'"),
        (lambda b: [s.update(seconds=1.0) for s in b["scenes"]], "at least 3 seconds"),
        (lambda b: b.update(aspect="square"), "unknown aspect"),
        (lambda b: b.pop("scenes"), "no 'scenes' list"),
    ],
)
def test_a_malformed_brief_is_refused_with_a_sentence(mutate, expected):
    data = json.loads(json.dumps(TWO_SCENES))
    mutate(data)
    with pytest.raises(BriefError) as exc:
        parse_brief(data)
    message = str(exc.value)
    assert expected in message
    assert message == message.strip() and not message.startswith("Traceback")


@pytest.mark.parametrize(
    ("media", "expected"),
    [
        ({"source": "/etc/passwd"}, "inside the workspace"),
        ({"source": "../secrets/clip.mp4"}, "inside the workspace"),
        ({"source": "media/clip.exe"}, "use one of"),
        ({"source": "media/clip.mp4", "start": -3}, "negative"),
        ({"source": "media/clip.mp4", "crop": [1, 2, 3]}, "[x, y, width, height]"),
        ({"source": "media/clip.mp4", "crop": [0, 0, 4, 900]}, "smaller than 16px"),
        ({}, "no 'source'"),
    ],
)
def test_media_a_model_invents_is_refused(media, expected):
    data = json.loads(json.dumps(TWO_SCENES))
    data["scenes"][0]["media"] = media
    with pytest.raises(BriefError) as exc:
        parse_brief(data)
    assert expected in str(exc.value)


def test_media_that_is_not_in_the_workspace_is_refused_before_anything_renders(tmp_path: Path):
    data = json.loads(json.dumps(TWO_SCENES))
    data["scenes"][0]["media"] = {"source": "website/media/never-recorded.mp4"}
    with pytest.raises(BriefError) as exc:
        parse_brief(data, media_root=tmp_path)
    assert "not in the workspace" in str(exc.value)
    assert "never-recorded.mp4" in str(exc.value)


def test_a_refused_brief_never_reaches_the_renderer(tmp_path: Path):
    result = render_video({"title": "x", "scenes": []}, tmp_path)
    assert result.ok is False
    assert "scene" in result.error
    assert list(tmp_path.iterdir()) == []  # no empty file left behind


def test_the_timeline_is_a_pure_function_of_the_brief():
    brief = parse_brief(TWO_SCENES)
    assert timeline_html(brief) == timeline_html(brief)
    html = timeline_html(brief)
    assert "Empty chairs cost money." not in html  # the heading is split into animated words
    assert 'data-anim="rise"' in html and "window.seek" in html
    assert "#0071e3" in html  # the design.css accent
    assert html.count("<section") == 2


def test_the_page_owns_no_clock_of_its_own():
    """Determinism is a property of the page, not of the machine: a renderer that drives the clock
    cannot be reproducible if the page also reads one."""
    html = timeline_html(parse_brief(TWO_SCENES))
    for forbidden in ("Date.now", "new Date", "setTimeout", "setInterval", "Math.random",
                      "performance.now"):
        assert forbidden not in html, forbidden


def test_markup_in_a_brief_is_escaped_not_rendered():
    brief = parse_brief({**TWO_SCENES, "scenes": [
        {"heading": "<script>alert(1)</script>", "seconds": 2.0},
        {"heading": "Fine", "seconds": 2.0}]})
    html = timeline_html(brief)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_available_media_only_offers_what_is_really_there(tmp_path: Path):
    assert available_media(None) == []
    assert available_media(tmp_path) == []
    (tmp_path / "website" / "media").mkdir(parents=True)
    (tmp_path / "website" / "media" / "clip.mp4").write_bytes(b"\x00")
    (tmp_path / "website" / "media" / "notes.txt").write_text("not media")
    assert available_media(tmp_path) == ["website/media/clip.mp4"]


# ── the environment, reported as a sentence ────────────────────────────────────────────────────
def test_a_missing_ffmpeg_is_a_sentence_not_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REVENUEOS_FFMPEG", str(tmp_path / "nowhere" / "ffmpeg"))
    result = render_video(TWO_SCENES, tmp_path / "out")
    assert result.ok is False
    assert "ffmpeg was not found" in result.error and "install" in result.error
    assert "Traceback" not in result.error
    assert not (tmp_path / "out").exists() or list((tmp_path / "out").iterdir()) == []


def test_a_missing_browser_is_a_sentence_not_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from revenueos import creative

    monkeypatch.setattr(creative, "find_browser", lambda workspace=None: None)
    result = creative.render_video(TWO_SCENES, tmp_path / "out")
    assert result.ok is False
    assert "headless browser" in result.error and "playwright install" in result.error


def test_a_browser_that_never_starts_is_reported_and_leaves_no_empty_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import shutil

    from revenueos import creative

    if not creative.ffmpeg_path():
        pytest.skip("ffmpeg is not installed on this machine")
    quiet = shutil.which("true")
    if not quiet:
        pytest.skip("no `true` binary to stand in for a browser that does nothing")
    monkeypatch.setattr(creative, "find_browser",
                        lambda workspace=None: creative.Browser(kind="chromium", chromium=quiet))
    out = tmp_path / "out"
    result = creative.render_video(TWO_SCENES, out)
    assert result.ok is False
    assert "headless browser" in result.error
    assert not out.exists() or list(out.iterdir()) == []


def test_a_brief_with_media_but_no_workspace_says_so(tmp_path: Path):
    if _renderable():
        pytest.skip(_renderable() or "")
    data = json.loads(json.dumps(TWO_SCENES))
    data["scenes"][0]["media"] = {"source": "media/clip.png"}
    result = render_video(data, tmp_path / "out")
    assert result.ok is False
    assert "no workspace was given" in result.error


# ── rendering, end to end ──────────────────────────────────────────────────────────────────────
@needs_renderer
def test_a_brief_renders_a_real_mp4_of_real_frames(tmp_path: Path):
    result = render_video(TWO_SCENES, tmp_path, stem="acme")
    assert result.ok, result.error
    assert result.frame_count == 150, "a 5-second film is 150 frames, not 2 stills"

    mp4 = tmp_path / "acme.mp4"
    assert mp4.is_file() and mp4.stat().st_size > 1024
    assert (tmp_path / "acme.webm").stat().st_size > 1024
    assert (tmp_path / "acme-poster.jpg").stat().st_size > 1024
    assert mp4.stat().st_size < 16 * 1024 * 1024

    if not ffprobe_path():
        pytest.skip("ffprobe is not installed, so the file cannot be verified independently")
    read = probe(mp4)
    assert read["width"] == 1920 and read["height"] == 1080
    assert read["codec"] == "h264"
    assert abs(read["seconds"] - 5.0) < 0.05, read  # the brief's 2.0 + 3.0 seconds


@needs_renderer
def test_the_picture_actually_moves(tmp_path: Path):
    """The failure this catches is the one the old renderer had: six stills cross-dissolved. Inside
    a single scene's entrance, consecutive frames must differ from one another."""
    result = render_video(TWO_SCENES, tmp_path, stem="motion", formats=("mp4",), keep_frames=True)
    assert result.ok, result.error
    frames = sorted((tmp_path / "motion-frames").glob("f*.jpg"))
    assert len(frames) == 150
    early = [f.read_bytes() for f in frames[2:20]]           # mid-entrance of scene 1
    assert len(set(early)) == len(early), "every frame of an entrance must be a different picture"
    # scene 2 runs 2.0-5.0s and never lifts out, so by 4.0s every entrance has come to rest
    settled = [f.read_bytes() for f in frames[120:126]]
    assert len(set(settled)) == 1, "a scene that has settled should hold still"


@needs_renderer
def test_the_business_own_footage_lands_in_the_picture(tmp_path: Path):
    """A software advertisement's strongest asset is the product working. This proves a plate is
    really composited into the film and not quietly dropped."""
    workspace = tmp_path / "ws"
    _plate(workspace / "website" / "media" / "plate.png")
    brief = {
        "title": "Acme with footage", "aspect": "landscape",
        "scenes": [
            {"heading": "Before.", "seconds": 1.5},
            {"heading": "The product.", "seconds": 2.5, "align": "center",
             "media": {"source": "website/media/plate.png"}, "caption": "a real screen"},
        ],
    }
    out = tmp_path / "out"
    result = render_video(brief, out, stem="footage", formats=("mp4",), workspace=workspace,
                          keep_frames=True)
    assert result.ok, result.error
    frames = sorted((out / "footage-frames").glob("f*.jpg"))
    ffmpeg = ffmpeg_path()
    assert ffmpeg
    before = _average_rgb(ffmpeg, frames[30])   # 1.0s: type on the ground, no plate yet
    during = _average_rgb(ffmpeg, frames[100])  # 3.3s: the plate is on screen
    assert before[0] < 40, f"the opening scene should be dark, got {before}"
    assert during[0] > 60, f"the plate should dominate the frame, got {during}"
    assert during[0] > during[2] * 3, f"the plate is red; the frame should be too, got {during}"


@needs_renderer
def test_portrait_is_a_different_shape_of_the_same_brief(tmp_path: Path):
    result = render_video({**TWO_SCENES, "aspect": "portrait"}, tmp_path, stem="acme-portrait",
                          formats=("mp4",))
    assert result.ok, result.error
    assert (result.width, result.height) == (1080, 1350)
    if ffprobe_path():
        read = probe(result.mp4)
        assert (read["width"], read["height"]) == (1080, 1350)


@needs_renderer
def test_the_same_brief_renders_identical_bytes_twice(tmp_path: Path):
    workspace = tmp_path / "ws"
    _plate(workspace / "website" / "media" / "plate.png", colour="blue")
    brief = {"title": "Repeatable", "aspect": "landscape", "scenes": [
        {"heading": "One.", "seconds": 1.6, "exit": "lift"},
        {"heading": "Two.", "seconds": 2.0, "align": "center", "transition": "dissolve",
         "media": {"source": "website/media/plate.png", "crop": [0, 0, 800, 400]}},
    ]}
    first = render_video(brief, tmp_path / "one", stem="v", workspace=workspace)
    second = render_video(brief, tmp_path / "two", stem="v", workspace=workspace)
    assert first.ok and second.ok, first.error or second.error
    for name in ("v.mp4", "v.webm", "v-poster.jpg"):
        assert (tmp_path / "one" / name).read_bytes() == (tmp_path / "two" / name).read_bytes(), name


@needs_renderer
def test_the_rendered_file_plays_as_video_start_to_finish(tmp_path: Path):
    """ffmpeg decoding every frame is the only proof that the container is not a plausible-looking
    file. A silent empty file would fail here."""
    result = render_video(TWO_SCENES, tmp_path, stem="acme", formats=("mp4",))
    assert result.ok, result.error
    decode = subprocess.run([ffmpeg_path(), "-v", "error", "-i", str(result.mp4), "-f", "null", "-"],
                            capture_output=True, text=True, check=False)
    assert decode.returncode == 0 and decode.stderr.strip() == "", decode.stderr


@needs_renderer
def test_the_asset_is_silent_and_says_so(tmp_path: Path):
    """No voice-over and no music is a decision, not an oversight: the installed system voices are
    not good enough to put in front of a customer, so the file carries no audio stream at all."""
    result = render_video(TWO_SCENES, tmp_path, stem="silent", formats=("mp4",))
    assert result.ok, result.error
    ffprobe = ffprobe_path()
    if not ffprobe:
        pytest.skip("ffprobe is not installed")
    streams = subprocess.run([ffprobe, "-v", "error", "-show_entries", "stream=codec_type",
                              "-of", "csv=p=0", str(result.mp4)],
                             capture_output=True, text=True, check=True).stdout
    assert "audio" not in streams, streams
    assert "video" in streams, streams


# ── the loop: proposal, approval, execution, measurement ───────────────────────────────────────
def test_without_an_llm_the_content_worker_proposes_no_video(workspace, store, onboarded):
    """The brief is the model's work. With `llm=None` there is no brief, so there is nothing to
    propose — the worker must not invent one."""
    from revenueos.workers.content import ContentWorker

    onboarded.config["channels"] = ["youtube", "seo"]
    result = ContentWorker().run(workspace, store, onboarded, None, store.start_run("content", "test"))
    assert result.ok
    assert [a for a in store.list_actions("pending") if a["context"].get("executor") == "render_video"] == []
    assert "no LLM credential" in result.details["video"]


def test_a_channel_without_video_proposes_nothing_even_with_a_model(workspace, store, onboarded):
    from revenueos.workers.content import ContentWorker

    onboarded.config["channels"] = ["seo", "cold email"]

    class Loud:
        def complete_sync(self, *a, **k):
            raise AssertionError("the model must not be asked for a brief for a channel with no video")

    result = ContentWorker().run(workspace, store, onboarded, Loud(), store.start_run("content", "test"))
    assert "no configured channel" in result.details["video"]


def test_a_model_that_returns_nonsense_is_refused_not_rendered(onboarded):
    class Nonsense:
        def complete_sync(self, *a, **k):
            return json.dumps({"title": "Fine", "scenes": [{"heading": "x" * 200, "seconds": 3}]})

    brief, note = write_brief(Nonsense(), company="Acme", channel="youtube", aspect="landscape",
                              context_summary="Acme sells scheduling software.")
    assert brief is None
    assert note.startswith("the video brief was refused:")


def test_a_model_that_invents_footage_is_refused(tmp_path: Path):
    """The model is shown the workspace's real media and nothing else; a path it made up is caught
    here, before an action is ever created."""
    invented = json.loads(json.dumps(TWO_SCENES))
    invented["scenes"][0]["media"] = {"source": "website/media/drone-shot.mp4"}

    class Inventive:
        def complete_sync(self, *a, **k):
            return json.dumps(invented)

    brief, note = write_brief(Inventive(), company="Acme", channel="youtube", aspect="landscape",
                              context_summary="Acme sells scheduling software.", workspace=tmp_path)
    assert brief is None
    assert "not in the workspace" in note


def test_the_model_is_shown_the_footage_the_business_actually_has(tmp_path: Path):
    (tmp_path / "website" / "media").mkdir(parents=True)
    (tmp_path / "website" / "media" / "demo.mp4").write_bytes(b"\x00")
    seen: dict[str, str] = {}

    class Watcher:
        def complete_sync(self, messages, **k):
            seen["user"] = messages[-1].content
            return json.dumps(TWO_SCENES)

    brief, note = write_brief(Watcher(), company="Acme", channel="youtube", aspect="landscape",
                             context_summary="Acme sells scheduling software.", workspace=tmp_path)
    assert brief is not None, note
    assert "website/media/demo.mp4" in seen["user"]
    assert "AVAILABLE MEDIA" in seen["user"]


def test_a_model_answer_wrapped_in_prose_is_still_read(onboarded):
    class Chatty:
        def complete_sync(self, *a, **k):
            return "Sure — here is the brief:\n```json\n" + json.dumps(TWO_SCENES) + "\n```\nHope that helps."

    brief, note = write_brief(Chatty(), company="Acme", channel="youtube", aspect="landscape",
                             context_summary="Acme sells scheduling software.")
    assert brief is not None and brief.title == "Acme Recall"
    assert note == "brief written and validated"


def test_video_channel_picks_the_native_aspect():
    assert video_channel(["seo", "instagram"]) == ("instagram", "portrait")
    assert video_channel(["YouTube"]) == ("YouTube", "landscape")
    assert video_channel(["seo", "cold email"]) is None


def test_the_executor_refuses_an_action_that_was_not_approved(workspace, store, onboarded):
    from revenueos.workers import refused
    from revenueos.workers.executors import execute_render_video

    aid = store.create_action("content_opportunity", "YouTube: Acme Recall", "a video",
                              context={"executor": "render_video", "video_brief": TWO_SCENES})
    outcome = execute_render_video(workspace, store, onboarded, None, store.get_action(aid))
    assert refused(outcome), outcome
    assert "has not been approved" in outcome
    assert list(workspace.outputs.iterdir()) == []


def test_the_executor_refuses_an_approved_action_with_a_bad_brief(workspace, store, onboarded):
    from revenueos.workers import refused
    from revenueos.workers.executors import execute_render_video

    aid = store.create_action("content_opportunity", "YouTube: broken", "a video",
                              context={"executor": "render_video", "video_brief": {"title": "x", "scenes": []}})
    store.set_action_status(aid, "approved")
    outcome = execute_render_video(workspace, store, onboarded, None, store.get_action(aid))
    assert refused(outcome) and "scene" in outcome
    assert list(workspace.outputs.iterdir()) == []


def test_the_executor_refuses_footage_the_workspace_does_not_have(workspace, store, onboarded):
    from revenueos.workers import refused
    from revenueos.workers.executors import execute_render_video

    brief = json.loads(json.dumps(TWO_SCENES))
    brief["scenes"][0]["media"] = {"source": "website/media/stock-footage.mp4"}
    aid = store.create_action("content_opportunity", "YouTube: invented", "a video",
                              context={"executor": "render_video", "video_brief": brief})
    store.set_action_status(aid, "approved")
    outcome = execute_render_video(workspace, store, onboarded, None, store.get_action(aid))
    assert refused(outcome) and "not in the workspace" in outcome
    assert list(workspace.outputs.iterdir()) == []


def test_the_executor_refuses_an_approved_action_carrying_no_brief(workspace, store, onboarded):
    from revenueos.workers import refused
    from revenueos.workers.executors import execute_render_video

    aid = store.create_action("content_opportunity", "YouTube: nothing", "a video",
                              context={"executor": "render_video"})
    store.set_action_status(aid, "approved")
    outcome = execute_render_video(workspace, store, onboarded, None, store.get_action(aid))
    assert refused(outcome) and "no video brief" in outcome


@needs_renderer
def test_approved_then_executed_writes_the_asset_and_measures_it_as_produced(workspace, store, onboarded):
    """The whole path: an action with a brief → approve → execute_action dispatches `render_video`
    → the file is on disk and its real dimensions are on the action → measure records
    'produced (not published)' and invents no audience number."""
    from revenueos.workers import execute_action, refused
    from revenueos.workers.measure import MeasureWorker

    aid = store.create_action("content_opportunity", "YouTube: Acme Recall", "a video",
                              context={"executor": "render_video", "channel": "youtube",
                                       "video_brief": TWO_SCENES, "before": {"video_rendered": 0.0}})
    store.set_action_status(aid, "approved")
    outcome = execute_action(workspace, store, onboarded, None, store.get_action(aid))
    assert not refused(outcome), outcome
    assert f"{FPS}fps" in outcome
    store.set_action_status(aid, "executed")

    context = store.get_action(aid)["context"]
    asset = workspace.root / context["video"]
    assert asset.is_file() and asset.stat().st_size > 1024
    assert (context["video_width"], context["video_height"]) == (1920, 1080)
    assert abs(float(context["video_seconds"]) - 5.0) < 0.05
    assert context["rendered_at"]

    MeasureWorker().run(workspace, store, onboarded, None, store.start_run("measure", "test"))
    outcomes = store.latest_outcome(aid)
    assert outcomes["status"] == "produced"
    assert outcomes["metric"] == "deliverable_produced"
    assert "not published" in outcomes["note"]
    for forbidden in ("view", "impression", "reach", "engagement"):
        assert forbidden not in json.dumps(outcomes).lower(), f"no {forbidden} number may be invented"
