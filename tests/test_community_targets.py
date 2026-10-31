from __future__ import annotations

import asyncio

from tgfarm import community_targets


def _post():
    return {
        "public_id": "published_community_test",
        "title": "Python automation for developers",
        "body": "A practical guide to Python programming and developer tools.",
        "cta": "",
        "sources": [],
        "hashtags": ["Python", "Programming"],
        "media": [],
    }


def _page(url: str, *, title: str = "", description: str = "", text: str = "", chunks=None, links=None):
    return {
        "url": url,
        "title": title,
        "description": description,
        "text": text,
        "chunks": list(chunks or []),
        "links": list(links or []),
    }


def test_extract_rules_finds_community_rules_section():
    rules = community_targets._rules(
        [
            "О сообществе",
            "Сообщество Python-разработчиков.",
            "Правила сообщества",
            "Публикуйте только материалы по Python.",
            "Не размещайте рекламные объявления.",
            "Популярные посты",
            "Большая подборка публикаций.",
        ],
        "",
    )
    assert "только материалы по Python" in rules
    assert "Популярные посты" not in rules


def test_pikabu_recommender_uses_actual_community_page_and_rule_text(monkeypatch):
    async def fake_fetch(url: str):
        if url == community_targets.PIKABU_DIRECTORY:
            return _page(
                url,
                links=[
                    ("/community/python", "Python"),
                    ("/community/cooking", "Кулинария"),
                ],
            )
        if url == community_targets.PIKABU_RULES:
            return _page(url, text="Общие правила Пикабу.")
        if url.endswith("/community/python"):
            return _page(
                url,
                title="Сообщество Python",
                description="Материалы и обсуждения программирования на Python.",
                chunks=[
                    "О сообществе",
                    "Посты о Python и разработке.",
                    "Правила сообщества",
                    "Публикуйте только материалы по Python и разработке.",
                ],
                links=[
                    ("/tag/python", "Python"),
                    ("/tag/development", "Разработка"),
                    ("/tag/cooking", "Кулинария"),
                ],
            )
        return _page(
            url,
            title="Кулинария",
            description="Рецепты еды и домашняя кухня.",
            chunks=["О сообществе", "Рецепты еды.", "Правила сообщества", "Только рецепты."],
        )

    monkeypatch.setattr(community_targets, "_fetch", fake_fetch)
    monkeypatch.setattr("tgfarm.community_drafts._load_post", lambda _post_id: _post())

    result = asyncio.run(
        community_targets.recommend_community_target("pikabu", "published_community_test")
    )

    assert result["selected"]["slug"] == "python"
    assert result["selected"]["rules_available"] is True
    assert "Python" in result["selected"]["suggested_tags"]
    assert result["selected"]["recommended"] is True
    assert result["auto_publish_supported"] is False


def test_pikabu_rule_blocker_prevents_a_recommendation_but_warnings_do_not():
    post = _post()
    warnings, blockers = community_targets._pikabu_risks(
        "Правила сообщества: публикуйте только оригинальные материалы.",
        post,
        "Общие правила Пикабу.",
    )
    assert blockers == []

    post_with_link = {**post, "body": post["body"] + " Источник https://example.org/article"}
    warning_only, blockers = community_targets._pikabu_risks(
        "Правила сообщества: публикуйте только оригинальные материалы.",
        post_with_link,
        "Общие правила Пикабу.",
    )
    assert blockers == []
    assert any("внешняя ссылка" in item.lower() for item in warning_only)

    _warnings, blockers = community_targets._pikabu_risks(
        "Правила сообщества: реклама и самопродвижение запрещены.",
        post,
        "Общие правила Пикабу.",
    )
    assert blockers
    assert any("рекламы" in item.lower() or "самопродвижения" in item.lower() for item in blockers)


def test_pikabu_recommender_does_not_choose_a_community_without_readable_rules(monkeypatch):
    async def fake_fetch(url: str):
        if url == community_targets.PIKABU_DIRECTORY:
            return _page(url, links=[("/community/python", "Python")])
        if url == community_targets.PIKABU_RULES:
            return _page(url, text="Общие правила.")
        return _page(url, title="Python", description="Python development.", chunks=["О сообществе", "Python"])

    monkeypatch.setattr(community_targets, "_fetch", fake_fetch)
    monkeypatch.setattr("tgfarm.community_drafts._load_post", lambda _post_id: _post())

    result = asyncio.run(
        community_targets.recommend_community_target("pikabu", "published_community_test")
    )

    assert result["selected"] is None
    assert result["candidates"][0]["recommended"] is False
    assert result["candidates"][0]["blockers"]


def test_vc_recommender_reads_current_topics_and_selects_best_description(monkeypatch):
    async def fake_fetch(url: str):
        if url == community_targets.VC_DIRECTORY:
            return _page(
                url,
                links=[
                    ("/ai", "AI"),
                    ("/marketing", "Маркетинг"),
                    ("/career", "Карьера"),
                ],
            )
        if url == community_targets.VC_RULES:
            return _page(url, text="Правила vc.ru запрещают спам и манипуляции.")
        if url.endswith("/marketing"):
            return _page(url, title="Маркетинг", description="Маркетинг, продвижение и рекламные кампании.")
        if url.endswith("/career"):
            return _page(url, title="Карьера", description="Работа, поиск вакансий и профессиональный рост.")
        return _page(url, title="AI", description="Нейросети, искусственный интеллект, ML и разработка AI-сервисов.")

    post = {
        "public_id": "published_vc_test",
        "title": "Маркетинг цифровых продуктов",
        "body": "Маркетинг цифровых продуктов, продвижение и аудит рекламных кампаний.",
        "cta": "",
        "sources": [],
        "hashtags": [],
        "media": [],
    }
    monkeypatch.setattr(community_targets, "_fetch", fake_fetch)
    monkeypatch.setattr("tgfarm.community_drafts._load_post", lambda _post_id: post)

    result = asyncio.run(
        community_targets.recommend_community_target("vc_ru", "published_vc_test")
    )

    assert result["selected"]["slug"] == "marketing"
    assert result["selected"]["target_type"] == "topic"
    assert result["selected"]["score"] >= community_targets.MIN_SCORE
    assert "личный блог" in " ".join(result["general_notes"]).lower()
    assert result["auto_publish_supported"] is False


def test_target_recommendation_api_requires_auth_and_returns_selected_target(client, monkeypatch):
    from tgfarm.core.apiauth import HEADER, ensure_token

    async def fake_recommend(platform_id, post_public_id):
        return {
            "platform": platform_id,
            "post_public_id": post_public_id,
            "selected": {"name": "Python", "score": 88},
            "candidates": [],
            "auto_publish_supported": False,
        }

    import tgfarm.community_targets as targets

    monkeypatch.setattr(targets, "recommend_community_target", fake_recommend)
    assert client.post(
        "/api/platform-drafts/pikabu/published_123/targets", json={}
    ).status_code == 401

    response = client.post(
        "/api/platform-drafts/pikabu/published_123/targets",
        json={},
        headers={HEADER: ensure_token()},
    )
    assert response.status_code == 200
    assert response.json()["selected"]["name"] == "Python"
