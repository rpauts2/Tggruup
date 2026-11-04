"""Dzen metrics must be matchable even when the publisher never learned the URL.

Live finding (2026-11-04): every Dzen crosspost row has ``remote_id=None`` and
``url=None`` — the browser publisher confirms "published" without a per-article
URL — so the Studio collector fetched four articles and matched none of them.
The headline is the only stable link left, and it must be used strictly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from tgfarm.db.models import Channel, CrosspostLog, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s8_analytics.dzen_web import (
    DzenPublicationMetrics,
    _normalise_title,
    _title_matches,
    record_publication_metrics,
)


def _seed(hook: str, *, remote_id: str | None = None, url: str | None = None) -> str:
    suffix = uuid4().hex[:8]
    channel_id = f"dz_{suffix}"
    public_id = f"dzstat{suffix}"
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Дзен", niche="tech", platform="dzen"))
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                status="published",
                published_at=datetime.now(UTC),
                hook=hook,
                body="Текст",
            )
        )
        session.add(
            CrosspostLog(
                post_public_id=public_id,
                platform="dzen",
                status="posted",
                remote_id=remote_id,
                url=url,
                created_at=datetime.now(UTC),
            )
        )
    return public_id


def _metrics(title: str, *, url: str = "https://dzen.ru/a/auTestOnly") -> DzenPublicationMetrics:
    return DzenPublicationMetrics(
        url=url,
        remote_id=None,
        title=title,
        impressions=1000,
        opens=120,
        views=60,
        reactions=7,
        comments=2,
        forwards=1,
    )


def test_title_normalisation_and_prefix_rule():
    assert _normalise_title("«Заголовок» — с пунктуацией!") == "заголовок с пунктуацией"
    long_title = "Очень длинный заголовок статьи про технологии и людей сегодня"
    assert _title_matches(long_title, long_title[:45])
    assert not _title_matches("короткий", "короткий заголовок другой статьи")


def test_title_only_publication_is_matched_and_recorded():
    hook = "Новый маршрутизатор для дома: сравнение двух моделей"
    public_id = _seed(hook)
    result = record_publication_metrics([_metrics(hook)])

    assert result["saved"] == 1
    assert result["unmatched"] == 0
    with session_scope() as session:
        rows = session.execute(
            select(PostStat)
            .join(Post, Post.id == PostStat.post_id)
            .where(Post.public_id == public_id)
        ).scalars().all()
    assert len(rows) == 1
    assert (rows[0].impressions, rows[0].opens, rows[0].views) == (1000, 120, 60)


def test_truncated_studio_title_still_matches():
    hook = "Как выбрать роутер в 2026 году и не переплатить за лишние функции"
    _seed(hook)
    truncated = hook[:48] + "…"
    result = record_publication_metrics([_metrics(truncated)])
    assert result["saved"] == 1


def test_ambiguous_titles_are_not_attributed():
    hook = "Одинаковый заголовок для проверки неоднозначности"
    _seed(hook)
    _seed(hook)
    result = record_publication_metrics([_metrics(hook)])
    assert result["saved"] == 0
    assert result["unmatched"] == 1


def test_remote_id_still_wins_over_title():
    hook = "Заголовок с известным идентификатором"
    public_id = _seed(hook, remote_id="auKnownId123")
    result = record_publication_metrics(
        [_metrics("Совсем другой заголовок", url="https://dzen.ru/a/auKnownId123")]
    )
    assert result["saved"] == 1
    with session_scope() as session:
        count = session.execute(
            select(PostStat)
            .join(Post, Post.id == PostStat.post_id)
            .where(Post.public_id == public_id)
        ).scalars().all()
    assert len(count) == 1
