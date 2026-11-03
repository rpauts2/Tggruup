from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from tgfarm import community_drafts


def _post(**overrides):
    value = {
        "title": "Новая технология",
        "body": "# Что произошло\n\n**Главный факт** и обычный текст.",
        "cta": "Обсудим результаты.",
        "hashtags": ["технологии", "AI"],
        "sources": [{"title": "Источник", "url": "https://example.com/story"}],
    }
    value.update(overrides)
    return value


def test_editor_text_is_clean_and_keeps_sources_and_call_to_action():
    text = community_drafts.compose_editor_text(_post(), "vc_ru")
    assert "# Что произошло" not in text
    assert "Главный факт" in text
    assert "**" not in text
    assert "Обсудим результаты." in text
    assert "Источник: https://example.com/story" in text
    assert "#технологии" in text


def test_pikabu_keeps_tags_out_of_the_body_for_native_tag_picker():
    text = community_drafts.compose_editor_text(_post(), "pikabu")
    assert "#технологии" not in text
    assert "Главный факт" in text


def test_pikabu_title_limit_is_enforced_without_silent_truncation():
    with pytest.raises(community_drafts.CommunityDraftError, match="140"):
        community_drafts.compose_editor_text(_post(title="А" * 141), "pikabu")


def test_editor_url_must_stay_on_the_exact_platform_domain():
    assert community_drafts._safe_editor_url("vc_ru", "https://vc.ru/editor/123")
    assert community_drafts._safe_editor_url("pikabu", "https://pikabu.ru/create")
    assert not community_drafts._safe_editor_url("vc_ru", "http://vc.ru/editor")
    assert not community_drafts._safe_editor_url("vc_ru", "https://vc.ru.evil.invalid/editor")
    assert not community_drafts._safe_editor_url("pikabu", "https://vc.ru/editor")


def test_only_article_photos_and_ai_art_are_selected(tmp_path: Path):
    article = tmp_path / "article.jpg"
    art = tmp_path / "ai-art.png"
    template = tmp_path / "template.png"
    for path in (article, art, template):
        Image.new("RGB", (640, 360), "white").save(path)

    media = [
        {"kind": "template", "path": str(template), "is_primary": True},
        {"kind": "ai_art", "path": str(art), "is_primary": True},
        {"kind": "article_image", "path": str(article), "is_primary": False},
        {"kind": "video", "path": str(template), "is_primary": True},
    ]
    selected = community_drafts._image_candidates(media, "pikabu")
    assert selected == [article, art]


def test_image_count_is_limited_per_platform(tmp_path: Path):
    paths = []
    media = []
    for index in range(4):
        path = tmp_path / f"photo-{index}.jpg"
        Image.new("RGB", (640, 360), "white").save(path)
        paths.append(path)
        media.append({"kind": "article_image", "path": str(path), "is_primary": index == 0})
    assert len(community_drafts._image_candidates(media, "vc_ru")) == 1
    assert len(community_drafts._image_candidates(media, "pikabu")) == 3


def test_state_registry_is_written_atomically_and_keeps_only_supplied_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(community_drafts, "DATA_DIR", tmp_path)
    community_drafts._write_state({
        "pikabu:post-123": {
            "status": "prepared",
            "draft_url": "https://pikabu.ru/create/123",
            "media_count": 1,
        }
    })
    restored = community_drafts._read_state()
    assert restored["pikabu:post-123"]["status"] == "prepared"
    assert restored["pikabu:post-123"]["media_count"] == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_fingerprint_changes_when_image_bytes_change(tmp_path: Path):
    image = tmp_path / "photo.jpg"
    Image.new("RGB", (640, 360), "white").save(image)
    first = community_drafts._fingerprint("pikabu", "Title", "Body", [image])
    Image.new("RGB", (640, 360), "black").save(image)
    second = community_drafts._fingerprint("pikabu", "Title", "Body", [image])
    assert first != second


