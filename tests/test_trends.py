"""S2 trend detection: dedup, clustering, heat scoring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tgfarm.core.text import simhash
from tgfarm.db.models import Cluster
from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.session import session_scope


def _insert(title: str, text: str, source_id: str, weight: float, minutes_ago: int, niche="tech"):
    from tgfarm.db.vector import pack
    from tgfarm.llm.embedding import get_embedder

    embedder = get_embedder()
    vec = embedder.embed(f"{title}\n{text}")
    with session_scope() as session:
        row = RawItemRow(
            source_id=source_id,
            external_id=f"{source_id}-{title[:24]}-{minutes_ago}",
            url=f"https://example.invalid/{source_id}/{abs(hash(title))}/{minutes_ago}",
            title=title,
            text=text,
            lang="ru",
            published_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
            niche_hint=[niche],
            source_weight=weight,
            embedding=pack(vec),
            simhash=simhash(title),
        )
        session.add(row)
        session.flush()
        return row.id


@pytest.fixture()
def clean_items():
    with session_scope() as session:
        session.query(RawItemRow).delete()
        session.query(Cluster).delete()
    yield
    with session_scope() as session:
        session.query(RawItemRow).delete()
        session.query(Cluster).delete()


def test_dedup_flags_identical_reports(clean_items):
    from tgfarm import s2_trends

    title = "ЦБ поднял ключевую ставку до 19 процентов"
    _insert(title, "Ставка выросла.", "outlet_a", 0.6, 5)
    _insert(title + " ", "Ставка выросла.", "outlet_b", 0.6, 6)
    _insert(title, "Ставка выросла.", "outlet_c", 0.6, 7)

    assert s2_trends._dedup_pass() >= 1


def test_dedup_leaves_distinct_stories(clean_items):
    from tgfarm import s2_trends

    _insert("Ethereum перешёл на L2", "Комиссия снизилась.", "a", 0.6, 5)
    _insert("Рецепт борща на зиму", "Долго варить капусту.", "b", 0.6, 5)
    assert s2_trends._dedup_pass() == 0


def test_clustering_groups_same_story(clean_items):
    from tgfarm import s2_trends

    title = "OpenAI выпустила GPT-5.5"
    body = "Новая модель выполняет 40% задач инженеров без участия человека."
    _insert(title, body, "techcrunch", 0.7, 3)
    _insert(title + " — разбор", body, "verge", 0.7, 6)
    _insert(title + ": цифры", body, "gnews", 0.6, 9)

    s2_trends._dedup_pass()
    s2_trends._cluster_pass()

    with session_scope() as session:
        clusters = session.execute(select(Cluster)).scalars().all()
        assert len(clusters) == 1
        members = session.execute(
            select(RawItemRow).where(RawItemRow.cluster_id == clusters[0].id)
        ).scalars().all()
        copies = session.execute(
            select(RawItemRow).where(RawItemRow.duplicate_of.is_not(None))
        ).scalars().all()
        # Three outlets carried one body. The reprint is linked to the document it
        # copies instead of joining the story, so the size of a cluster is the
        # number of independent documents and never the number of copies.
        assert len(copies) == 1
        assert copies[0].cluster_id is None
        assert clusters[0].item_count == len(members) == 2


def test_clustering_separates_different_stories(clean_items):
    from tgfarm import s2_trends

    _insert("Ethereum перешёл на L2 по умолчанию", "Комиссия упала.", "a", 0.7, 3)
    _insert("Ethereum перешёл на L2 по умолчанию", "Комиссия упала.", "b", 0.7, 6)
    _insert("ВОЗ обновила рекомендации по активности", "150 минут в неделю.", "c", 0.8, 3, niche="health")
    _insert("ВОЗ обновила рекомендации по активности", "150 минут в неделю.", "d", 0.8, 6, niche="health")

    s2_trends._dedup_pass()
    s2_trends._cluster_pass()

    with session_scope() as session:
        assert session.execute(select(Cluster)).scalars().all().__len__() == 2


def test_niche_assignment(clean_items):
    from tgfarm import s2_trends

    _insert("ВОЗ обновила рекомендации", "150 минут в неделю.", "who_news", 0.8, 3, niche="health")
    _insert("ВОЗ обновила рекомендации", "150 минут в неделю.", "medicalxpress", 0.7, 6, niche="health")
    s2_trends._dedup_pass()
    s2_trends._cluster_pass()
    assert s2_trends.assign_niches() >= 1

    with session_scope() as session:
        cluster = session.execute(select(Cluster)).scalar_one()
        assert cluster.niche == "health"


def test_heat_rewards_multi_source_fresh_stories(clean_items):
    """Four *independent* reports of one story — distinct documents, not reprints.

    Deliberately rewritten for the dedup-survival contract (P27's family): four
    outlets carrying the identical wire text are one document, heat is computed
    over one survivor and the story legitimately stays below the threshold.
    Multi-source heat can only be earned by four separate write-ups.
    """
    from tgfarm import s2_trends

    body = "Новая модель выполняет сорок процентов задач дешевле конкурентов."
    core = "OpenAI выпустила модель GPT-5.5 с ценой подписки 40 процентов ниже конкурентов"
    headlines = [
        core,
        core + " — аналитики ожидали большего до конца квартала",
        core + ": разработчики уже тестируют новые тарифы массово",
        core + ", однако конкуренты готовят ответный релиз",
    ]
    for i, (src, headline) in enumerate(zip(["a", "b", "c", "d"], headlines, strict=True)):
        _insert(headline, body, src, 0.75, i * 2)

    assert s2_trends._dedup_pass() == 0
    assert s2_trends._cluster_pass() == 1
    s2_trends.assign_niches()
    assert s2_trends._score_pass() >= 1
    assert s2_trends.promote_ready() == 1

    with session_scope() as session:
        cluster = session.execute(select(Cluster)).scalar_one()
        assert cluster.heat > 0.62
        assert cluster.item_count == 4
        assert cluster.status == "ready"


def test_heat_stays_below_threshold_for_one_weak_source(clean_items):
    """A single low-authority, stale item must not reach the generation queue."""
    from tgfarm import s2_trends

    _insert(
        "Случайная заметка из низковесящего источника",
        "Короткая заметка без чисел.",
        "weak_source",
        0.15,
        minutes_ago=600,
    )
    s2_trends._cluster_pass()
    s2_trends.assign_niches()
    s2_trends._score_pass()
    assert s2_trends.promote_ready() == 0


def test_promote_ready_respects_threshold(clean_items):
    from tgfarm import s2_trends

    with session_scope() as session:
        session.add(Cluster(public_id="cold", label="x", heat=0.1, status="forming"))
        session.add(Cluster(public_id="warm", label="y", heat=0.9, status="forming"))

    assert s2_trends.promote_ready(0.62) == 1

    with session_scope() as session:
        by_id = {c.public_id: c.status for c in session.execute(select(Cluster)).scalars()}
    assert by_id == {"cold": "forming", "warm": "ready"}


def test_mark_consumed(clean_items):
    from tgfarm import s2_trends

    with session_scope() as session:
        session.add(Cluster(public_id="c1", label="x", heat=0.9, status="ready"))
    s2_trends.mark_consumed("c1")
    with session_scope() as session:
        assert session.execute(select(Cluster)).scalar_one().status == "consumed"