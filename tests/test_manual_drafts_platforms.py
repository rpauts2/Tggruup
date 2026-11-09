"""Contract pins and defect regressions for the manual handoff packages.

Every destination in MANUAL_DESTINATIONS is manual-only in production, so this
module is the operator's real handoff. These tests pin that it stays honest: no
publish side effects, no silent content loss, and no invented platform limits.
"""
from __future__ import annotations

import socket

import pytest

from tgfarm.manual_drafts import MANUAL_DESTINATIONS, build_manual_package
from tgfarm.platform_editorial import PROFILES

DESTINATIONS = (
    "vk_clips", "rutube", "vc_ru", "habr", "pikabu", "tenchat", "medium",
    "whatsapp_channels", "twitch", "snapchat", "wechat", "douyin", "rednote", "tiktok",
)
ARTICLE_BODY = "# Факт\n\n**Текст статьи.**\n\n# AI #Science"


def _post(**overrides):
    value = {
        "id": "post-123",
        "hook": "Заголовок",
        "body": ARTICLE_BODY,
        "cta": "",
        "hashtags": ["AI", "Science"],
        "sources": [],
        "media": [],
    }
    value.update(overrides)
    return value


def test_registry_is_exactly_the_fourteen_manual_destinations():
    assert set(DESTINATIONS) == set(MANUAL_DESTINATIONS)


@pytest.mark.parametrize("platform_id", DESTINATIONS)
@pytest.mark.parametrize("with_video", [False, True], ids=["no_video", "video"])
@pytest.mark.parametrize("body", [ARTICLE_BODY, "#AI"], ids=["article", "hashtag_only"])
def test_manual_package_invariants_for_every_destination(platform_id, with_video, body):
    media = [{"kind": "video", "sha": "vid-hash"}] if with_video else []
    package = build_manual_package(platform_id, _post(body=body, media=media))

    assert package["ok"] is True
    assert package["manual_only"] is True
    assert package["publish_action"] == "none"
    assert package["platform"] == platform_id
    assert package["title"].strip()
    assert package["body"].strip(), "handoff body must never be silently emptied"

    needs_video = bool(MANUAL_DESTINATIONS[platform_id].get("needs_video"))
    assert package["needs_video"] is needs_video
    assert package["has_video"] is with_video
    expected_ready = not (needs_video and not with_video)
    assert package["ready"] is expected_ready


