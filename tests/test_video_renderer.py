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


def test_corrupt_cached_mp4_is_not_reported_as_reused(tmp_path, monkeypatch):
    from tgfarm.s11_video import renderer

    target = tmp_path / "corrupt.mp4"
    target.write_bytes(b"not an mp4")
    monkeypatch.setattr(renderer.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="FFmpeg не найден"):
        renderer.render_short_video(TITLE, BODY, output_path=target)

    assert not target.exists()


def test_renderer_rejects_duration_limit_shorter_than_minimum_storyboard(monkeypatch, tmp_path):
    from tgfarm.s11_video import renderer

    monkeypatch.setattr(
        renderer,
        "get_settings",
        lambda: SimpleNamespace(video_max_duration_s=2, font_path="", video_root=lambda: tmp_path),
    )
    with pytest.raises(ValueError, match="VIDEO_MAX_DURATION_S"):
        renderer.render_short_video(TITLE, BODY, output_path=tmp_path / "too-short.mp4")


def test_render_post_short_attaches_video_without_replacing_primary_cover(
    tmp_path, monkeypatch
):
    import hashlib
    import uuid
    from types import SimpleNamespace

    from tgfarm.db.models import Channel, Media, Post
    from tgfarm.db.session import session_scope
    from tgfarm.s11_video import renderer

    suffix = uuid.uuid4().hex[:8]
    channel_id = "video_attach_" + suffix
    public_id = "video_post_" + suffix
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"cover")

    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Video attach", niche="tech", platform="telegram"))
        post = Post(
            public_id=public_id,
            channel_id=channel_id,
            hook=TITLE,
            body=BODY,
            status="published",
        )
        session.add(post)
        session.flush()
        post_id = post.id
        session.add(
            Media(
                post_id=post_id,
                kind="template",
                path=str(cover),
                width=1280,
                height=720,
                sha=hashlib.sha256(cover.read_bytes()).hexdigest(),
                is_primary=True,
            )
        )

    output = tmp_path / "rendered.mp4"

    def fake_render(title, body, *, output_path=None, force=False, **_kwargs):
        output.write_bytes(bytes.fromhex("000000186674797069736f6d") + b"mock-video")
        return renderer.ShortVideoArtifact(
            path=str(output.resolve()),
            title=title,
            width=1080,
            height=1920,
            fps=30,
            duration_s=10.5,
            scene_count=3,
            bytes=output.stat().st_size,
            sha256="a" * 64,
        )

    monkeypatch.setattr(renderer, "render_short_video", fake_render)
    artifact = renderer.render_post_short(public_id, output_path=output)

    assert artifact.path == str(output.resolve())
    with session_scope() as session:
        rows = session.query(Media).filter_by(post_id=post_id).order_by(Media.id.asc()).all()

    assert len(rows) == 2
    primary = next(row for row in rows if row.is_primary)
    video = next(row for row in rows if row.kind == "video")
    assert primary.path == str(cover)
    assert video.path == str(output.resolve())
    assert video.is_primary is False
    assert video.width == 1080 and video.height == 1920

    # API consumers need both records: the non-primary MP4 must not vanish
    # from the post drawer or replace the primary landscape cover.
    from tgfarm.api import _post_detail

    detail = _post_detail(public_id)
    assert detail["cover"]["id"] == primary.sha
    assert detail["video"]["id"] == video.sha

