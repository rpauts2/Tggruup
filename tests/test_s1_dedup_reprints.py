"""S1 ingest dedup: the content fingerprint, not only the exact (source, external_id).

`external_id` is `sha1(guid|url)`, so it can only ever recognise the same
document arriving from the same outlet twice. A second outlet that republishes
the wire copy arrives with a different guid, a different URL and a different
source_id — and used to be stored as a fresh event. These tests pin the ingest
decision: recognising a reprint by fingerprint and recording it, while never
merging two articles that merely cover the same subject.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from tgfarm.core.schemas import RawItem
from tgfarm.core.text import simhash
from tgfarm.db.models import Cluster
from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.session import session_scope
from tgfarm.s1_sources import _persist_detailed
from tgfarm.s1_sources.base import ContentFingerprint, is_content_reprint

WIRE_TITLE = "ЦБ поднял ключевую ставку до 19 процентов годовых"
WIRE_TEXT = (
    "Банк России повысил ключевую ставку на 100 базисных пунктов, до 19 процентов годовых. "
    "Регулятор объяснил решение ускорением инфляционных ожиданий и рисками перегрева "
    "спроса; следующее заседание совету назначено на сентябрь."
)
# Same subject, a different article: its own numbers, its own sentences.
SIBLING_TITLE = "ЦБ сохранил ключевую ставку на уровне 19 процентов"
SIBLING_TEXT = (
    "Банк России оставил ключевую ставку без изменений, вопреки ожиданиям аналитиков. "
    "Инфляция замедлилась до 8,4 процента, а регулятор стал обращать внимание на "
    "охлаждение рынка труда и на крепость рубля."
)


def _item(
    source_id: str,
    *,
    title: str,
    text: str,
    external_id: str | None = None,
    url: str | None = None,
    published_at: datetime | None = None,
) -> RawItem:
    return RawItem(
        source_id=source_id,
        external_id=external_id or f"{source_id}-guid",
        url=url or f"https://{source_id}.invalid/story",
        title=title,
        text=text,
        lang="ru",
        published_at=published_at,
        niche_hint=["finance"],
        source_weight=0.6,
    )


def _wipe() -> None:
    with session_scope() as session:
        session.execute(update(RawItemRow).values(duplicate_of=None, cluster_id=None))
        session.query(RawItemRow).delete()
        session.query(Cluster).delete()


@pytest.fixture()
def clean_store():
    _wipe()
    yield
    _wipe()


def _rows() -> list[RawItemRow]:
    with session_scope() as session:
        return list(
            session.execute(select(RawItemRow).order_by(RawItemRow.id)).scalars().all()
        )


# ------------------------------------------------------------- the exact pair
def test_exact_duplicate_from_the_same_outlet_is_dropped(clean_store):
    item = _item("s1dedup_rss", title=WIRE_TITLE, text=WIRE_TEXT, external_id="guid-1")

    written_first, _ = _persist_detailed([item])
    written_second, _ = _persist_detailed([item])

    assert written_first == 1
    assert written_second == 0
    assert len(_rows()) == 1


def test_second_copy_inside_one_batch_does_not_double_insert(clean_store):
    item = _item("s1dedup_rss", title=WIRE_TITLE, text=WIRE_TEXT, external_id="guid-1")
    same_again = _item("s1dedup_rss", title=WIRE_TITLE + "!", text=WIRE_TEXT, external_id="guid-1")

    written, _ = _persist_detailed([item, same_again])

    assert written == 1
    assert len(_rows()) == 1


# ----------------------------------------------------------- near-duplicates
def test_near_duplicate_reprint_from_another_outlet_is_flagged(clean_store):
    original = _item(
        "s1dedup_habr", title=WIRE_TITLE, text=WIRE_TEXT, external_id="guid-habr-1"
    )
    reprint = _item(
        "s1dedup_rbc",
        title="ЦБ поднял ключевую ставку до 19 процентов",
        text=WIRE_TEXT,
        external_id="guid-rbc-9",
        url="https://s1dedup_rbc.invalid/news/9",
    )

    _persist_detailed([original])
    _persist_detailed([reprint])

    rows = _rows()
    assert len(rows) == 2, "a reprint stays as coverage evidence, it is not deleted"
    by_source = {row.source_id: row for row in rows}
    assert by_source["s1dedup_habr"].duplicate_of is None
    assert by_source["s1dedup_rbc"].duplicate_of == by_source["s1dedup_habr"].id
    # and it is not the "new independent event" the old ingest produced
    assert by_source["s1dedup_rbc"].simhash == simhash(
        f"{by_source['s1dedup_rbc'].title} {by_source['s1dedup_rbc'].text[:600]}"
    )


def test_reprint_storm_points_at_one_original(clean_store):
    items = [
        _item(
            f"s1dedup_outlet{i}",
            title=WIRE_TITLE if i else "ЦБ поднял ставку до 19 процентов годовых",
            text=WIRE_TEXT,
            external_id=f"guid-{i}",
            url=f"https://s1dedup-outlet{i}.invalid/{i}",
        )
        for i in range(4)
    ]

    _persist_detailed(items)

    rows = _rows()
    originals = [row for row in rows if row.duplicate_of is None]
    assert len(originals) == 1
    assert all(row.duplicate_of == originals[0].id for row in rows if row is not originals[0])


def test_two_articles_on_the_same_topic_are_not_merged(clean_store):
    first = _item(
        "s1dedup_a", title=WIRE_TITLE, text=WIRE_TEXT, external_id="guid-a", url="https://a.invalid/1"
    )
    second = _item(
        "s1dedup_b",
        title=SIBLING_TITLE,
        text=SIBLING_TEXT,
        external_id="guid-b",
        url="https://b.invalid/2",
    )

    _persist_detailed([first])
    _persist_detailed([second])

    rows = _rows()
    assert len(rows) == 2
    assert all(row.duplicate_of is None for row in rows), (
        "the fingerprint is content similarity; two articles that merely discuss "
        "the same decision are two documents"
    )


def test_fingerprint_requires_both_copy_similarity_and_word_overlap():
    reprint = ContentFingerprint.of(1, WIRE_TITLE, WIRE_TEXT)
    trimmed = ContentFingerprint.of(2, "ЦБ поднял ключевую ставку до 19 процентов", WIRE_TEXT)
    other_article = ContentFingerprint.of(3, SIBLING_TITLE, SIBLING_TEXT)

    assert is_content_reprint(reprint, trimmed) is True
    assert is_content_reprint(reprint, other_article) is False

    # an absent fingerprint proves nothing at all — not even equality with itself
    legacy = ContentFingerprint.of(4, "", "", simhash_value=0)
    assert is_content_reprint(reprint, legacy) is False
    assert is_content_reprint(legacy, legacy) is False


def test_legacy_row_without_simhash_does_not_swallow_a_new_item(clean_store):
    with session_scope() as session:
        session.add(
            RawItemRow(
                source_id="s1dedup_legacy",
                external_id="legacy-1",
                url="https://s1dedup-legacy.invalid/1",
                title=WIRE_TITLE,
                text=WIRE_TEXT,
                lang="ru",
                published_at=datetime.now(UTC) - timedelta(hours=3),
                fetched_at=datetime.now(UTC) - timedelta(hours=3),
                niche_hint=["finance"],
                source_weight=0.6,
                embedding=None,
                simhash=0,
            )
        )

    written, _ = _persist_detailed(
        [_item("s1dedup_new", title=WIRE_TITLE, text=WIRE_TEXT, external_id="new-1")]
    )

    assert written == 1
    fresh = [row for row in _rows() if row.source_id == "s1dedup_new"]
    assert len(fresh) == 1
    assert fresh[0].duplicate_of is None, (
        "a row with no fingerprint is not comparable; treating 0 as a match would "
        "flag every incoming item"
    )


def test_ingest_dedup_window_can_be_narrowed(clean_store):
    original = _item("s1dedup_old", title=WIRE_TITLE, text=WIRE_TEXT, external_id="old-1")
    _persist_detailed([original])
    with session_scope() as session:
        session.execute(
            update(RawItemRow)
            .where(RawItemRow.source_id == "s1dedup_old")
            .values(fetched_at=datetime.now(UTC) - timedelta(hours=30))
        )

    reprint = _item(
        "s1dedup_new2",
        title="ЦБ поднял ключевую ставку до 19 процентов",
        text=WIRE_TEXT,
        external_id="new-2",
    )
    written, _ = _persist_detailed([reprint], dedup_window_hours=6)

    assert written == 1
    assert [r.duplicate_of for r in _rows() if r.source_id == "s1dedup_new2"] == [None]
