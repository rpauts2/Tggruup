from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


def test_editorial_profiles_cover_every_catalogue_destination():
    from tgfarm.platform_editorial import PROFILES
    from tgfarm.platforms import CATALOG

    catalogue_ids = {item["id"] for item in CATALOG}
    assert catalogue_ids == set(PROFILES)
    assert all(profile.get("audience") for profile in PROFILES.values())
    assert all(profile.get("goal") for profile in PROFILES.values())
    assert all(profile.get("target_type") for profile in PROFILES.values())
    assert all(profile.get("rule_gate") for profile in PROFILES.values())


def test_native_format_selection_does_not_replace_required_video_with_text():
    from tgfarm.platform_editorial import choose_native_format

    youtube_without_video = choose_native_format("youtube", set())
    youtube_with_video = choose_native_format("youtube", {"video"})
    shorts_without_video = choose_native_format("vk_clips", {"image"})
    reddit_text = choose_native_format("reddit", set())

    assert youtube_without_video["eligible"] is False
    assert youtube_without_video["media_kind"] == "missing"
    assert youtube_with_video["eligible"] is True
    assert youtube_with_video["media_kind"] == "video"
    assert shorts_without_video["eligible"] is False
    assert reddit_text["eligible"] is True
    assert reddit_text["media_kind"] == "text"


def test_editorial_brief_contains_native_audience_target_and_gate():
    from tgfarm.platform_editorial import build_editorial_brief

    brief = build_editorial_brief(
        "pikabu",
        media_kinds={"image"},
        topic="Технологии и образование",
        source_angle="analysis",
    )

    assert brief["format_eligible"] is True
    assert brief["target_type"] == "community"
    assert "правил" in brief["rule_gate"].lower()
    assert brief["topic"] == "Технологии и образование"
    assert brief["source_angle"] == "analysis"


def test_reddit_and_consent_platforms_keep_their_special_workflows(monkeypatch):
    from tgfarm.platform_editorial import adapt_payload_with_ai

    payload = SimpleNamespace(
        platform="tiktok",
        title="A short video",
        text="An original explainer with context.",
        image_path=None,
        video_path="/tmp/original.mp4",
        link=None,
        hashtags=[],
        meta={},
    )
    import tgfarm.llm.policy as policy

    monkeypatch.setattr(
        policy,
        "should_use_ai",
        lambda *args, **kwargs: pytest.fail("a background TikTok crosspost must not use generic rewrite"),
    )

    result = asyncio.run(adapt_payload_with_ai(payload, allow_ai=True, preview_only=False))
    assert result["status"] == "special_workflow"


def test_preview_only_consent_format_can_be_ai_adapted_and_keeps_sources(monkeypatch):
    from tgfarm.llm import policy
    from tgfarm.platform_editorial import adapt_payload_with_ai

    payload = SimpleNamespace(
        platform="tiktok",
        title="An original video about a verified topic",
        text="A factual explanation with context.",
        image_path=None,
        video_path="/tmp/original.mp4",
        link="https://example.org/source",
        hashtags=["technology"],
        meta={"post_public_id": "published_editorial_123"},
    )
    monkeypatch.setattr(policy, "should_use_ai", lambda *args, **kwargs: (True, "test"))
    monkeypatch.setattr(
        policy,
        "reserve",
        lambda *args, **kwargs: SimpleNamespace(allowed=True, usage_id=123, reason="reserved"),
    )
    monkeypatch.setattr(policy, "finalize", lambda *args, **kwargs: None)

    calls = {}

    class FakeLLM:
        async def complete_json(self, *args, **kwargs):
            calls["model"] = kwargs.get("model")
            return (
                {"title": "Native video title", "body": "A short native caption.", "notes": []},
                SimpleNamespace(provider="test-provider", model="test-model", latency_ms=12),
            )

    monkeypatch.setattr(
        "tgfarm.core.settings.get_settings",
        lambda: SimpleNamespace(llm_platform_editor_model="editor-model-v2"),
    )
    monkeypatch.setattr("tgfarm.llm.client.get_llm", lambda: FakeLLM())
    result = asyncio.run(adapt_payload_with_ai(payload, allow_ai=True, preview_only=True))

    assert result["status"] == "ai_native"
    assert result["used_ai"] is True
    assert calls["model"] == "editor-model-v2"
    assert payload.meta["editorial_adaptation"]["model"] == "test-model"
    assert payload.meta["editorial_adaptation"]["provider"] == "test-provider"
    assert payload.title == "Native video title"
    assert "https://example.org/source" in payload.text
    assert payload.meta["editorial_adaptation"]["status"] == "ai_native"


