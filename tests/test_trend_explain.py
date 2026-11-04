"""S2 explainability: `cluster_signals` / `explain_cluster`.

The point of these tests is the contract an operator UI depends on: numbers are
present, a missing cluster is a value and not an exception, and the diagnostic
functions never write to the tables the ranking pass owns.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import delete, select

from tgfarm.db.models import Channel, Cluster, Post
from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.session import session_scope
from tgfarm.db.vector import pack


def _vec(*values: float) -> bytes:
    return pack(np.asarray(values, dtype=np.float32))


def _make_cluster(
    public_id: str,
    *,
    label: str = "",
    hours_ago: float = 1.0,
    status: str = "forming",
    heat: float = 0.5,
) -> int:
    moment = datetime.now(UTC) - timedelta(hours=hours_ago)
    with session_scope() as session:
        cluster = Cluster(
            public_id=public_id,
            label=label,
            niche="tech",
            status=status,
            heat=heat,
            item_count=0,
            created_at=moment,
            last_seen_at=moment,
        )
        session.add(cluster)
        session.flush()
        return int(cluster.id)


def _add_item(
    cluster_id: int,
    *,
    title: str,
    source_id: str = "src_a",
    hours_ago: float = 1.0,
    embedding: bytes | None = None,
    duplicate_of: int | None = None,
) -> int:
    moment = datetime.now(UTC) - timedelta(hours=hours_ago)
    with session_scope() as session:
        row = RawItemRow(
            source_id=source_id,
            external_id=f"{source_id}-{cluster_id}-{hours_ago}-{title[:16]}",
            url=f"https://example.invalid/{cluster_id}/{source_id}/{hours_ago}",
            title=title,
            text=title,
            lang="ru",
            published_at=moment,
            fetched_at=moment,
            niche_hint=["tech"],
            source_weight=0.6,
            embedding=embedding,
            duplicate_of=duplicate_of,
            cluster_id=cluster_id,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def _add_post(public_id: str, channel_id: str, cluster_public_id: str, status: str = "published"):
    with session_scope() as session:
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                cluster_public_id=cluster_public_id,
                hook="Заголовок",
                body="Текст поста",
                status=status,
                published_at=datetime.now(UTC) if status == "published" else None,
            )
        )


@pytest.fixture()
def clean_db():
    """Wipe only what these tests own: items/clusters plus prefixed posts and channels.

    Channels can be seeded by `ensure_bootstrapped`, so deleting all of them would
    leak into other test modules that share the session-scoped database.
    """

    def wipe() -> None:
        with session_scope() as session:
            session.execute(delete(Post).where(Post.public_id.like("explain-%")))
            session.query(RawItemRow).delete()
            session.query(Cluster).delete()
            session.execute(delete(Channel).where(Channel.id.like("explain-%")))

    wipe()
    yield
    wipe()


# --------------------------------------------------------------------- missing
def test_missing_cluster_is_a_value_not_an_exception(clean_db):
    """Empty DB: both entry points answer with zeros instead of raising."""
    from tgfarm import s2_trends

    for ref in ("no-such-cluster", 999999):
        signals = s2_trends.cluster_signals(ref)
        assert signals["ok"] is False
        assert signals["reason"] == "not_found"
        assert signals["items"]["total"] == 0
        assert signals["saturation"]["published_posts"] == 0
        assert signals["recurrence"]["method"] == "none"
        assert signals["priority"] == 0.0

    explanation = s2_trends.explain_cluster("no-such-cluster")
    assert explanation["ok"] is False
    assert explanation["reason"] == "not_found"
    assert re.search(r"\d", explanation["summary"])
    assert re.search(r"\d", explanation["reasons"][0]["text"])


# -------------------------------------------------------------------- velocity
def test_growing_cluster_has_positive_velocity(clean_db):
    from tgfarm import s2_trends

    cluster_id = _make_cluster("growing", label="OpenAI выпустила GPT-5.5", hours_ago=8)
    # Four elements over 7.5 hours: 4 / 7.5 = 0.53 elements per hour.
    for index, (hours_ago, source) in enumerate(
        [(8.0, "a"), (5.0, "b"), (2.0, "c"), (0.5, "d")]
    ):
        _add_item(
            cluster_id,
            title=f"OpenAI выпустила GPT-5.5, детали {index}",
            source_id=f"src_{source}",
            hours_ago=hours_ago,
        )

    signals = s2_trends.cluster_signals("growing")
    assert signals["ok"] is True
    assert signals["items"]["total"] == 4
    assert signals["items"]["unique"] == 4
    assert signals["items"]["sources"] == 4
    assert signals["window"]["span_hours"] == pytest.approx(7.5, abs=0.05)
    assert signals["velocity"]["items_per_hour"] > 0
    assert signals["velocity"]["items_per_hour"] == pytest.approx(0.53, abs=0.01)
    # 5.0h, 2.0h and 0.5h are all inside the 6h recent window.
    assert signals["velocity"]["recent_items"] == 3

    reasons = s2_trends.explain_cluster("growing")["reasons"]
    reason = next(item for item in reasons if item["code"] == "velocity")
    assert "эл./ч" in reason["text"]
    assert re.search(r"\d", reason["text"])


# ------------------------------------------------------------------- freshness
def test_old_cluster_has_large_freshness_and_lower_priority(clean_db):
    from tgfarm import s2_trends
    from tgfarm.s2_trends.explain import STALE_AFTER_HOURS

    fresh_id = _make_cluster("fresh_story", label="Свежая новость", hours_ago=0.2)
    old_id = _make_cluster("old_story", label="Старая новость", hours_ago=30)
    _add_item(fresh_id, title="Свежая новость", hours_ago=0.2)
    _add_item(old_id, title="Старая новость", hours_ago=30)

    fresh = s2_trends.cluster_signals("fresh_story")
    old = s2_trends.cluster_signals("old_story")

    assert old["freshness"]["hours_since_last"] == pytest.approx(30.0, abs=0.1)
    assert old["freshness"]["hours_since_last"] > STALE_AFTER_HOURS
    assert old["freshness"]["stale"] is True
    assert old["freshness"]["decay"] < fresh["freshness"]["decay"]
    assert old["priority"] < fresh["priority"]
    assert old["priority_band"] == "low"

    explanation = s2_trends.explain_cluster("old_story")
    freshness_reason = next(item for item in explanation["reasons"] if item["code"] == "freshness")
    assert "30.0 ч" in freshness_reason["text"]

    # `now` is injectable and a naive value is read as UTC (SQLite returns naive rows).
    shifted = s2_trends.cluster_signals(
        "old_story", now=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=6)
    )
    assert shifted["freshness"]["hours_since_last"] == pytest.approx(36.0, abs=0.1)


# ------------------------------------------------------------------ saturation
def test_saturation_counts_published_posts_and_channels(clean_db):
    from tgfarm import s2_trends

    cluster_id = _make_cluster("covered", label="Тема уже выходила", heat=0.5)
    _add_item(cluster_id, title="Тема уже выходила", hours_ago=2)
    with session_scope() as session:
        session.add(Channel(id="explain-a", title="Канал A", niche="tech"))
        session.add(Channel(id="explain-b", title="Канал B", niche="tech"))

    _add_post("explain-post-1", "explain-a", "covered")
    _add_post("explain-post-2", "explain-b", "covered")
    _add_post("explain-post-3", "explain-a", "covered", status="draft")

    signals = s2_trends.cluster_signals(cluster_id)
    saturation = signals["saturation"]
    assert saturation["published_posts"] == 2
    assert saturation["posts_total"] == 3
    assert saturation["channels"] == 2
    assert saturation["level"] == "saturated"
    assert saturation["hours_since_last_published"] is not None

    # A read-only diagnostic must not touch the ranking columns.
    with session_scope() as session:
        row = session.execute(select(Cluster).where(Cluster.public_id == "covered")).scalar_one()
        assert row.heat == 0.5
        assert row.status == "forming"

    reason = next(
        item for item in s2_trends.explain_cluster("covered")["reasons"] if item["code"] == "saturation"
    )
    assert "2" in reason["text"]


# ---------------------------------------------------------------- explanation
def test_explanation_has_two_to_four_reasons_with_numbers(clean_db):
    from tgfarm import s2_trends

    cluster_id = _make_cluster("explained", label="ЦБ поднял ключевую ставку до 19 процентов")
    for index, hours_ago in enumerate([6.0, 4.0, 2.0, 0.5]):
        _add_item(
            cluster_id,
            title=f"ЦБ поднял ключевую ставку до 19 процентов, {index}",
            source_id=f"src_{index}",
            hours_ago=hours_ago,
        )

    explanation = s2_trends.explain_cluster("explained")
    assert explanation["ok"] is True
    assert explanation["affects_ranking"] is False
    assert 2 <= len(explanation["reasons"]) <= 4
    assert re.search(r"\d", explanation["summary"])
    for reason in explanation["reasons"]:
        assert re.search(r"\d", reason["text"]), reason
        assert reason["unit"]
    codes = {reason["code"] for reason in explanation["reasons"]}
    assert {"freshness", "saturation"} <= codes


# ----------------------------------------------------------------- recurrence
def test_recurrence_falls_back_to_titles_without_embeddings(clean_db):
    from tgfarm import s2_trends

    target_id = _make_cluster("repeat_now", label="OpenAI выпустила GPT-5.5 с ценой ниже")
    _add_item(target_id, title="OpenAI выпустила GPT-5.5 с ценой ниже", hours_ago=1)

    earlier_id = _make_cluster(
        "repeat_before", label="OpenAI выпустила GPT-5.5 с ценой ниже", hours_ago=48
    )
    _add_item(earlier_id, title="OpenAI выпустила GPT-5.5 с ценой ниже", hours_ago=48)

    unrelated_id = _make_cluster("soup", label="Рецепт борща на зиму", hours_ago=24)
    _add_item(unrelated_id, title="Рецепт борща на зиму", hours_ago=24)

    signals = s2_trends.cluster_signals("repeat_now")
    recurrence = signals["recurrence"]
    assert recurrence["method"] == "title"
    assert recurrence["fallback"] is True
    assert "заголовк" in recurrence["note"]
    assert recurrence["count"] == 1
    assert recurrence["similar"][0]["public_id"] == "repeat_before"
    assert recurrence["similar"][0]["method"] == "title"


def test_recurrence_uses_embeddings_when_present(clean_db):
    from tgfarm import s2_trends

    target_id = _make_cluster("embed_now", label="Тема A")
    _add_item(target_id, title="Тема A", hours_ago=1, embedding=_vec(1, 0, 0, 0))

    same_id = _make_cluster("embed_same", label="Тема A пересказ", hours_ago=24)
    _add_item(same_id, title="Тема A пересказ", hours_ago=24, embedding=_vec(0.9, 0.1, 0, 0))

    different_id = _make_cluster("embed_other", label="Совсем другой сюжет", hours_ago=12)
    _add_item(different_id, title="Совсем другой сюжет", hours_ago=12, embedding=_vec(0, 1, 0, 0))

    signals = s2_trends.cluster_signals("embed_now")
    recurrence = signals["recurrence"]
    assert recurrence["method"] == "embedding"
    assert recurrence["fallback"] is False
    assert recurrence["count"] == 1
    assert recurrence["similar"][0]["public_id"] == "embed_same"
    assert recurrence["similar"][0]["similarity"] > 0.9
