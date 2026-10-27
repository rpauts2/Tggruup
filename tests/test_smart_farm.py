"""Smart-farm mechanics: model rotation, few-shot examples, approval mode."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace


def _make_post(session, public_id: str, channel_id: str, quality: dict, hook: str = "Хук"):
    from tgfarm.db.models import Post

    post = Post(
        public_id=public_id,
        channel_id=channel_id,
        hook=hook,
        body="Тело опубликованного поста, достаточно длинное для примера.",
        status="published",
        published_at=datetime.now(UTC),
        quality=quality,
    )
    session.add(post)
    session.flush()
    return post


def test_split_models_parses_the_chain():
    from tgfarm.llm.client import _split_models

    assert _split_models("a, b ,c,") == ["a", "b", "c"]
    assert _split_models("") == [""]
    assert _split_models("one") == ["one"]


def test_openai_provider_keeps_the_whole_candidate_list():
    from tgfarm.llm.client import _OpenAI

    provider = _OpenAI("key", "m1, m2,m3", 5.0)
    assert provider._models == ["m1", "m2", "m3"]
    assert provider._model == "m1"


def test_llm_client_builds_rotation_from_settings(monkeypatch):
    import os

    from tgfarm.core import settings as settings_mod
    from tgfarm.llm import client as client_mod

    monkeypatch.setenv("LLM_PROVIDERS", "compatible")
    monkeypatch.setenv("LLM_MODEL", "free-a:free,free-b:free")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "k")
    settings_mod.reset_settings_cache()
    client_mod.reset_llm()
    try:
        llm = client_mod.get_llm()
        provider = llm._providers[0]
        assert provider._models == ["free-a:free", "free-b:free"]
    finally:
        client_mod.reset_llm()
        settings_mod.reset_settings_cache()
        os.environ.pop("LLM_PROVIDERS", None)
        os.environ.pop("LLM_MODEL", None)
        os.environ.pop("LLM_BASE_URL", None)
        os.environ.pop("LLM_API_KEY", None)
        settings_mod.reset_settings_cache()
        client_mod.reset_llm()


def test_best_examples_rank_by_quality_mean(db):
    from tgfarm.db.models import Channel
    from tgfarm.s4_generation import _best_examples

    db.merge(Channel(id="few_shot_chan", title="Few shot", niche="general", platform="vk"))
    _make_post(db, "ex-low", "few_shot_chan", {"scores": {"tone": 0.6}}, hook="Слабый")
    _make_post(db, "ex-high", "few_shot_chan", {"scores": {"tone": 0.95}}, hook="Сильный")
    db.commit()

    examples = _best_examples("few_shot_chan", limit=2)
    assert examples, "опубликованные посты с оценками должны найтись"
    assert examples[0][0] == "Сильный"
    assert examples[1][0] == "Слабый"
    assert _best_examples("empty_channel_x", limit=2) == []


def test_examples_block_forbids_copying_content():
    from tgfarm.s4_generation import _examples_block

    block = _examples_block([("Хук поста.", "Тело поста.")])
    assert "ПРИМЕРЫ ЛУЧШИХ ПОСТОВ" in block
    assert "Хук поста." in block
    assert "только из блока ИСТОЧНИКИ" in block
    assert _examples_block([]) == ""


def test_approval_mode_holds_posts_in_gated(db, monkeypatch):
    from tgfarm.db.models import Post
    from tgfarm.db.session import session_scope
    from tgfarm.s7_publish import schedule_ready_posts

    monkeypatch.setattr(
        "tgfarm.s7_publish.get_settings",
        lambda: SimpleNamespace(approval_required=True),
    )

    with session_scope() as session:
        post = Post(
            public_id="approval-1",
            channel_id="zen_misc_ru",
            hook="Хук одобрения.",
            body="Тело, прошедшее гейты.",
            status="gated",
        )
        session.add(post)

    assert schedule_ready_posts() == 0

    with session_scope() as session:
        row = session.get(Post, post.id)
        assert row.status == "gated", "в режиме одобрения пост не должен планироваться"

# ------------------------------------------------------------- critic rubric
def test_tone_rubric_covers_humor_templates_and_facts():
    from tgfarm.s5_quality import TONE_SYSTEM

    assert "ЮМОР" in TONE_SYSTEM and "НЕ в заголовке" in TONE_SYSTEM
    assert "ШАБЛОННОСТЬ" in TONE_SYSTEM
    assert "ФАКТЫ" in TONE_SYSTEM
    assert "КЛИКБЕЙТ" in TONE_SYSTEM


def test_critic_model_override(monkeypatch):
    import tgfarm.s5_quality as s5

    monkeypatch.setattr(s5, "get_settings", lambda: SimpleNamespace(llm_critic_model="critic-x"))
    assert s5._critic_model() == "critic-x"
    monkeypatch.setattr(s5, "get_settings", lambda: SimpleNamespace(llm_critic_model=""))
    assert s5._critic_model() is None


# ------------------------------------------------------------------- alerts
def test_collect_alerts_flags_quiet_farm(db, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from tgfarm import pipeline
    from tgfarm.db.models import Post

    monkeypatch.setattr(
        "tgfarm.pipeline.get_settings",
        lambda: SimpleNamespace(
            live=True,
            alert_no_publish_hours=3,
            cycle_timeout_min=60,
            cycle_stale_min=45,
        ),
    )
    now = datetime.now(UTC)
    # Тестовая БД общая: гасим чужие свежие публикации, чтобы окно тишины
    # считалось только от того, что вставили мы.
    db.query(Post).filter(Post.status == "published").update(
        {"published_at": now - timedelta(days=30)}, synchronize_session=False
    )
    db.add(Post(public_id="alert-old", channel_id="zen_misc_ru", hook="хук", body="тело",
                status="published", published_at=now - timedelta(hours=6)))
    db.add(Post(public_id="alert-gated", channel_id="zen_misc_ru", hook="хук", body="тело",
                status="gated"))
    db.commit()

    codes = [a["code"] for a in pipeline.collect_alerts(now)]
    assert "no_publications" in codes

    db.add(Post(public_id="alert-fresh", channel_id="zen_misc_ru", hook="хук", body="тело",
                status="published", published_at=now))
    db.commit()
    codes = [a["code"] for a in pipeline.collect_alerts(now)]
    assert "no_publications" not in codes
    db.rollback()


def test_collect_alerts_flags_stuck_publishing(db, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from tgfarm import pipeline
    from tgfarm.db.models import Post

    monkeypatch.setattr(
        "tgfarm.pipeline.get_settings",
        lambda: SimpleNamespace(
            live=False,
            alert_no_publish_hours=3,
            cycle_timeout_min=60,
            cycle_stale_min=45,
        ),
    )
    db.add(Post(public_id="stuck-alert", channel_id="zen_misc_ru", hook="хук", body="тело",
                status="publishing",
                scheduled_at=datetime.now(UTC) - timedelta(hours=2)))
    db.commit()

    codes = [a["code"] for a in pipeline.collect_alerts()]
    assert "publishing_stuck" in codes


# -------------------------------------------------------------- maintenance
def test_maintenance_expires_stale_clusters(db):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from tgfarm.db.models import Cluster
    from tgfarm.maintenance import expire_stale_clusters

    db.merge(Cluster(public_id="cl-old", label="старый", status="forming", heat=0.1,
                     last_seen_at=datetime.now(UTC) - timedelta(hours=100)))
    db.merge(Cluster(public_id="cl-new", label="свежий", status="forming", heat=0.1,
                     last_seen_at=datetime.now(UTC)))
    db.commit()

    assert expire_stale_clusters(hours=72) >= 1
    statuses = dict(db.execute(select(Cluster.public_id, Cluster.status)).all())
    assert statuses["cl-old"] == "stale"
    assert statuses["cl-new"] == "forming"


def test_maintenance_prunes_old_raw_items(db):
    from datetime import UTC, datetime, timedelta

    from tgfarm.db.models import RawItem
    from tgfarm.maintenance import prune_raw_items

    now = datetime.now(UTC)
    old = RawItem(source_id="src", external_id="old-item", url="https://example.invalid/old",
                  title="Старый материал", text="текст", fetched_at=now - timedelta(days=120))
    db.add(old)
    db.flush()
    child = RawItem(source_id="src", external_id="child-item", url="https://example.invalid/child",
                    title="Дубль", text="текст", fetched_at=now, duplicate_of=old.id)
    fresh = RawItem(source_id="src", external_id="fresh-item", url="https://example.invalid/fresh",
                    title="Свежий материал", text="текст", fetched_at=now)
    db.add_all([child, fresh])
    db.commit()

    assert prune_raw_items(days=90) >= 1

    remaining = {r.external_id for r in db.query(RawItem).all()}
    assert "old-item" not in remaining
    assert "fresh-item" in remaining and "child-item" in remaining
