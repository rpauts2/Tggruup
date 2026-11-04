"""The Dzen collector must not lose rows in silence.

Live evidence (2026-11-04/05): the Studio visit parsed four rows and attributed
none of them, while the same channel's own page says it lists eighteen
publications - and 49 of the farm's 57 published Dzen posts have no snapshot at
all. Two mechanisms produced that, and both were invisible:

* ``parse_table_rows`` required thirteen cells and read columns by position, so
  the redesigned table (one selectable metric plus icon columns) was dropped row
  by row with no trace;
* attribution only consulted ``crosspost_logs`` rows marked ``posted``, so a
  published Dzen post whose log row was lost could never be measured.

Every case below is offline: rows are the JSON the DOM evaluate already returns.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from tgfarm.db.models import Channel, CrosspostLog, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s8_analytics.dzen_web import (
    DzenPublicationMetrics,
    DzenScrapeResult,
    map_columns,
    parse_rows,
    parse_table_rows,
    record_publication_metrics,
)

# The redesigned header: one metric family plus icon columns, no "openings".
REDESIGNED_HEADERS = [
    "publication",
    "show",
    "watchTime",
    "subscriber",
    "comment",
    "like",
    "share",
]
LEGACY_HEADERS = [
    "Публикация",
    "Дата публикации",
    "Публикация",
    "Статус",
    "Показы",
    "Открытия",
    "Дочитывания и просмотры",
    "Время просмотра",
    "Подписки",
    "Комментарии",
    "Лайки",
    "Репосты",
]


def row(href: str | None, cells: list[str], title: str = "") -> dict:
    return {"href": href, "title": title, "cells": cells}


def test_column_roles_are_read_from_the_header_not_from_position():
    columns = map_columns(REDESIGNED_HEADERS)
    assert columns == {"impressions": 1, "comments": 4, "reactions": 5, "forwards": 6}
    # "watchTime" and "subscriber" are not views: writing minutes into `views`
    # would invent a read-through rate for every article.
    assert "views" not in columns


def test_russian_labels_map_to_the_same_roles():
    assert map_columns(LEGACY_HEADERS) == {
        "impressions": 4,
        "opens": 5,
        "views": 6,
        "comments": 9,
        "reactions": 10,
        "forwards": 11,
    }


def test_a_narrow_redesigned_row_is_still_measured():
    """Nine cells used to mean "not our table"; now the header decides."""
    headline = "Как перестать платить за лишние подписки в 2026 году"
    rows = [
        row(
            "https://dzen.ru/a/auNarrowRow1?ref=stat",
            ["", headline, "1 240", "3 мин", "2", "5", "1"],
            title=headline,
        )
    ]
    headers = ["", "publication", "show", "watchTime", "comment", "like", "share"]
    parsed, dropped, _listed = parse_rows(rows, headers)
    assert dropped == []
    assert len(parsed) == 1
    assert parsed[0].impressions == 1240
    assert parsed[0].comments == 2
    assert parsed[0].reactions == 5
    assert parsed[0].forwards == 1
    assert parsed[0].remote_id == "auNarrowRow1"
    # The headline must come from the publication column, not from wherever the
    # legacy layout kept it - index 2 here is the impressions counter.
    assert parsed[0].title == headline


def test_unusable_header_falls_back_to_the_legacy_positions():
    legacy = [
        row(
            "https://dzen.ru/a/auLegacy1",
            ["", "", "Заголовок", "Вчера", "1 000", "120", "80", "2 мин", "3", "4", "5", "6", ""],
        )
    ]
    parsed, dropped, _listed = parse_rows(legacy, [])
    assert dropped == []
    assert (parsed[0].impressions, parsed[0].opens, parsed[0].views) == (1000, 120, 80)
    assert (parsed[0].comments, parsed[0].reactions, parsed[0].forwards) == (4, 5, 6)


def test_parse_table_rows_keeps_its_old_shape():
    rows = [
        row(
            "https://dzen.ru/a/auLegacy2",
            ["", "", "Заголовок", "Вчера", "9", "9", "9", "0", "0", "0", "0", "0", ""],
        )
    ]
    assert isinstance(parse_table_rows(rows), list)


def test_every_dropped_row_carries_a_reason():
    headers = ["", "publication", "date", "show", "comment", "like", "share"]
    rows = [
        row(None, ["Всего за 30 дней", "18 публикаций", "0", "0", "0", "0", "0", "0", "0"]),
        row("https://dzen.ru/a/auDup", ["", "Раз", "12 окт", "10", "1", "1", "1"]),
        row("https://dzen.ru/a/auDup", ["", "Два", "12 окт", "10", "1", "1", "1"]),
        row("https://dzen.ru/a/auShort", ["", "Три"]),
    ]
    parsed, dropped, listed = parse_rows(rows, headers)
    reasons = [entry["reason"] for entry in dropped]
    assert reasons == ["no_publication_link", "duplicate_remote_id", "metric_columns_out_of_range"]
    assert listed == 18
    assert len(parsed) == 1


def test_a_header_that_names_no_metric_is_reported_not_guessed():
    rows = [row("https://dzen.ru/a/auUnknownCols", ["", "Заголовок", "12", "5", "3", "1", "1", "0", "0"])]
    parsed, dropped, _listed = parse_rows(rows, ["publication", "date", "rating"])
    assert parsed == []
    assert dropped[0]["reason"] == "no_metric_column_identified"


def test_scrape_report_separates_the_table_from_the_channel():
    """`table_rows` against `listed_publications` is the coverage gap, made visible."""
    scrape = DzenScrapeResult(
        items=[DzenPublicationMetrics(url="https://dzen.ru/a/auOne", remote_id="auOne")],
        dropped=[{"reason": "no_publication_link", "href": ""}] * 3,
        table_rows=4,
        headers=REDESIGNED_HEADERS,
        columns={"impressions": 1},
        listed_publications=18,
        subscribers=1,
    )
    report = scrape.as_report()
    assert report["table_rows"] == 4
    assert report["listed_publications"] == 18
    assert report["drop_reasons"] == {"no_publication_link": 3}
    assert "comments" in report["metrics_not_in_table"]


@pytest.fixture(autouse=True)
def _clear_dzen_rows():
    """Attribution consults the whole database, so each case starts from zero.

    The wipe is undone on teardown: the session-scoped fleet seeded by conftest
    must survive for the suites that run after this file. Children before
    parents - the test schema enforces foreign keys.
    """
    import test_metrics_coverage as _cov

    snapshot = _cov._snapshot_and_wipe(_cov._ANALYTICS_TABLES)
    yield
    _cov._restore(_cov._ANALYTICS_TABLES, snapshot)


def _seed_dzen_post(hook: str, *, with_log: bool = True, remote_id: str | None = None) -> str:
    suffix = uuid4().hex[:8]
    channel_id = f"dzcov_{suffix}"
    public_id = f"dzcov{suffix}"
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Дзен", niche="news", platform="dzen"))
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
        if with_log:
            session.add(
                CrosspostLog(
                    post_public_id=public_id,
                    platform="dzen",
                    status="posted",
                    remote_id=remote_id,
                    url=f"https://dzen.ru/a/{remote_id}" if remote_id else None,
                    created_at=datetime.now(UTC),
                )
            )
    return public_id


def test_a_published_post_without_a_crosspost_row_is_still_attributable():
    """The lost log row is a bookkeeping accident, not a reason to stay blind."""
    hook = "Рынок аренды подешевел впервые за три квартала по данным союза"
    public_id = _seed_dzen_post(hook, with_log=False)
    result = record_publication_metrics(
        [
            DzenPublicationMetrics(
                url="https://dzen.ru/a/auNoLogRow1",
                remote_id="auNoLogRow1",
                title=hook,
                impressions=900,
                opens=80,
                views=40,
            )
        ]
    )
    assert result["saved"] == 1
    assert result["unmatched"] == 0
    with session_scope() as session:
        post_id = session.execute(
            select(Post.id).where(Post.public_id == public_id)
        ).scalar_one()
        rows = session.execute(
            select(PostStat).where(PostStat.post_id == post_id)
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].impressions == 900


def test_an_ambiguous_headline_is_named_as_ambiguity_not_as_absence():
    hook = "Одинаковый заголовок для проверки неоднозначности привязки"
    _seed_dzen_post(hook)
    _seed_dzen_post(hook)
    result = record_publication_metrics(
        [
            DzenPublicationMetrics(
                url="https://dzen.ru/a/auAmbiguous",
                remote_id="auAmbiguous",
                title=hook,
                impressions=10,
            )
        ]
    )
    assert result["saved"] == 0
    assert result["unmatched_samples"][0]["reason"] == "headline_ambiguous"


def test_an_unknown_headline_stays_a_different_reason():
    _seed_dzen_post("Совсем другой заголовок этой публикации ни о чём не говорит")
    result = record_publication_metrics(
        [
            DzenPublicationMetrics(
                url="https://dzen.ru/a/auUnknownTitle",
                remote_id="auUnknownTitle",
                title="Ничего похожего ни на один сохранённый заголовок публикации",
                impressions=10,
            )
        ]
    )
    assert result["unmatched_samples"][0]["reason"] == "headline_not_attributable"
