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
    from tgfarm import llm
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

    class FakeLLM:
        async def complete_json(self, *args, **kwargs):
            return (
                {"title": "Native video title", "body": "A short native caption.", "notes": []},
                SimpleNamespace(provider="test-provider", model="test-model", latency_ms=12),
            )

    monkeypatch.setattr("tgfarm.llm.client.get_llm", lambda: FakeLLM())
    result = asyncio.run(adapt_payload_with_ai(payload, allow_ai=True, preview_only=True))

    assert result["status"] == "ai_native"
    assert result["used_ai"] is True
    assert payload.title == "Native video title"
    assert "https://example.org/source" in payload.text
    assert payload.meta["editorial_adaptation"]["status"] == "ai_native"
