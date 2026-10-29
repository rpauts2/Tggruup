from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


TITLE = "Почему меняется рынок технологий"
BODY = (
    "Компании пересматривают планы после новых данных. "
    "Эксперты отмечают рост затрат, но отмечают и новые возможности. "
    "Результат будет зависеть от того, как быстро бизнес адаптируется."
)


def test_storyboard_uses_only_post_facts_and_caps_scene_count():
    from tgfarm.s11_video.renderer import build_storyboard

    scenes = build_storyboard(TITLE, BODY, max_scenes=5)
    assert 3 <= len(scenes) <= 5
    assert scenes[0].role == "hook"
    assert scenes[0].text == TITLE
    assert scenes[-1].role == "close"
    assert all(scene.text.strip() for scene in scenes)
    assert "fake-source.invalid" not in repr(scenes)


def test_storyboard_removes_urls_and_markup_from_scene_text():
    from tgfarm.s11_video.renderer import build_storyboard

    scenes = build_storyboard(
        "Важное <b>обновление</b>",
        "Появились новые данные. Подробнее https://private.example/story?token=secret",
    )
    rendered_text = repr(scenes)
    assert "<b>" not in rendered_text
    assert "https://" not in rendered_text
    assert "secret" not in rendered_text


@pytest.mark.parametrize(
    ("title", "body", "message"),
    [
        ("", BODY, "заголовок"),
        (TITLE, "", "текста"),
    ],
)
def test_storyboard_rejects_missing_content(title, body, message):
    from tgfarm.s11_video.renderer import build_storyboard

    with pytest.raises(ValueError, match=message):
        build_storyboard(title, body)


def test_renderer_requires_ffmpeg_instead_of_fake_success(tmp_path, monkeypatch):
    from tgfarm.s11_video import renderer

    monkeypatch.setattr(renderer.shutil, "which", lambda _name: None)
    output = tmp_path / "missing.mp4"

    with pytest.raises(RuntimeError, match="FFmpeg не найден"):
        renderer.render_short_video(TITLE, BODY, output_path=output)

    assert not output.exists()


def test_renderer_creates_mp4_manifest_and_reuses_existing_artifact(tmp_path, monkeypatch):
    from tgfarm.s11_video import renderer

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        output = Path(command[-1])
        output.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00mock-video")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(renderer.subprocess, "run", fake_run)
    output = tmp_path / "rendered" / "short.mp4"

    artifact = renderer.render_short_video(
        TITLE,
        BODY,
        output_path=output,
        ffmpeg_bin="ffmpeg-test-double",
    )

    assert output.is_file()
    assert artifact.path == str(output.resolve())
    assert artifact.width == 1080
    assert artifact.height == 1920
    assert artifact.fps == 30
    assert artifact.scene_count >= 3
    assert artifact.bytes > 0
    assert artifact.sha256
    assert artifact.reused_existing is False
    assert len(calls) == 1

    command = calls[0][0]
    assert command[0] == "ffmpeg-test-double"
    assert "-c:v" in command and command[command.index("-c:v") + 1] == "libx264"
    assert "1080x1920" in command[command.index("-vf") + 1]
    assert command[-1] == str(output.resolve())

    manifest = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
    assert manifest["format"] == "tgfarm-original-short"
    assert manifest["audio"] == "none"
    assert manifest["source_video_reused"] is False
    assert manifest["asset"]["sha256"] == artifact.sha256

    # The content-addressed output can be reused without invoking FFmpeg again.
    monkeypatch.setattr(renderer.shutil, "which", lambda _name: None)
    reused = renderer.render_short_video(TITLE, BODY, output_path=output)
    assert reused.reused_existing is True
    assert reused.sha256 == artifact.sha256
    assert len(calls) == 1


def test_renderer_does_not_accept_non_mp4_output_path(tmp_path):
    from tgfarm.s11_video.renderer import render_short_video

    with pytest.raises(ValueError, match="MP4"):
        render_short_video(TITLE, BODY, output_path=tmp_path / "output.mov")


def test_video_render_cli_is_registered():
    from tgfarm.cli import build_parser

    args = build_parser().parse_args(["video-render", "post-123", "--force"])
    assert args.command == "video-render"
    assert args.post_id == "post-123"
    assert args.force is True
