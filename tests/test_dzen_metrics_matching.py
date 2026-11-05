"""Dzen metrics must be attributed by identifier, and by headline only without one.

Live finding (2026-11-04, re-measured on a read-only copy of the production
database): of 37 `posted` Dzen rows, 29 stored neither `remote_id` nor `url`.
The browser publisher confirms "published" on a page whose article links are
root-relative (`/a/auXXXX`), which the absolute-only extractor could not read,
so the identifier was never learned — never merely dropped. Everything below
therefore pins both halves: the id that now reaches the journal has to win, and
the headline fallback has to keep working for the history that still has none.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from tgfarm.db.models import Channel, CrosspostLog, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s8_analytics.dzen_web import (
    DzenPublicationMetrics,
    _normalise_title,
    _title_matches,
    record_publication_metrics,
)


@pytest.fixture(autouse=True)
def _remove_seeded_rows():
    """Leave no Dzen publications behind for the rest of the session.

    `s8_analytics.formats` measures a platform rather than a channel, so a
    `published` Dzen post with a `posted` journal row that survives this file is
    evidence for the next file's format lesson — with a real number of reactions
    on it, because `record_publication_metrics` writes the stats here. The seeded
    posts carry no explicit format, so they land in `news` — the column default,
    re-asserted by `formats.py:407` — and are therefore evidence for a format the
    next file never seeded.

    The ids are recorded rather than matched by prefix on purpose: `dz_` is also
    what `tests/test_pipeline.py` names its own channels with, and a cleanup that
    sweeps another file's rows turns this file into a source of order-dependent
    failures of its own.
    """
    yield
    if not _seeded_channels:
        return
    with session_scope() as session:
        channel_ids = list(_seeded_channels)
        rows = list(session.scalars(select(Post).where(Post.channel_id.in_(channel_ids))).all())
        post_ids = [row.id for row in rows]
        public_ids = [row.public_id for row in rows]
        if post_ids:
            session.execute(delete(PostStat).where(PostStat.post_id.in_(post_ids)))
        if public_ids:
            session.execute(delete(CrosspostLog).where(CrosspostLog.post_public_id.in_(public_ids)))
        session.execute(delete(Post).where(Post.id.in_(post_ids)))
        session.execute(delete(Channel).where(Channel.id.in_(channel_ids)))
    _seeded_channels.clear()


#: Every channel `_seed` created and the teardown has not removed yet.
_seeded_channels: list[str] = []


def _seed(
    hook: str,
    *,
    remote_id: str | None = None,
    url: str | None = None,
    status: str = "posted",
) -> str:
    suffix = uuid4().hex[:8]
    channel_id = f"dz_{suffix}"
    public_id = f"dzstat{suffix}"
    _seeded_channels.append(channel_id)
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
                status=status,
                remote_id=remote_id,
                url=url,
                created_at=datetime.now(UTC),
            )
        )
    return public_id


def _uid() -> str:
    """A fresh article id: the shared test database must not mix rows up."""
    return f"au{uuid4().hex[:16]}"


def _metrics(
    title: str,
    *,
    url: str | None = None,
    remote_id: str | None = None,
) -> DzenPublicationMetrics:
    """One Studio row as the collector produces it.

    Every row gets its own article URL, because a real Studio table lists one
    article per row; a shared placeholder URL would let the identifier one test
    stored answer a different test's lookup.
    """
    ref = remote_id or _uid()
    return DzenPublicationMetrics(
        url=url or f"https://dzen.ru/a/{ref}",
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


def test_headline_surrounded_by_cell_junk_is_attributed():
    """Studio's publication cell is not only the headline.

    The live table puts the date (and sometimes the author) around the title, so a
    stored hook that appears *inside* the scraped text is the same article. The
    40-character evidence rule and the single-candidate rule both still apply.
    """
    hook = "Пшеничный экспорт вырос втрое за квартал по данным союза"
    public_id = _seed(hook)
    result = record_publication_metrics([_metrics(f"5 нояб. 2026 {hook} Автор: Фирма")])

    assert result["saved"] == 1
    with session_scope() as session:
        rows = session.execute(
            select(PostStat)
            .join(Post, Post.id == PostStat.post_id)
            .where(Post.public_id == public_id)
        ).scalars().all()
    assert len(rows) == 1


def test_an_unmatched_row_says_what_was_actually_scraped():
    """`unmatched: 4` alone cannot tell a wrong column from a missing post."""
    result = record_publication_metrics([_metrics("Ничего похожего на наши посты и вообще пусто")])

    sample = result["unmatched_samples"][0]

    assert result["unmatched"] == 1
    assert sample["reason"] == "headline_not_attributable"
    assert sample["title"].startswith("Ничего похожего")


def _row_ids(public_id: str) -> list[tuple[str | None, str | None, str]]:
    """What the journal actually holds for one post: (status, remote_id, url)."""
    with session_scope() as session:
        rows = session.execute(
            select(CrosspostLog)
            .where(CrosspostLog.post_public_id == public_id, CrosspostLog.platform == "dzen")
            .order_by(CrosspostLog.id)
        ).scalars().all()
    return [(row.status, row.remote_id, row.url) for row in rows]


def test_stored_id_attributes_a_row_whose_title_is_garbage():
    """An exact identifier beats the headline entirely.

    The Studio cell can hold anything — a truncated title, a date, an author, a
    headline the operator edited after publishing. With the id in the journal the
    row is still the post it belongs to, so attribution does not depend on text
    agreement at all.
    """
    public_id = _seed("Заголовок, который в Студии выглядит совсем иначе", remote_id="auExactKey1")
    result = record_publication_metrics(
        [_metrics("1 нояб. 2026 · Автор не тот · …", url="https://dzen.ru/a/auExactKey1")]
    )

    assert result["saved"] == 1
    assert result["matched_by_remote_id"] == 1
    assert result["matched_by_title"] == 0
    with session_scope() as session:
        stat = session.execute(
            select(PostStat).join(Post, Post.id == PostStat.post_id).where(Post.public_id == public_id)
        ).scalars().all()
    assert len(stat) == 1


def test_stored_id_separates_two_posts_with_the_same_title():
    """The case title matching can never solve, and must not pretend to.

    Two posts share a headline verbatim: an id-bearing row is still attributed
    exactly, while the same article scraped without an id stays refused, because
    guessing which of the two it is would put one post's numbers on the other.
    """
    hook = "Одинаковый заголовок двух разных статей фермы про одно и то же"
    with_id = _seed(hook, remote_id="auTwinHolder")
    _seed(hook)

    attributed = record_publication_metrics(
        [_metrics("Совсем другой текст ячейки", url="https://dzen.ru/a/auTwinHolder")]
    )
    assert attributed["saved"] == 1
    with session_scope() as session:
        owner = session.execute(
            select(Post.public_id)
            .join(PostStat, PostStat.post_id == Post.id)
            .where(Post.public_id == with_id)
        ).scalars().all()
    assert owner == [with_id]

    refused = record_publication_metrics([_metrics(hook)])
    assert refused["saved"] == 0
    assert refused["unmatched_samples"][0]["reason"] == "headline_ambiguous"


def test_legacy_title_match_stores_the_id_for_the_next_run():
    """A headline match is a one-time guess, not a permanent condition.

    A row published before the identifier was kept has NULL remote_id. Once the
    headline attributes it unambiguously, the id the Studio table names is
    written onto that same journal row (NULLs only), so the next collection
    matches by identifier and survives a later title edit.
    """
    hook = "Как мы считали тарифы на отопление в три разных способа"
    public_id = _seed(hook)
    item = _metrics(hook)

    first = record_publication_metrics([item])
    assert first["saved"] == 1
    assert first["matched_by_title"] == 1
    assert first["remote_ids_stored"] == 1
    stored = _row_ids(public_id)[0][1]
    assert stored == item.url.rstrip("/").rsplit("/", 1)[-1]

    second = record_publication_metrics([item])
    assert second["unchanged"] == 1
    assert second["matched_by_remote_id"] == 1
    assert second["matched_by_title"] == 0


def test_id_on_an_earlier_attempt_row_still_attributes():
    """The journal is append-only, so the id may sit on a row that is not `posted`.

    Operator reconciliation inserts a new `posted` row copying the id of the row
    it answered — usually the empty `publishing` one. An identifier the browser
    publisher managed to store on any non-dry-run row is still the only true
    record of that article, so reading only `posted` rows would throw it away.
    """
    hook = "Пост, чей идентификатор живёт на строке подтверждения"
    public_id = _seed(hook, remote_id="auOnUnknownRow", status="unknown")
    _seed_log(public_id, "posted")

    result = record_publication_metrics(
        [_metrics("Ячейка совсем не про этот заголовок", url="https://dzen.ru/a/auOnUnknownRow")]
    )
    assert result["saved"] == 1
    assert result["matched_by_remote_id"] == 1
    with session_scope() as session:
        measured = session.execute(
            select(PostStat.id)
            .join(Post, Post.id == PostStat.post_id)
            .where(Post.public_id == public_id)
            .limit(1)
        ).scalar_one_or_none()
    assert measured is not None


def _seed_log(public_id: str, status: str, *, remote_id: str | None = None, url: str | None = None) -> None:
    """One more journal row for an existing post (a retry, a confirmation)."""
    with session_scope() as session:
        session.add(
            CrosspostLog(
                post_public_id=public_id,
                platform="dzen",
                status=status,
                remote_id=remote_id,
                url=url,
                created_at=datetime.now(UTC),
            )
        )


def test_retry_row_does_not_erase_the_stored_id():
    """A second attempt must not cost the post its identifier.

    The retry writes a fresh row with NULL columns; the row that carries the id
    stays, the attribution still lands by identifier, and the id-less retry row
    is filled in rather than left to be guessed forever.
    """
    public_id = _seed("Пост, опубликованный со второй попытки", remote_id="auSurvivor")
    _seed_log(public_id, "posted")

    result = record_publication_metrics(
        [_metrics("Что-то нечитаемое в ячейке Студии", url="https://dzen.ru/a/auSurvivor")]
    )
    assert result["saved"] == 1
    assert result["remote_ids_stored"] == 1

    rows = _row_ids(public_id)
    assert [status for status, _, _ in rows] == ["posted", "posted"]
    assert rows[0][1] == "auSurvivor"
    assert rows[1][1] == "auSurvivor"


def test_dry_run_placeholder_is_not_an_article_id():
    """`dry-<post id>` is a run marker, never a Dzen identifier.

    A dry run leaves a journal row with a fake remote_id; trusting it would let a
    collected article land on a post that was never published at all.
    """
    hook = "Черновик, который не публиковали"
    _seed(hook, remote_id="dry-4242", url="https://dzen.ru/a/dry-4242", status="dry_run")

    result = record_publication_metrics(
        [_metrics("Любая ячейка", url="https://dzen.ru/a/dry-4242", remote_id="dry-4242")]
    )
    assert result["saved"] == 0
    assert result["unmatched_samples"][0]["reason"] in {
        "headline_not_attributable",
        "headline_ambiguous",
    }


def test_short_headline_evidence_is_still_refused():
    """The 40-character rule survives the identifier path.

    A two-word hook is not evidence: the scraped cell could be any article, so
    without an identifier nothing is attributed.
    """
    _seed("Тарифы ЖКХ")
    result = record_publication_metrics([_metrics("Тарифы ЖКХ вырастут в следующем году по плану")])

    assert result["saved"] == 0
    assert result["matched_by_title"] == 0


def test_a_dry_run_publication_is_never_attributed():
    """A dry run journals `dry_run` and still marks the post published.

    Measured on the production copy: the 22 published Dzen posts with no `posted`
    row are exactly the 22 whose only Dzen journal row is a dry run — they were
    never sent. A real Studio article must not be credited to one of them, not by
    identifier and not by headline, because the numbers would teach the farm a
    result it never produced.
    """
    hook = "Статья, которую ферма только прогоняла в сухом режиме"
    public_id = _seed(hook, remote_id="dry-1234", status="dry_run")

    by_id = record_publication_metrics(
        [_metrics("Что-то совсем другое", url="https://dzen.ru/a/dry-1234", remote_id="dry-1234")]
    )
    assert by_id["saved"] == 0
    assert by_id["matched_by_remote_id"] == 0

    by_title = record_publication_metrics([_metrics(hook)])
    assert by_title["saved"] == 0
    assert by_title["matched_by_title"] == 0
    with session_scope() as session:
        measured = session.execute(
            select(PostStat.id)
            .join(Post, Post.id == PostStat.post_id)
            .where(Post.public_id == public_id)
            .limit(1)
        ).scalar_one_or_none()
    assert measured is None
    assert all(remote_id == "dry-1234" for _s, remote_id, _u in _row_ids(public_id))


def test_a_dry_run_followed_by_a_real_publish_is_attributed():
    """The guard is about never being sent, not about having been rehearsed."""
    hook = "Сначала суходо прогнанный, а потом всё же опубликованный материал"
    public_id = _seed(hook, status="dry_run")
    _seed_log(public_id, "posted")

    item = _metrics(hook)
    result = record_publication_metrics([item])

    assert result["saved"] == 1
    assert result["matched_by_title"] == 1
    assert result["remote_ids_stored"] == 1
