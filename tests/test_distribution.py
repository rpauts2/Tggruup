from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from tgfarm.api import create_app

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as test_client:
        yield test_client


def _post(*, title: str, body: str, angle: str = "analysis", age_hours: int = 2):
    return SimpleNamespace(
        public_id="distribution-test-post",
        hook=title,
        body=body,
        angle=angle,
        published_at=datetime.now(UTC) - timedelta(hours=age_hours),
        quality={"gate": "pass", "scores": {"facts": 0.95, "structure": 0.9, "tone": 0.85}},
        claims=[{"verified": True}, {"verified": True}],
        sources=[{"title": "Primary source", "url": "https://example.com/source"}],
    )


def test_editorial_score_prefers_platform_topic_fit():
    from tgfarm.distribution import score_post_for_platform

    tech = _post(
        title="Новая AI-модель помогает разработчикам",
        body="Исследование технологий искусственного интеллекта и программирования.",
    )
    lifestyle = _post(
        title="Как организовать семейный отпуск",
        body="Практические советы по отдыху и планированию путешествий.",
        angle="checklist",
    )
    channel = SimpleNamespace(niche="tech")

    tech_score = score_post_for_platform(tech, "x", channel=channel)
    lifestyle_score = score_post_for_platform(lifestyle, "x", channel=channel)

    assert tech_score["eligible"] is True
    assert tech_score["score"] > lifestyle_score["score"]
    assert tech_score["topic"] == "технологии и AI"
    assert lifestyle_score["eligible"] is False


def test_editorial_score_rejects_stale_and_near_duplicate_posts():
    from tgfarm.distribution import score_post_for_platform

    post = _post(
        title="Open source project publishes a new privacy tool",
        body="A practical analysis of open source privacy and Linux security.",
    )
    recent = f"{post.hook}\n{post.body}"
    duplicate = score_post_for_platform(post, "mastodon", recent_texts=[recent])
    stale_post = _post(
        title="Open source privacy tool",
        body="A practical analysis of Linux security and open source software.",
        age_hours=24 * 10,
    )
    stale = score_post_for_platform(stale_post, "mastodon")

    assert duplicate["eligible"] is False
    assert "already" not in duplicate["reason"].lower()
    assert "similar" in duplicate["reason"].lower() or "похож" in duplicate["reason"].lower()
    assert stale["eligible"] is False
    assert "устарел" in stale["reason"]


def test_platform_daily_caps_are_conservative_and_learn_only_from_reliability():
    from tgfarm.distribution import PLATFORM_RULES, effective_daily_cap

    good = [
        SimpleNamespace(platform="vk", status="posted")
        for _ in range(8)
    ]
    bad = [
        SimpleNamespace(platform="vk", status="failed")
        for _ in range(8)
    ]
    assert PLATFORM_RULES["reddit"]["daily"] == 1
    assert effective_daily_cap("vk", good) == 4
    assert effective_daily_cap("vk", bad) == 2
    assert effective_daily_cap("vk", good[:4]) == 3


def test_manual_only_platforms_never_enter_the_automatic_plan():
    from tgfarm.distribution import MANUAL_ONLY, PLATFORM_RULES
    from tgfarm.platforms import enabled_crosspost_platforms

    enabled = set(enabled_crosspost_platforms())
    assert MANUAL_ONLY["linkedin"]
    assert MANUAL_ONLY["tiktok"]
    assert "linkedin" not in enabled
    assert "tiktok" not in enabled
    assert "instagram" not in enabled
    assert "reddit" in PLATFORM_RULES


def test_runtime_mode_is_saved_without_touching_other_env_values(tmp_path, monkeypatch):
    import os

    import tgfarm.distribution as distribution

    env_path = tmp_path / ".env"
    env_path.write_text(
        "OTHER_SETTING=keep-me\nTGFARM_MODE=dry\nTGFARM_MODE=off\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(distribution, "ROOT", tmp_path)
    old_mode = os.environ.get("TGFARM_MODE")

    try:
        assert distribution.set_runtime_mode("live") == "live"
        raw = env_path.read_text(encoding="utf-8")
        assert raw.count("TGFARM_MODE=") == 1
        assert "TGFARM_MODE=live" in raw
        assert "OTHER_SETTING=keep-me" in raw
        assert os.environ["TGFARM_MODE"] == "live"
    finally:
        if old_mode is None:
            os.environ.pop("TGFARM_MODE", None)
        else:
            os.environ["TGFARM_MODE"] = old_mode
        from tgfarm.core.settings import reset_settings_cache

        reset_settings_cache()


def test_primary_platform_cadence_has_global_daily_limits():
    from tgfarm.s7_publish import PRIMARY_PLATFORM_DAILY_CAPS

    assert PRIMARY_PLATFORM_DAILY_CAPS == {"dzen": 2, "vk": 3}


def test_runtime_mode_api_requires_explicit_live_confirmation(client):
    from tgfarm.core.apiauth import HEADER, ensure_token

    response = client.put(
        "/api/runtime/mode",
        json={"mode": "live", "confirm_live": False},
        headers={HEADER: ensure_token()},
    )
    assert response.status_code == 400
    assert "подтвердить" in response.json()["detail"].lower()


def test_distribution_plan_is_exposed_only_to_authenticated_dashboard(client):
    from tgfarm.core.apiauth import HEADER, ensure_token

    assert client.get("/api/distribution/plan").status_code == 401
    response = client.get("/api/distribution/plan", headers={HEADER: ensure_token()})
    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] in {"off", "dry", "live"}
    assert "primary_platforms" in payload
    assert "platforms" in payload
