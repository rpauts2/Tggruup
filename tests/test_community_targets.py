from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from tgfarm import community_targets
from tgfarm.db.models import CommunityPolicySnapshot as Snapshot
from tgfarm.db.session import session_scope


@pytest.fixture(autouse=True)
def _drop_snapshots_this_file_creates():
    """`recommend_community_target` records rules history; this file left it behind.

    `gate_plan(..., record=True)` writes a `community_policy_snapshots` row for the
    destination each selector test names (`pikabu/python`, `vc_ru/business`), and
    the session database is shared. Until `vc_ru` had a rules reader an unwatched
    leftover was only untidy; now the maintenance pass that some other file runs
    *unscoped* treats a stored vc.ru topic as a destination to re-read, and a fake
    page is not queued for it — so the suite would try the real network. Cleaned by
    an id waterline rather than by name: every row above the line is one this file
    just wrote, whatever slug its tests invent next.
    """
    with session_scope() as session:
        waterline = int(
            session.execute(select(func.max(Snapshot.id))).scalar_one() or 0
        ) + 1
    yield
    with session_scope() as session:
        session.execute(delete(Snapshot).where(Snapshot.id >= waterline))


@pytest.fixture()
def client():
    from tgfarm.api import create_app

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as test_client:
        yield test_client


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
                    ("/tag/programming", "Programming"),
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
    assert result["auto_publish_supported"] is True


def test_target_editorial_brief_is_evidence_backed_and_never_authorizes_publication():
    brief = community_targets._target_editorial_brief(
        "pikabu",
        _post(),
        {
            "name": "Python",
            "score": 76,
            "matches": ["python", "programming"],
            "suggested_tags": ["Python"],
            "rules": "Публикуйте только материалы по Python. Не размещайте рекламу.",
            "risks": ["Проверь внешние ссылки."],
            "blockers": [],
            "recommended": True,
        },
    )
    assert brief["role"] == "community_editor"
    assert brief["destination"] == "Python"
    assert brief["topic_fit"] == 76
    assert brief["rules_read"] is True
    assert "Публикуйте только материалы по Python" in brief["constraints"]
    assert brief["decision"] == "eligible_for_automatic_pipeline"
    assert brief["publication_authorized"] is False

    blocked = community_targets._target_editorial_brief(
        "vc_ru", _post(), {"name": "Офтоп", "score": 12, "rules": "",
        "risks": [], "blockers": ["Правила не прочитаны."], "recommended": False}
    )
    assert blocked["decision"] == "hold"
    assert blocked["publication_authorized"] is False
    assert blocked["blockers"] == ["Правила не прочитаны."]


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
        "Правила сообщества: публикуйте оригинальные материалы.",
        post,
        "Общий Кодекс Пикабу: реклама и самопродвижение запрещены.",
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

    assert result["selected"] is None
    assert result["candidates"][0]["recommended"] is False
    assert result["candidates"][0]["topic_rules_available"] is False
    assert result["candidates"][0]["global_rules_available"] is True
    assert any("правила выбранной темы" in item.lower() for item in result["candidates"][0]["blockers"])
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


def test_pikabu_requires_global_code_and_community_rules(monkeypatch):
    async def fake_fetch(url: str):
        if url == community_targets.PIKABU_DIRECTORY:
            return _page(url, links=[("/community/python", "Python")])
        if url == community_targets.PIKABU_RULES:
            return _page(url, text="")
        return _page(
            url,
            title="Сообщество Python",
            description="Python programming, developer tools and automation.",
            chunks=["О сообществе", "Python development.", "Правила сообщества", "Публикуйте материалы о Python."],
        )

    monkeypatch.setattr(community_targets, "_fetch", fake_fetch)
    monkeypatch.setattr("tgfarm.community_drafts._load_post", lambda _post_id: _post())

    result = asyncio.run(
        community_targets.recommend_community_target("pikabu", "published_community_test")
    )

    assert result["selected"] is None
    assert result["candidates"][0]["recommended"] is False
    assert any("общие правила" in blocker.lower() for blocker in result["candidates"][0]["blockers"])