def test_package_build_never_opens_a_network_connection(monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("manual package must not contact a destination platform")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    for platform_id in DESTINATIONS:
        assert build_manual_package(platform_id, _post())["publish_action"] == "none"


# --- defect 1: blind str.replace() ate code identifiers and lone markers ---


def test_clean_text_preserves_dunder_snake_case_and_exponent():
    body = "Вызови __init__, присвой my__var и вычисли 2**10."
    package = build_manual_package("habr", _post(body=body))
    assert "__init__" in package["body"]
    assert "my__var" in package["body"]
    assert "2**10" in package["body"]


def test_clean_text_preserves_lone_tilde_marker_inside_quotes():
    package = build_manual_package("habr", _post(body='Автор написал "~~" и передумал.'))
    assert '"~~"' in package["body"]


def test_clean_text_still_strips_real_markdown_emphasis():
    body = "**Жирный** и __многословный акцент__ и `код` и ~~зачёркнуто~~."
    package = build_manual_package("habr", _post(body=body))
    assert package["body"] == "Жирный и многословный акцент и код и зачёркнуто."


# --- defect 2: trailing-line strip deleted legitimate numeric references ---


def test_trailing_numbered_reference_is_not_deleted():
    package = build_manual_package("habr", _post(body="Основной текст.\n\n#1"))
    assert package["body"].endswith("#1")


def test_trailing_numeric_token_mixed_with_hashtag_is_not_deleted():
    package = build_manual_package("habr", _post(body="Основной текст.\n#1 #AI"))
    assert "#1" in package["body"]


def test_trailing_word_hashtag_line_is_still_removed():
    package = build_manual_package("habr", _post(body="Основной текст.\n\n#AI #Science"))
    assert package["body"] == "Основной текст."


def test_hashtag_only_body_is_not_emptied():
    package = build_manual_package("habr", _post(body="#AI"))
    assert package["body"] == "#AI"


# --- defect 3: title limits ---


def test_twitch_title_over_documented_140_limit_is_rejected():
    with pytest.raises(ValueError, match="140"):
        build_manual_package("twitch", _post(hook="Я" * 141))


def test_twitch_title_at_documented_140_limit_is_accepted():
    package = build_manual_package("twitch", _post(hook="Я" * 140))
    assert len(package["title"]) == 140


def test_pikabu_title_over_140_limit_is_still_rejected():
    with pytest.raises(ValueError, match="140"):
        build_manual_package("pikabu", _post(hook="Я" * 141))


def test_unverified_title_limit_is_disclosed_instead_of_guessed():
    package = build_manual_package("medium", _post(hook="Я" * 200))
    assert len(package["title"]) == 200
    assert any("не подтверждён" in note and "200" in note for note in package["notes"])


def test_enforced_title_limit_is_not_reported_as_unverified():
    package = build_manual_package("twitch", _post(hook="Короткий заголовок"))
    assert not any("не подтверждён" in note for note in package["notes"])


# --- defect 4: http sources dropped without a word ---


def test_http_source_is_kept_and_attributed():
    post = _post(sources=[{"title": "HTTP", "url": "http://example.com/a"}])
    package = build_manual_package("habr", post)
    assert [item["url"] for item in package["sources"]] == ["http://example.com/a"]
    assert "http://example.com/a" in package["body"]


def test_unsupported_source_scheme_is_disclosed():
    post = _post(sources=[{"title": "FTP", "url": "ftp://example.com/b"}])
    package = build_manual_package("habr", post)
    assert package["sources"] == []
    assert any("не попала в пакет" in note for note in package["notes"])


def test_source_overflow_beyond_eight_is_disclosed():
    post = _post(sources=[{"title": f"S{i}", "url": f"https://example.com/{i}"} for i in range(10)])
    package = build_manual_package("habr", post)
    assert len(package["sources"]) == 8
    assert any("не попала в пакет" in note and "2" in note for note in package["notes"])


def test_non_http_source_is_never_rendered_into_the_body():
    post = _post(sources=[{"title": "JS", "url": "javascript:alert(1)"}])
    package = build_manual_package("habr", post)
    assert package["sources"] == []
    assert "javascript:" not in package["body"]


# --- defect 5: colliding download filenames ---


def test_multi_media_filenames_are_unique():
    post = _post(media=[
        {"kind": "ai_art", "sha": "a"},
        {"kind": "article_image", "sha": "b"},
        {"kind": "quote", "sha": "c"},
        {"kind": "video", "sha": "v1"},
        {"kind": "video", "sha": "v2"},
    ])
    package = build_manual_package("rednote", post)
    names = [item["filename"] for item in package["media"]]
    assert len(names) == 5
    assert len(set(names)) == 5, f"operator would download colliding files: {names}"
    assert [item["url"] for item in package["media"]] == [
        "/api/media/a", "/api/media/b", "/api/media/c", "/api/media/v1", "/api/media/v2",
    ]


def test_single_media_filename_is_unchanged():
    package = build_manual_package("rednote", _post(media=[{"kind": "ai_art", "sha": "a"}]))
    assert package["media"] == [
        {"kind": "ai_art", "url": "/api/media/a", "filename": "tgfarm-post-123.jpg"},
    ]


# --- defect 6: substring CTA check both misses and falsely matches ---


def test_cta_differing_only_in_whitespace_is_not_duplicated():
    post = _post(body="Текст  с   пробелами.", cta="Текст с пробелами.")
    package = build_manual_package("habr", post)
    normalized = " ".join(package["body"].split())
    assert normalized.count("Текст с пробелами.") == 1


def test_cta_inside_an_unrelated_word_is_still_appended():
    post = _post(body="Дизлайк убивает охваты.", cta="лайк")
    package = build_manual_package("habr", post)
    assert package["body"].endswith("лайк")


def test_cta_already_present_as_a_whole_phrase_is_not_duplicated():
    post = _post(body="Текст статьи. Подробнее в источнике.", cta="Подробнее в источнике.")
    package = build_manual_package("habr", post)
    assert " ".join(package["body"].split()).count("Подробнее в источнике.") == 1


# --- defect 7: silent truncation handed over as "ready" ---


def test_truncation_to_the_profile_limit_is_disclosed_with_both_lengths():
    body = "Слово " * 2000
    limit = PROFILES["whatsapp_channels"]["max_chars"]
    assert limit < 4000, "the editorial profile must bind here, not the internal ceiling"
    package = build_manual_package("whatsapp_channels", _post(body=body))
    assert len(package["body"]) <= limit
    assert package["ready"] is True
    assert any(
        "сокращён" in note
        and f"с {len(body.strip())}" in note
        and str(limit) in note
        and "профилем" in note
        for note in package["notes"]
    ), f"truncation was silent: {package['notes']}"


def test_truncation_to_the_internal_ceiling_is_disclosed_as_unverified():
    assert PROFILES["medium"]["max_chars"] > 30_000, "the internal ceiling must bind here"
    package = build_manual_package("medium", _post(body="Слово " * 6000))
    assert len(package["body"]) <= 30_000
    assert any(
        "сокращён" in note and "30000" in note and "не подтверждён" in note
        for note in package["notes"]
    ), f"truncation was silent: {package['notes']}"


def test_untruncated_body_gets_no_truncation_note():
    package = build_manual_package("whatsapp_channels", _post(body="Короткий текст."))
    assert not any("сокращён" in note for note in package["notes"])