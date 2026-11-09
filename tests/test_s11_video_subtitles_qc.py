"""S11's two missing stages: the caption track and the post-render QC.

Before this file the video pipeline had no way to say anything about the file it
had just produced. ``render_short_video`` returned an artifact built from the
*intent* of the render — the width, height and duration it asked FFmpeg for —
and nothing ever opened the MP4 to check that those numbers were true. Two
defects lived in that gap, and both fail silently in production:

* **Captions that do not exist.** A short with no caption track is a short that
  is unwatchable on mute, and the storyboard already holds the text. The cue
  grid has to come from the same ``scene_seconds`` the encoder used, or cue *i*
  ends somewhere other than where cue *i+1* starts and the captions drift off
  the picture while still looking authoritative.
* **"Verified" that was never verified.** A QC pass that answers ``ok: True``
  because ffprobe was missing is worse than no QC at all: the operator reads a
  green light and ships a truncated file. ``qc_video`` therefore refuses to
  return ``ok: True`` when it could not probe, and the reason string says
  ``ffprobe unavailable`` so the caller can tell "bad file" from "no tool".

Hermetic: no FFmpeg, no ffprobe and no network. ``probe_video`` is replaced with
a fake for the limit tests, and ``shutil.which`` is forced to ``None`` for the
missing-tool test, so the suite never depends on what is installed on the box.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tgfarm.s11_video.renderer import (
    ShortScene,
    build_srt,
    probe_video,
    qc_video,
    write_srt,
)

# `HH:MM:SS,mmm --> HH:MM:SS,mmm` — the comma is the part players are strict
# about; a dot there is the classic "subtitle file loads but shows nothing".
_TIMECODE_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})$")


def _scenes(*texts: str) -> list[ShortScene]:
    return [ShortScene("fact", f"ФАКТ {index:02d}", text) for index, text in enumerate(texts, 1)]


def _parse_srt(text: str) -> list[tuple[int, str, str, str]]:
    """Split an SRT into (number, start, end, body) so a test can assert on parts."""
    cues: list[tuple[int, str, str, str]] = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        number = int(lines[0])
        start, _, end = lines[1].partition(" --> ")
        cues.append((number, start, end, "\n".join(lines[2:])))
    return cues


def _seconds(timecode: str) -> float:
    hours, minutes, rest = timecode.split(":")
    whole, millis = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(whole) + int(millis) / 1000


def test_build_srt_emits_a_well_formed_cue_per_scene():
    scenes = _scenes("Первый факт.", "Второй факт.", "Третий факт.")

    text = build_srt(scenes, scene_seconds=3.5)

    cues = _parse_srt(text)
    assert [cue[0] for cue in cues] == [1, 2, 3], "cue numbers must be 1-based and contiguous"
    assert [cue[3] for cue in cues] == ["Первый факт.", "Второй факт.", "Третий факт."]
    for _number, start, end, _body in cues:
        assert _TIMECODE_RE.match(f"{start} --> {end}"), f"not an SRT timecode: {start} --> {end}"


def test_build_srt_timecodes_are_contiguous_and_start_at_zero():
    """Cue *i* must end exactly where cue *i+1* starts, or the captions drift."""
    scenes = _scenes("Раз", "Два", "Три", "Четыре")

    cues = _parse_srt(build_srt(scenes, scene_seconds=2.25))

    assert _seconds(cues[0][1]) == 0.0
    for previous, following in zip(cues, cues[1:], strict=False):
        assert previous[2] == following[1], (
            f"gap between cue {previous[0]} and {following[0]}: {previous[2]} != {following[1]}"
        )
    assert _seconds(cues[-1][2]) == pytest.approx(4 * 2.25)


def test_build_srt_strips_markup_urls_and_control_characters():
    """Caption text is untrusted post content: a URL or a NUL byte is a leak."""
    scenes = _scenes(
        "Важное <b>обновление</b> рынка",
        "Подробнее https://private.example/story?token=secret",
        "Строка\x00с\x07управляющими\x1bсимволами",
    )

    text = build_srt(scenes, scene_seconds=3.0)

    assert "<b>" not in text
    assert "https://" not in text
    assert "secret" not in text
    assert not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", text), (
        "a control character survived into the caption track"
    )
    assert "управляющими" in text, "the words around the control characters must survive"


def test_build_srt_of_no_scenes_is_an_empty_string():
    assert build_srt([], scene_seconds=3.5) == ""


def test_build_srt_drops_a_scene_whose_text_cleans_to_nothing():
    """An empty cue is a blank flash on screen; the numbering must stay contiguous."""
    scenes = _scenes("Первый факт.", "https://only-a-link.example/x", "Третий факт.")

    cues = _parse_srt(build_srt(scenes, scene_seconds=2.0))

    assert [cue[0] for cue in cues] == [1, 2]
    assert [cue[3] for cue in cues] == ["Первый факт.", "Третий факт."]


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf")])
def test_build_srt_rejects_a_non_positive_scene_length(bad):
    with pytest.raises(ValueError, match="scene_seconds"):
        build_srt(_scenes("Текст"), scene_seconds=bad)


def test_write_srt_writes_utf8_without_a_bom(tmp_path):
    target = tmp_path / "nested" / "captions.srt"

    written = write_srt(_scenes("Привет, мир."), target, scene_seconds=3.0)

    assert written == target
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "a BOM makes some players show a stray glyph"
    assert raw.decode("utf-8").startswith("1\n00:00:00,000 --> 00:00:03,000\nПривет, мир.")


def test_qc_video_refuses_a_missing_file(tmp_path):
    result = qc_video(tmp_path / "never-rendered.mp4", max_duration_s=180, max_mb=45)

    assert result["ok"] is False
    assert result["reason"] == "file not found"
    assert result["checks"]["exists"] is False


def test_qc_video_refuses_an_empty_file(tmp_path):
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")

    result = qc_video(empty, max_duration_s=180, max_mb=45)

    assert result["ok"] is False
    assert result["reason"] == "file is empty"
    assert result["checks"]["non_empty"] is False


def test_qc_video_cannot_report_success_when_ffprobe_is_missing(tmp_path, monkeypatch):
    """The main test of this file: no tool must never look like a pass.

    A QC that answers ``ok: True`` because it had nothing to check with is the
    exact failure mode the project forbids — "проверено" must not be claimed for
    "не проверено". The reason has to name ffprobe so the caller can tell a
    missing tool from a broken file.
    """
    from tgfarm.s11_video import renderer

    video = tmp_path / "rendered.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * 512)
    monkeypatch.setattr(renderer.shutil, "which", lambda _name: None)

    result = qc_video(video, max_duration_s=180, max_mb=45)

    assert result["ok"] is False, "an unprobed file must not be reported as verified"
    assert "ffprobe" in result["reason"]
    assert result["checks"]["exists"] is True
    assert result["checks"]["non_empty"] is True


def test_probe_video_reports_the_missing_tool_instead_of_guessing(tmp_path, monkeypatch):
    from tgfarm.s11_video import renderer

    video = tmp_path / "rendered.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * 512)
    monkeypatch.setattr(renderer.shutil, "which", lambda _name: None)

    result = probe_video(video)

    assert result["ok"] is False
    assert result["reason"] == "ffprobe unavailable"
    assert "duration_s" not in result, "a guessed duration is worse than none"


def _fake_probe(monkeypatch, *, duration_s: float, width: int = 1080, height: int = 1920,
                has_audio: bool = False):
    from tgfarm.s11_video import renderer

    def fake(path, **_kwargs):
        return {
            "ok": True,
            "duration_s": duration_s,
            "width": width,
            "height": height,
            "has_audio": has_audio,
            "size_bytes": Path(path).stat().st_size,
        }

    monkeypatch.setattr(renderer, "probe_video", fake)


def test_qc_video_passes_a_vertical_short_within_both_limits(tmp_path, monkeypatch):
    video = tmp_path / "good.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * 1024)
    _fake_probe(monkeypatch, duration_s=10.5)

    result = qc_video(video, max_duration_s=180, max_mb=45)

    assert result["ok"] is True, result["reason"]
    assert result["reason"] == ""
    assert result["checks"]["duration_within_limit"] is True
    assert result["checks"]["size_within_limit"] is True
    assert result["checks"]["vertical"] is True
    # A silent short is legal, so the missing audio track is a warning, not a refusal.
    assert result["warnings"] == ["no audio track"]


def test_qc_video_refuses_a_short_that_runs_past_the_duration_limit(tmp_path, monkeypatch):
    video = tmp_path / "long.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * 1024)
    _fake_probe(monkeypatch, duration_s=240.0)

    result = qc_video(video, max_duration_s=180, max_mb=45)

    assert result["ok"] is False
    assert result["checks"]["duration_within_limit"] is False
    assert "duration" in result["reason"] and "180" in result["reason"]


def test_qc_video_refuses_a_short_that_runs_past_the_size_limit(tmp_path, monkeypatch):
    video = tmp_path / "heavy.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * (2 * 1024 * 1024))
    _fake_probe(monkeypatch, duration_s=10.0)

    result = qc_video(video, max_duration_s=180, max_mb=1)

    assert result["ok"] is False
    assert result["checks"]["size_within_limit"] is False
    assert "size" in result["reason"] and "1 MB" in result["reason"]


def test_qc_video_warns_about_a_landscape_frame_without_refusing_it(tmp_path, monkeypatch):
    """A 16:9 file is an operator judgement call, not a broken render."""
    video = tmp_path / "landscape.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * 1024)
    _fake_probe(monkeypatch, duration_s=10.0, width=1920, height=1080, has_audio=True)

    result = qc_video(video, max_duration_s=180, max_mb=45)

    assert result["ok"] is True, result["reason"]
    assert result["checks"]["vertical"] is False
    assert any("not vertical" in warning for warning in result["warnings"])
    assert "no audio track" not in result["warnings"]


def test_qc_video_refuses_a_zero_duration_file(tmp_path, monkeypatch):
    video = tmp_path / "zero.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * 1024)
    _fake_probe(monkeypatch, duration_s=0.0)

    result = qc_video(video, max_duration_s=180, max_mb=45)

    assert result["ok"] is False
    assert "duration is zero" in result["reason"]


# --- the render stage wires both new stages in -------------------------------

TITLE = "Почему меняется рынок технологий"
BODY = (
    "Компании пересматривают планы после новых данных. "
    "Эксперты отмечают рост затрат, но отмечают и новые возможности. "
    "Результат будет зависеть от того, как быстро бизнес адаптируется."
)


def _fake_ffmpeg(monkeypatch, *, fail_when: str | None = None):
    """Stand in for FFmpeg: write a plausible MP4 and record every command.

    `fail_when` makes the first command whose `-vf` contains that substring exit
    non-zero, which is how the "libass is missing" branch is reached without a
    real FFmpeg build.
    """
    from types import SimpleNamespace

    from tgfarm.s11_video import renderer

    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        vf = command[command.index("-vf") + 1] if "-vf" in command else ""
        if fail_when and fail_when in vf:
            return SimpleNamespace(returncode=1, stderr="No such filter: 'subtitles'")
        Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisom" + b"mock-video")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(renderer.subprocess, "run", fake_run)
    return calls


def test_render_short_video_reports_qc_and_never_claims_success_without_ffprobe(
    tmp_path, monkeypatch
):
    """The render must carry a QC verdict, and a missing ffprobe must show as one."""
    from tgfarm.s11_video import renderer

    _fake_ffmpeg(monkeypatch)
    monkeypatch.setattr(renderer.shutil, "which", lambda _name: None)
    output = tmp_path / "rendered.mp4"

    artifact = renderer.render_short_video(
        TITLE, BODY, output_path=output, ffmpeg_bin="ffmpeg-test-double",
    )

    assert artifact.qc is not None, "a render without a QC verdict is an unverified render"
    assert artifact.qc["ok"] is False
    assert "ffprobe" in artifact.qc["reason"]
    assert artifact.subtitles == {"burned": False, "reason": "not requested", "path": None}


def test_render_short_video_burns_subtitles_into_the_same_ffmpeg_pass(tmp_path, monkeypatch):
    from tgfarm.s11_video import renderer

    calls = _fake_ffmpeg(monkeypatch)
    monkeypatch.setattr(renderer.shutil, "which", lambda _name: None)
    output = tmp_path / "rendered.mp4"

    artifact = renderer.render_short_video(
        TITLE, BODY, output_path=output, ffmpeg_bin="ffmpeg-test-double",
        burn_subtitles=True,
    )

    assert len(calls) == 1, "burning must not cost a second encode"
    vf = calls[0][calls[0].index("-vf") + 1]
    assert "subtitles=filename=" in vf
    assert "force_style=" in vf, (
        "without an explicit style libass uses its own default: a ~48px line "
        "pinned to the bottom edge, which prints over the card's footer text"
    )
    assert artifact.subtitles["burned"] is True
    sidecar = Path(artifact.subtitles["path"])
    assert sidecar.is_file() and sidecar.suffix == ".srt"
    assert sidecar.read_text(encoding="utf-8").startswith("1\n00:00:00,000 --> ")


def test_the_burned_style_is_written_in_the_script_space_libass_uses():
    """An SRT script has no PlayResX/Y, so libass lays it out on 384x288.

    The frame is 1080x1920, so every length in the style is scaled by ~6.67 before
    it reaches the picture. `Fontsize=30,MarginV=300` — numbers that look right for
    a 1080p frame — puts the cue off the bottom of the screen entirely, and the
    visible failure is silent: FFmpeg exits 0 and the default style shows through.
    These are the magnitudes that were measured against extracted frames.
    """
    from tgfarm.s11_video.renderer import SUBTITLE_STYLE

    values = dict(
        part.split("=", 1) for part in SUBTITLE_STYLE.split(",") if "=" in part
    )
    assert int(values["Fontsize"]) <= 10, (
        f"Fontsize={values['Fontsize']} is frame pixels, not script units: at the "
        f"~6.67x scale the cue alone would be {int(values['Fontsize']) * 7}px tall"
    )
    bottom_margin = int(values["MarginV"])
    assert 30 <= bottom_margin <= 60, (
        f"MarginV={bottom_margin} puts the cue's lower line at y≈"
        f"{1920 - bottom_margin * 6.67:.0f}; the card's footer starts at 1665 and "
        "the panel ends at 1530, and the cue belongs between them"
    )
    assert values["Alignment"] == "2", "the cue must stay bottom-centre"


def test_render_post_short_passes_the_caption_choice_through(tmp_path, monkeypatch):
    """The low-level flag is worthless if the product path cannot reach it.

    `render_post_short` is what the API and the CLI actually call, so the caption
    option has to survive that hop; a mutant that drops the keyword argument here
    leaves every test above green while making the feature unreachable. The post
    is a real row, because this function reads it before it renders anything.
    """
    import uuid

    from tgfarm.db.models import Channel, Post
    from tgfarm.db.session import session_scope
    from tgfarm.s11_video import renderer

    suffix = uuid.uuid4().hex[:8]
    public_id = "qc_burn_" + suffix
    with session_scope() as session:
        session.add(Channel(id="qc_ch_" + suffix, title="QC", niche="tech", platform="telegram"))
        session.add(Post(public_id=public_id, channel_id="qc_ch_" + suffix,
                         hook=TITLE, body=BODY, status="published"))

    seen: list[dict] = []

    def fake_render(title, body, **kwargs):
        seen.append(kwargs)
        return renderer.ShortVideoArtifact(
            path=str(tmp_path / "x.mp4"), title=title, width=1080, height=1920, fps=30,
            duration_s=1.0, scene_count=1, bytes=1, sha256="b" * 64,
        )

    monkeypatch.setattr(renderer, "render_short_video", fake_render)
    monkeypatch.setattr("tgfarm.s6_visual.persist_media", lambda *a, **k: None)
    try:
        renderer.render_post_short(public_id, output_path=tmp_path / "x.mp4",
                                   burn_subtitles=True)
        assert seen and seen[-1].get("burn_subtitles") is True, seen
        renderer.render_post_short(public_id, output_path=tmp_path / "x.mp4",
                                   burn_subtitles=False)
        assert seen[-1].get("burn_subtitles") is False, seen
    finally:
        with session_scope() as session:
            session.query(Post).filter(Post.public_id == public_id).delete()
            session.query(Channel).filter(Channel.id == "qc_ch_" + suffix).delete()


def test_the_cli_offers_the_caption_flag(monkeypatch):
    """`tgfarm video-render --subtitles` must parse and reach the renderer."""
    from tgfarm import cli
    from tgfarm.s11_video import renderer

    seen: dict = {}

    class _Art:
        def as_dict(self):
            return {"path": "x"}

    def fake_render_post_short(post_public_id, **kwargs):
        seen.update(kwargs)
        return _Art()

    monkeypatch.setattr(renderer, "render_post_short", fake_render_post_short)
    monkeypatch.setattr(cli, "_bootstrap", lambda *a, **k: None)
    code = cli.main(["video-render", "post-1", "--subtitles"])
    assert code == 0
    assert seen.get("burn_subtitles") is True, seen


def test_the_render_endpoint_accepts_the_caption_choice():
    """The endpoint parameter is the operator's only way to ask for captions."""
    source = (
        Path(__file__).resolve().parent.parent / "tgfarm" / "api.py"
    ).read_text(encoding="utf-8")
    marker = '"/api/posts/{post_public_id}/video/render"'
    assert marker in source, "the render endpoint is gone"
    start = source.index(marker)
    section = source[start: start + 1600]
    assert "subtitles: bool = False" in section, "the endpoint lost the parameter"
    assert "burn_subtitles=bool(subtitles)" in section, (
        "the endpoint accepts the parameter and never passes it to the renderer"
    )


def test_render_short_video_keeps_the_video_when_the_subtitle_filter_is_rejected(
    tmp_path, monkeypatch
):
    """A build without libass must cost the captions, not the render."""
    from tgfarm.s11_video import renderer

    calls = _fake_ffmpeg(monkeypatch, fail_when="subtitles=")
    monkeypatch.setattr(renderer.shutil, "which", lambda _name: None)
    output = tmp_path / "rendered.mp4"

    artifact = renderer.render_short_video(
        TITLE, BODY, output_path=output, ffmpeg_bin="ffmpeg-test-double",
        burn_subtitles=True,
    )

    assert output.is_file(), "the encoded video must survive a caption failure"
    assert len(calls) == 2, "one attempt with the filter, one without"
    assert "subtitles=" not in calls[1][calls[1].index("-vf") + 1]
    assert artifact.subtitles["burned"] is False
    assert "subtitles filter" in artifact.subtitles["reason"]
    # The track is still written: it is uploadable as a sidecar even unburned.
    assert Path(artifact.subtitles["path"]).is_file()

