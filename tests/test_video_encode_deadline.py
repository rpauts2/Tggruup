from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tgfarm.s11_video import renderer

TITLE = "Video deadline"
BODY = "One factual sentence."
MP4 = b"\x00\x00\x00\x18ftypisom" + b"test-video"


@pytest.fixture()
def render_env(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(
        renderer, "time", SimpleNamespace(monotonic=lambda: clock.now), raising=False,
    )
    monkeypatch.setattr(
        renderer, "get_settings",
        lambda: SimpleNamespace(video_max_duration_s=180, video_max_mb=45),
    )
    monkeypatch.setattr(
        renderer, "_draw_scene", lambda scene, index, total, path: path.write_bytes(b"frame"),
    )
    monkeypatch.setattr(renderer.shutil, "which", lambda name: None)
    return clock, tmp_path / "short.mp4"


@pytest.mark.parametrize(
    ("scene_seconds", "first_budget", "elapsed", "remaining"),
    [(3.5, 120, 80.25, 39.75), (20, 180, 123.5, 56.5), (3.5, 120, 119.75, 0.25)],
)
def test_caption_fallback_receives_only_remaining_budget(
    render_env, monkeypatch, scene_seconds, first_budget, elapsed, remaining,
):
    clock, target = render_env
    calls = []

    def encode(binary, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            clock.now += elapsed
            kwargs["target"].write_bytes(b"partial")
            return subprocess.CompletedProcess(binary, 1, stdout="", stderr="No such filter: subtitles")
        kwargs["target"].write_bytes(MP4)
        return subprocess.CompletedProcess(binary, 0, stdout="", stderr="")

    monkeypatch.setattr(renderer, "_encode", encode)
    artifact = renderer.render_short_video(
        TITLE, BODY, output_path=target, ffmpeg_bin="fake-ffmpeg",
        scene_seconds=scene_seconds, burn_subtitles=True,
    )

    assert [call["timeout_s"] for call in calls] == pytest.approx([first_budget, remaining])
    assert "subtitles=" in calls[0]["vf"]
    assert "subtitles=" not in calls[1]["vf"]
    assert target.read_bytes() == MP4
    assert artifact.subtitles["burned"] is False
    assert "subtitles filter" in artifact.subtitles["reason"]
    assert Path(artifact.subtitles["path"]).is_file()


@pytest.mark.parametrize("elapsed", [120.0, 120.5])
def test_exhausted_caption_budget_does_not_retry_and_removes_partial(
    render_env, monkeypatch, elapsed,
):
    clock, target = render_env
    calls = []

    def encode(binary, **kwargs):
        calls.append(kwargs)
        clock.now += elapsed
        kwargs["target"].write_bytes(MP4)
        return subprocess.CompletedProcess(binary, 1, stdout="", stderr="subtitles rejected")

    monkeypatch.setattr(renderer, "_encode", encode)
    with pytest.raises(RuntimeError, match="FFmpeg превысил лимит времени рендеринга видео"):
        renderer.render_short_video(
            TITLE, BODY, output_path=target, ffmpeg_bin="fake-ffmpeg", burn_subtitles=True,
        )

    assert len(calls) == 1
    assert not target.exists()
    assert not target.with_suffix(".srt").exists()
    assert not target.with_suffix(".json").exists()


@pytest.mark.parametrize("burn_subtitles", [False, True])
def test_success_needs_only_one_encode(render_env, monkeypatch, burn_subtitles):
    clock, target = render_env
    calls = []

    def encode(binary, **kwargs):
        calls.append(kwargs)
        clock.now += 10
        kwargs["target"].write_bytes(MP4)
        return subprocess.CompletedProcess(binary, 0, stdout="", stderr="")

    monkeypatch.setattr(renderer, "_encode", encode)
    artifact = renderer.render_short_video(
        TITLE, BODY, output_path=target, ffmpeg_bin="fake-ffmpeg", burn_subtitles=burn_subtitles,
    )

    assert len(calls) == 1
    assert calls[0]["timeout_s"] == 120
    assert ("subtitles=" in calls[0]["vf"]) is burn_subtitles
    assert artifact.path == str(target.resolve())
    assert artifact.duration_s == 10.5
    assert artifact.subtitles["burned"] is burn_subtitles
    assert target.with_suffix(".srt").exists() is burn_subtitles
    manifest = json.loads(target.with_suffix(".json").read_text(encoding="utf-8"))
    assert manifest["asset"]["sha256"] == artifact.sha256
    assert manifest["asset"]["subtitles"] == artifact.subtitles


def test_non_caption_failure_is_not_retried(render_env, monkeypatch):
    clock, target = render_env
    calls = []

    def encode(binary, **kwargs):
        calls.append(kwargs)
        clock.now += 130
        kwargs["target"].write_bytes(b"partial")
        return subprocess.CompletedProcess(binary, 1, stdout="", stderr="encoder rejected input")

    monkeypatch.setattr(renderer, "_encode", encode)
    with pytest.raises(RuntimeError, match="FFmpeg не смог собрать видео: encoder rejected input"):
        renderer.render_short_video(TITLE, BODY, output_path=target, ffmpeg_bin="fake-ffmpeg")

    assert len(calls) == 1
    assert calls[0]["timeout_s"] == 120
    assert not target.exists()


@pytest.mark.parametrize(
    ("burn_subtitles", "reject_caption", "budgets"),
    [(True, False, [120]), (True, True, [120, 39.75]), (False, False, [120])],
)
def test_subprocess_timeout_removes_partial_and_preserves_runtime_error_contract(
    render_env, monkeypatch, burn_subtitles, reject_caption, budgets,
):
    clock, target = render_env
    timeouts = []

    def run(command, **kwargs):
        timeouts.append(kwargs["timeout"])
        Path(command[-1]).write_bytes(b"partial")
        if reject_caption and "subtitles=" in command[command.index("-vf") + 1]:
            clock.now += 80.25
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="subtitles rejected")
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(renderer.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="FFmpeg превысил лимит времени рендеринга видео") as error:
        renderer.render_short_video(
            TITLE, BODY, output_path=target, ffmpeg_bin="fake-ffmpeg", burn_subtitles=burn_subtitles,
        )

    assert timeouts == pytest.approx(budgets)
    assert isinstance(error.value.__cause__, subprocess.TimeoutExpired)
    assert not target.exists()
    assert not target.with_suffix(".srt").exists()
    assert not target.with_suffix(".json").exists()
