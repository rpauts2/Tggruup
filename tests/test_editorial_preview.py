from __future__ import annotations

from fastapi.testclient import TestClient


def _post_detail(**overrides):
    value = {
        "id": "published_editorial_123",
        "status": "published",
        "hook": "Технологии меняют образование",
        "body": "Разбираем проверяемый пример и объясняем ограничения подхода.",
        "cta": "",
        "sources": [{"title": "Источник", "url": "https://example.org/source"}],
        "hashtags": ["technology", "education"],
        "angle": "analysis",
        "media": [],
    }
    value.update(overrides)
    return value


def _client():
    from tgfarm.api import create_app

    return TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1")


def test_editorial_preview_requires_api_authentication(monkeypatch):
    client = _client()
    try:
        response = client.post(
            "/api/platform-drafts/bluesky/published_editorial_123/editorial",
            json={},
        )
        assert response.status_code == 401
    finally:
        client.close()


def test_editorial_preview_returns_native_draft_without_publishing(monkeypatch):
    import tgfarm.api as api_module
    from tgfarm.core.apiauth import HEADER, ensure_token

    async def fake_adapt(payload, **kwargs):
        payload.title = "Нативный заголовок для Bluesky"
        payload.text = "Короткая версия с проверяемым выводом."
        return {"used_ai": True, "status": "ai_native", "reason": "test"}

    monkeypatch.setattr(api_module, "_post_detail", lambda _post_id: _post_detail())
    monkeypatch.setattr("tgfarm.platform_editorial.adapt_payload_with_ai", fake_adapt)
    client = _client()
    try:
        response = client.post(
            "/api/platform-drafts/bluesky/published_editorial_123/editorial",
            json={},
            headers={HEADER: ensure_token()},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["status"] == "preview_ready"
        assert result["draft"]["title"] == "Нативный заголовок для Bluesky"
        assert result["review_required"] is True
        assert result["publish_action"] == "none"
    finally:
        client.close()


def test_editorial_preview_refuses_video_destination_without_video(monkeypatch):
    import tgfarm.api as api_module
    from tgfarm.core.apiauth import HEADER, ensure_token

    monkeypatch.setattr(api_module, "_post_detail", lambda _post_id: _post_detail())
    client = _client()
    try:
        response = client.post(
            "/api/platform-drafts/youtube/published_editorial_123/editorial",
            json={},
            headers={HEADER: ensure_token()},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is False
        assert result["status"] == "blocked_missing_media"
        assert "видео" in result["detail"].lower() or "медиа" in result["detail"].lower()
        assert result["publish_action"] == "none"
    finally:
        client.close()


def test_editorial_preview_wont_invent_a_pikabu_community(monkeypatch):
    import tgfarm.api as api_module
    from tgfarm.core.apiauth import HEADER, ensure_token

    monkeypatch.setattr(api_module, "_post_detail", lambda _post_id: _post_detail())
    async def no_safe_community(*_args, **_kwargs):
        return {
            "selected": None,
            "candidates": [{"name": "Технологии", "score": 72, "blockers": ["Правила недоступны"]}],
            "general_notes": ["Правила не прочитаны"],
            "detail": "Нет сообщества с подтверждёнными правилами.",
        }

    monkeypatch.setattr("tgfarm.community_targets.recommend_community_target", no_safe_community)
    client = _client()
    try:
        response = client.post(
            "/api/platform-drafts/pikabu/published_editorial_123/editorial",
            json={},
            headers={HEADER: ensure_token()},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is False
        assert result["status"] == "needs_target"
        assert result["candidates"][0]["name"] == "Технологии"
        assert result["publish_action"] == "none"
    finally:
        client.close()
