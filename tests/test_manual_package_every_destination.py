"""Каждый ручной пакет должен быть пригоден для вставки в редактор своей площадки."""
from __future__ import annotations

import pytest

from tgfarm.manual_drafts import MANUAL_DESTINATIONS, build_manual_package
from tgfarm.platform_editorial import PROFILES

LONG_BODY = "\n\n".join(f"Абзац {i}. " + "слово " * 60 for i in range(400))
LOCAL = "/home/private/secret/cover.mp4"


def _post(**overrides):
    value = {
        "id": "post-1",
        "hook": "Заголовок",
        "body": LONG_BODY,
        "cta": "Подробнее в источнике.",
        "hashtags": ["AI"],
        "sources": [{"title": "Документ", "url": "https://example.com/s"}],
        "media": [{"kind": "video", "sha": "abc", "path": LOCAL}],
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize("platform_id", sorted(MANUAL_DESTINATIONS))
def test_package_body_fits_the_editor_limit_of_its_platform(platform_id):
    package = build_manual_package(platform_id, _post())
    limit = PROFILES[platform_id]["max_chars"]
    assert 0 < len(package["body"]) <= limit, (platform_id, len(package["body"]), limit)


@pytest.mark.parametrize("platform_id", sorted(MANUAL_DESTINATIONS))
def test_package_is_manual_only_and_ready_with_video(platform_id):
    package = build_manual_package(platform_id, _post())
    assert package["manual_only"] is True
    assert package["publish_action"] == "none"
    assert package["ready"] is True
    assert package["url"].startswith("https://")
    assert LOCAL not in str(package)


@pytest.mark.parametrize(
    "platform_id", sorted(k for k, v in MANUAL_DESTINATIONS.items() if v.get("needs_video"))
)
def test_video_platform_without_mp4_is_not_ready(platform_id):
    package = build_manual_package(platform_id, _post(media=[]))
    assert package["ready"] is False
    assert package["notes"] and "MP4" in package["notes"][0]


@pytest.mark.parametrize("platform_id", sorted(MANUAL_DESTINATIONS))
def test_short_body_is_not_altered_by_the_limit(platform_id):
    package = build_manual_package(platform_id, _post(body="Короткий текст.", sources=[], cta=""))
    assert package["body"] == "Короткий текст."