def test_vc_requires_global_rules_before_recommending_topic(monkeypatch):
    async def fake_fetch(url: str):
        if url == community_targets.VC_DIRECTORY:
            return _page(url, links=[("/marketing", "Маркетинг")])
        if url == community_targets.VC_RULES:
            return _page(url, text="")
        return _page(
            url,
            title="Маркетинг",
            description="Маркетинг цифровых продуктов, рекламные кампании и продвижение.",
        )

    post = {
        "public_id": "published_vc_rules_missing",
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
        community_targets.recommend_community_target("vc_ru", "published_vc_rules_missing")
    )

    assert result["selected"] is None
    assert result["candidates"][0]["recommended"] is False
    assert any("общие правила vc.ru" in risk.lower() for risk in result["candidates"][0]["risks"])


def test_prepare_api_revalidates_selected_community_before_opening_editor(client, monkeypatch):
    from tgfarm import community_drafts, community_targets
    from tgfarm.core.apiauth import HEADER, ensure_token

    async def stale_recommendation(_platform, _post_id):
        return {
            "selected": None,
            "candidates": [
                {
                    "name": "Python",
                    "url": "https://pikabu.ru/community/python",
                    "recommended": False,
                }
            ],
        }

    async def never_prepare(*_args, **_kwargs):
        pytest.fail("browser draft must not open for a target that failed fresh rule checks")

    monkeypatch.setattr(community_targets, "recommend_community_target", stale_recommendation)
    monkeypatch.setattr(community_drafts, "prepare_community_draft", never_prepare)

    response = client.post(
        "/api/platform-drafts/pikabu/published_community_test/prepare"
        "?target_url=https%3A%2F%2Fpikabu.ru%2Fcommunity%2Fpython&target_name=Python",
        json={},
        headers={HEADER: ensure_token()},
    )

    assert response.status_code == 409
    assert "текущую проверку" in response.json()["detail"].lower()


def test_prepare_api_passes_only_freshly_recommended_target_to_editor(client, monkeypatch):
    from tgfarm import community_drafts, community_targets
    from tgfarm.core.apiauth import HEADER, ensure_token

    captured = {}

    async def fresh_recommendation(_platform, _post_id):
        return {
            "selected": {
                "name": "Python",
                "url": "https://pikabu.ru/community/python",
                "recommended": True,
            },
            "candidates": [
                {
                    "name": "Python",
                    "url": "https://pikabu.ru/community/python",
                    "recommended": True,
                }
            ],
        }

    async def fake_prepare(platform, post_id, **kwargs):
        captured.update({"platform": platform, "post_id": post_id, **kwargs})
        return {"ok": True, "status": "prepared", "target_name": kwargs["target_name"]}

    monkeypatch.setattr(community_targets, "recommend_community_target", fresh_recommendation)
    monkeypatch.setattr(community_drafts, "prepare_community_draft", fake_prepare)

    response = client.post(
        "/api/platform-drafts/pikabu/published_community_test/prepare"
        "?target_url=https%3A%2F%2Fpikabu.ru%2Fcommunity%2Fpython&target_name=Python",
        json={},
        headers={HEADER: ensure_token()},
    )

    assert response.status_code == 200
    assert captured == {
        "platform": "pikabu",
        "post_id": "published_community_test",
        "target_url": "https://pikabu.ru/community/python",
        "target_name": "Python",
    }