def test_profiles_do_not_advertise_media_the_current_adapter_cannot_publish():
    from tgfarm.platform_editorial import PROFILES, choose_native_format

    text_only = ("max", "x", "bluesky", "threads", "linkedin", "mastodon", "discord", "tumblr")
    for platform_id in text_only:
        profile = PROFILES[platform_id]
        assert profile["text_format"]
        assert profile["image_format"] is None, platform_id
        assert profile["video_format"] is None, platform_id
        assert choose_native_format(platform_id, {"image"})["media_kind"] == "text"
        assert choose_native_format(platform_id, {"video"})["media_kind"] == "text"

    assert PROFILES["vk"]["image_format"]
    assert PROFILES["vk"]["video_format"] is None
    assert choose_native_format("vk", {"image", "video"})["media_kind"] == "image"
    assert choose_native_format("vk", {"video"})["media_kind"] == "text"

    assert PROFILES["instagram"]["image_format"] == "одна фотография с caption"
    assert PROFILES["instagram"]["video_format"] == "один Reel с caption"
    assert PROFILES["pinterest"]["image_format"]
    assert PROFILES["pinterest"]["video_format"]
    assert PROFILES["facebook"]["image_format"]
    assert PROFILES["facebook"]["video_format"]
    assert PROFILES["odnoklassniki"]["image_format"]
    assert PROFILES["odnoklassniki"]["video_format"]

    assert PROFILES["tiktok"]["image_format"] is None
    assert PROFILES["tiktok"]["video_format"]
    assert choose_native_format("tiktok", {"image"})["eligible"] is False
    assert choose_native_format("tiktok", {"video"})["eligible"] is True
    assert PROFILES["douyin"]["image_format"] is None
    assert PROFILES["douyin"]["video_format"]


def test_owner_selected_destination_is_not_claimed_to_be_auto_discovered():
    from tgfarm.platform_editorial import PROFILES

    assert "указал в настройках" in PROFILES["pinterest"]["target_selection"]
    assert "ID" in PROFILES["pinterest"]["target_selection"]
    assert "доску" in PROFILES["pinterest"]["target_selection"]
    assert "Facebook Page" in PROFILES["facebook"]["target_selection"]
    assert "ID" not in PROFILES["facebook"]["target_selection"]


def test_dzen_chooses_post_or_article_from_the_real_1200_character_cutoff():
    from tgfarm.platform_editorial import choose_native_format

    short = choose_native_format("dzen", set(), content_chars=1199)
    article = choose_native_format("dzen", set(), content_chars=1200)
    illustrated = choose_native_format("dzen", {"image"}, content_chars=1600)

    assert short["format"] == "Пост Дзена"
    assert short["media_kind"] == "text"
    assert article["format"] == "Статья Дзена"
    assert article["media_kind"] == "text"
    assert illustrated["format"] == "Статья Дзена с иллюстрацией"
    assert illustrated["media_kind"] == "image"


def test_pinterest_video_pin_requires_a_real_cover_image():
    from tgfarm.platform_editorial import choose_native_format

    without_cover = choose_native_format("pinterest", {"video"})
    with_cover = choose_native_format("pinterest", {"video", "image"})

    assert without_cover["eligible"] is False
    assert "обложка" in without_cover["reason"].lower()
    assert with_cover["eligible"] is True
    assert with_cover["media_kind"] == "video"


