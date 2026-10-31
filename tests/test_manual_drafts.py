from __future__ import annotations

import pytest

from tgfarm.manual_drafts import MANUAL_DESTINATIONS, build_manual_package


def _post(**overrides):
    value = {
        "id": "post-123",
        "hook": "Заголовок",
        "body": "# Факт\n\n**Текст статьи.**\n\n# AI #Science",
        "cta": "Подробнее в источнике.",
        "hashtags": ["AI", "Science"],
        "sources": [{"title": "Документ", "url": "https://example.com/source"}],
        "media": [],
    }
    value.update(overrides)
    return value


def test_manual_package_has_no_publish_side_effect_and_keeps_source():
    package = build_manual_package("habr", _post())
    assert package["manual_only"] is True
    assert package["publish_action"] == "none"
    assert package["name"] == "Хабр"
    assert package["title"] == "Заголовок"
    assert "Текст статьи." in package["body"]
    assert "https://example.com/source" in package["body"]
    assert package["hashtags"] == ["AI", "Science"]
    assert package["url"] == "https://habr.com/ru/articles/new/"


def test_video_manual_package_blocks_missing_mp4_and_never_leaks_local_paths():
    package = build_manual_package("rutube", _post(media=[
        {"kind": "template", "sha": "cover-hash", "path": "C:\\private\\cover.png"},
    ]))
    assert package["ready"] is False
    assert package["has_video"] is False
    assert all("C:\\" not in str(item) for item in package["media"])
    assert "создай его" in package["notes"][0]


def test_video_manual_package_exposes_local_download_only_when_mp4_exists():
    package = build_manual_package("vk_clips", _post(media=[
        {"kind": "video", "sha": "video-hash", "path": "/home/private/video.mp4"},
    ]))
    assert package["ready"] is True
    assert package["media"] == [{
        "kind": "video",
        "url": "/api/media/video-hash",
        "filename": "tgfarm-post-123.mp4",
    }]


def test_manual_package_rejects_unregistered_or_empty_posts():
    with pytest.raises(ValueError, match="ручный пакет"):
        build_manual_package("instagram", _post())
    with pytest.raises(ValueError, match="заголовок и основной текст"):
        build_manual_package("habr", _post(hook=""))


def test_manual_destination_registry_covers_non_api_destinations():
    assert {
        "vk_clips", "rutube", "vc_ru", "habr", "pikabu", "tenchat", "medium",
        "whatsapp_channels", "twitch", "snapchat", "wechat", "douyin", "rednote",
    } <= set(MANUAL_DESTINATIONS)