def test_vc_skips_topic_with_explicit_promotion_ban_and_selects_next_safe_match(monkeypatch):
    async def fake_fetch(url: str):
        if url == community_targets.VC_DIRECTORY:
            return _page(
                url,
                links=[
                    ("/marketing", "Маркетинг"),
                    ("/business", "Бизнес и продукты"),
                ],
            )
        if url == community_targets.VC_RULES:
            return _page(url, text="Общие правила vc.ru: запрещены спам и манипуляции.")
        if url.endswith("/marketing"):
            return _page(
                url,
                title="Маркетинг цифровых продуктов",
                description="Маркетинг цифровых продуктов, рекламные кампании и продвижение.",
                chunks=[
                    "О теме",
                    "Материалы о маркетинге и продвижении.",
                    "Правила темы",
                    "Реклама запрещена. Самореклама запрещена.",
                ],
            )
        if url.endswith("/business"):
            return _page(
                url,
                title="Бизнес и продукты",
                description="Бизнес, цифровые продукты и стратегии развития компаний.",
                chunks=["О теме", "Обсуждения бизнеса и цифровых продуктов.", "Правила темы", "Только практические материалы о бизнесе и продуктах."],
            )
        raise AssertionError(f"Unexpected URL: {url}")

    post = {
        "public_id": "published_vc_promotion_policy",
        "title": "Маркетинг цифровых продуктов",
        "body": "Купите наш продукт со скидкой. Используйте промокод для заказа. Бизнес и продукты требуют оценки стратегии роста.",
        "cta": "",
        "sources": [],
        "hashtags": [],
        "media": [],
    }
    monkeypatch.setattr(community_targets, "_fetch", fake_fetch)
    monkeypatch.setattr("tgfarm.community_drafts._load_post", lambda _post_id: post)

    result = asyncio.run(
        community_targets.recommend_community_target("vc_ru", "published_vc_promotion_policy")
    )

    marketing = next(item for item in result["candidates"] if item["slug"] == "marketing")
    assert marketing["recommended"] is False
    assert marketing["topic_rules_available"] is True
    assert marketing["blockers"]
    assert result["selected"]["slug"] == "business"
    assert result["selected"]["recommended"] is True


def test_vc_refuses_to_recommend_any_topic_without_topic_specific_rules(monkeypatch):
    async def fake_fetch(url: str):
        if url == community_targets.VC_DIRECTORY:
            return _page(url, links=[("/technology", "Технологии")])
        if url == community_targets.VC_RULES:
            return _page(url, text="Общие правила vc.ru запрещают спам.")
        return _page(
            url,
            title="Технологии",
            description="Технологии, программирование и искусственный интеллект.",
            chunks=["О теме", "Обсуждение технологий и программирования."],
        )

    post = {
        "public_id": "published_vc_missing_topic_rules",
        "title": "Технологии искусственного интеллекта",
        "body": "Практический разбор технологий и программирования.",
        "cta": "",
        "sources": [],
        "hashtags": [],
        "media": [],
    }
    monkeypatch.setattr(community_targets, "_fetch", fake_fetch)
    monkeypatch.setattr("tgfarm.community_drafts._load_post", lambda _post_id: post)

    result = asyncio.run(
        community_targets.recommend_community_target("vc_ru", "published_vc_missing_topic_rules")
    )

    assert result["selected"] is None
    assert result["candidates"]
    assert all(item["recommended"] is False for item in result["candidates"])
    assert all(item["topic_rules_available"] is False for item in result["candidates"])
    assert all(item["blockers"] for item in result["candidates"])


def test_automatic_pikabu_selector_skips_targets_without_two_existing_tags(monkeypatch):
    async def fake_recommend(_platform, _post_id):
        return {"candidates": [
            {"name": "One tag", "recommended": True, "rules_available": True, "suggested_tags": ["Python"]},
            {"name": "Blocked", "recommended": False, "rules_available": True, "suggested_tags": ["Python", "Programming"]},
            {"name": "Two tags", "recommended": True, "rules_available": True, "suggested_tags": ["Python", "Programming"]},
        ]}

    monkeypatch.setattr(community_targets, "recommend_community_target", fake_recommend)
    result = asyncio.run(community_targets.recommend_automatic_pikabu_target("published_community_test"))
    assert result["ok"] is True
    assert result["selected"]["name"] == "Two tags"
    assert result["tags"] == ["Python", "Programming"]