def test_native_rewrite_provider_failure_keeps_deterministic_payload(monkeypatch):
    from tgfarm.llm import policy
    from tgfarm.platform_editorial import adapt_payload_with_ai

    payload = SimpleNamespace(
        platform="bluesky",
        title="Original factual title",
        text="Original source-backed text. https://example.org/source",
        image_path=None,
        video_path=None,
        link="https://example.org/source",
        hashtags=[],
        meta={"post_public_id": "published_ai_failure_123", "source_marker": "keep"},
    )
    finalized = []
    monkeypatch.setattr(policy, "should_use_ai", lambda *args, **kwargs: (True, "test"))
    monkeypatch.setattr(
        policy,
        "reserve",
        lambda *args, **kwargs: SimpleNamespace(allowed=True, usage_id=456, reason="reserved"),
    )
    monkeypatch.setattr(
        policy,
        "finalize",
        lambda *args, **kwargs: finalized.append(kwargs.get("used")),
    )

    class BrokenLLM:
        async def complete_json(self, *args, **kwargs):
            raise RuntimeError("provider temporarily unavailable")

    monkeypatch.setattr("tgfarm.llm.client.get_llm", lambda: BrokenLLM())
    result = asyncio.run(adapt_payload_with_ai(payload, allow_ai=True))

    assert result["used_ai"] is False
    assert result["status"] == "deterministic_fallback"
    assert payload.title == "Original factual title"
    assert payload.text == "Original source-backed text. https://example.org/source"
    assert payload.meta == {"post_public_id": "published_ai_failure_123", "source_marker": "keep"}
    assert finalized == [False]


def test_native_rewrite_rejects_new_numbers_and_unapproved_urls(monkeypatch):
    from tgfarm.llm import policy
    from tgfarm.platform_editorial import adapt_payload_with_ai

    payload = SimpleNamespace(
        platform="bluesky",
        title="Original source-backed title",
        text="The source confirms a qualitative change. https://example.org/source",
        image_path=None,
        video_path=None,
        link="https://example.org/source",
        hashtags=[],
        meta={"post_public_id": "native_guardrail_001"},
    )
    finalized = []
    monkeypatch.setattr(policy, "should_use_ai", lambda *args, **kwargs: (True, "test"))
    monkeypatch.setattr(
        policy, "reserve",
        lambda *args, **kwargs: SimpleNamespace(allowed=True, usage_id=789, reason="reserved"),
    )
    monkeypatch.setattr(
        policy, "finalize",
        lambda *args, **kwargs: finalized.append(kwargs.get("used")),
    )
    monkeypatch.setattr(
        "tgfarm.core.settings.get_settings",
        lambda: SimpleNamespace(llm_platform_editor_model=""),
    )

    class InventingLLM:
        async def complete_json(self, *args, **kwargs):
            return (
                {
                    "title": "Updated title with 999 users",
                    "body": "The change affects 999 users. https://unknown.example/new",
                    "notes": [],
                },
                SimpleNamespace(provider="test-provider", model="editor-v1", latency_ms=8),
            )

    monkeypatch.setattr("tgfarm.llm.client.get_llm", lambda: InventingLLM())
    result = asyncio.run(adapt_payload_with_ai(payload, allow_ai=True))

    assert result["used_ai"] is False
    assert result["status"] == "deterministic_guardrail"
    assert "числовые значения" in result["reason"]
    assert result["provider"] == "test-provider"
    assert result["model"] == "editor-v1"
    assert payload.meta["editorial_adaptation"]["provider"] == "test-provider"
    assert payload.meta["editorial_adaptation"]["model"] == "editor-v1"
    assert payload.title == "Original source-backed title"
    assert "999" not in payload.text
    assert payload.meta == {"post_public_id": "native_guardrail_001"}
    assert finalized == [False]