def test_state_entry_updates_merge_across_platforms(tmp_path, monkeypatch):
    monkeypatch.setattr(community_drafts, "DATA_DIR", tmp_path)
    community_drafts._write_state_entry(
        "vc_ru:post-123",
        {"status": "preparing", "draft_url": "https://vc.ru/editor/123"},
    )
    community_drafts._write_state_entry(
        "pikabu:post-456",
        {"status": "prepared", "draft_url": "https://pikabu.ru/create/456"},
    )
    restored = community_drafts._read_state()
    assert restored["vc_ru:post-123"]["status"] == "preparing"
    assert restored["pikabu:post-456"]["status"] == "prepared"


def test_pikabu_scheduler_respects_hour_gap_creation_day_and_pending_limit():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)
    first = community_drafts.plan_pikabu_schedule(now=now, entries={}, timezone_name="UTC")
    assert first == datetime(2026, 11, 3, 9, 0, tzinfo=UTC)

    entries = {"pikabu:one": {
        "status": "scheduled",
        "created_at": datetime(2026, 11, 3, 7, 0, tzinfo=UTC).isoformat(),
        "scheduled_at": datetime(2026, 11, 3, 10, 30, tzinfo=UTC).isoformat(),
    }}
    second = community_drafts.plan_pikabu_schedule(now=now, entries=entries, timezone_name="UTC")
    assert second == datetime(2026, 11, 4, 9, 0, tzinfo=UTC)

    crowded = {
        f"pikabu:{index}": {
            "status": "scheduled",
            "created_at": now.isoformat(),
            "scheduled_at": (now + timedelta(days=index + 1)).isoformat(),
        }
        for index in range(12)
    }
    with pytest.raises(community_drafts.CommunityDraftError, match="12"):
        community_drafts.plan_pikabu_schedule(now=now, entries=crowded, timezone_name="UTC")


def test_pikabu_tag_validation_requires_two_real_tags_and_limits_tag_length():
    assert community_drafts.normalize_pikabu_tags(["Python", "Programming", "Python"]) == ["Python", "Programming"]
    with pytest.raises(community_drafts.CommunityDraftError, match="от 2 до 20"):
        community_drafts.normalize_pikabu_tags(["Python"])
    with pytest.raises(community_drafts.CommunityDraftError, match="50"):
        community_drafts.normalize_pikabu_tags(["x" * 51, "Python"])



def test_pikabu_changed_material_cannot_duplicate_an_existing_scheduled_item(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from tgfarm import community_targets
    public_id = "pikabu_revision_123"
    async def selected_target(*_args, **_kwargs):
        return {
            "ok": True,
            "selected": {
                "url": "https://pikabu.ru/community/technology",
                "name": "Технологии",
            },
            "tags": ["Python", "Programming"],
        }

    monkeypatch.setattr(
        community_targets,
        "recommend_automatic_pikabu_target",
        selected_target,
    )
    monkeypatch.setattr(
        "tgfarm.core.settings.get_settings",
        lambda: SimpleNamespace(tz="UTC"),
    )
    monkeypatch.setattr(
        community_drafts,
        "_read_state",
        lambda: {
            f"pikabu:{public_id}": {
                "status": "scheduled",
                "fingerprint": "old-version-fingerprint",
                "scheduled_at": "2026-11-04T12:00:00+00:00",
                "target_url": "https://pikabu.ru/community/technology",
            }
        },
    )
    monkeypatch.setattr(
        community_drafts,
        "_write_state_entry",
        lambda *_args, **_kwargs: pytest.fail("must not replace the existing scheduled entry"),
    )

    with pytest.raises(community_drafts.CommunityDraftError, match="уже есть отложенная публикация"):
        asyncio.run(
            community_drafts.publish_pikabu_scheduled(
                public_id,
                title="A revised Python guide",
                body="A changed article body about Python programming.",
            )
        )
