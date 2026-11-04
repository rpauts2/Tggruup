"""The reprint backfill: the same verdict as ingest, over the rows that predate it.

Ingest (`_persist_detailed`) can only flag a copy against documents that were
already in the store when it arrived. Everything older than that rule — and every
pair that had already aged out of its 48-hour window — is still an unflagged row
that trends, niches, heat and `item_count` read as an independent document. These
tests pin the backfill to the ingest decision rather than to a similar one:

* a written link is always a pair `is_content_reprint` accepts, and a complete
  pass leaves no accepted pair behind;
* a bounded pass plus its resume produces exactly the mapping an unbounded pass
  would have produced;
* a second pass writes nothing;
* no link ever points forward in time, so no chain of them can close into a
  cycle, rows with no fingerprint take part in neither role, and a copy group ends
  up with one original.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import combinations

import pytest
import test_trends
from sqlalchemy import select, update

from tgfarm.core.schemas import RawItem
from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.session import session_scope
from tgfarm.s1_sources import INGEST_DEDUP_WINDOW_HOURS, _persist_detailed
from tgfarm.s1_sources.backfill_reprints import Cursor, backfill_reprints
from tgfarm.s1_sources.base import ContentFingerprint, fingerprint_of, is_content_reprint

# The store-wiping fixture S2's tests already use, borrowed from its own module
# (`tests/` has no `__init__.py`, so it is importable by top-level name). Aliased
# as a module attribute rather than `from test_trends import clean_items`, because
# an imported name shadowed by a fixture argument reads to the linter like a
# redefinition of the import.
clean_items = test_trends.clean_items

WIRE_TITLE = "ЦБ поднял ключевую ставку до 19 процентов годовых"
WIRE_TEXT = (
    "Банк России повысил ключевую ставку на 100 базисных пунктов, до 19 процентов годовых. "
    "Регулятор объяснил решение ускорением инфляционных ожиданий и рисками перегрева "
    "спроса; следующее заседание совета назначено на сентябрь."
)
SECOND_TITLE = "МРСК восстановила электроснабжение после аварии в Карелии"
SECOND_TEXT = (
    "Аварийные бригады вернули свет в 43 населённых пунктах республики к утру. "
    "Причиной отключения назвал гололёд, на место выехала комиссия Ростехнадзора."
)
# Same subject, its own article: its own numbers, its own sentences.
SIBLING_TITLE = "ЦБ сохранил ключевую ставку на уровне 19 процентов"
SIBLING_TEXT = (
    "Банк России оставил ключевую ставку без изменений, вопреки ожиданиям аналитиков. "
    "Инфляция замедлилась до 8,4 процента, а регулятор стал обращать внимание на "
    "охлаждение рынка труда и на крепость рубля."
)
OTHER_TITLE = "Ethereum перевёл основную часть комиссий в L2"
OTHER_TEXT = (
    "Переход на уровень два снизил среднюю стоимость перевода почти вчетверо, "
    "а очистка очереди прошла без отключения сети."
)

_VARIANTS = ("{title}", "{title} — главное", "{title}: что известно")


def _store(
    source_id: str,
    *,
    title: str,
    text: str,
    minutes_ago: int,
    simhash_value: int | None = None,
    fetched_minutes_ago: int | None = None,
) -> int:
    """A legacy row: stored with a fingerprint, never compared against anything.

    This is exactly the corpus the backfill inherits — `duplicate_of` NULL even
    though a second outlet copied the document, because the rule landed later. The
    fingerprint is the one ingest writes (`fingerprint_of`), so passing
    `simhash_value=0` describes a row written before that column existed.
    """
    now = datetime.now(UTC)
    with session_scope() as session:
        row = RawItemRow(
            source_id=source_id,
            external_id=f"{source_id}-{minutes_ago}",
            url=f"https://{source_id}.invalid/{abs(hash(title))}/{minutes_ago}",
            title=title,
            text=text,
            lang="ru",
            published_at=now - timedelta(minutes=minutes_ago),
            fetched_at=now - timedelta(minutes=fetched_minutes_ago or minutes_ago),
            niche_hint=["finance"],
            source_weight=0.6,
            embedding=None,  # the recipe needs no vectors; the blob is the table
            simhash=fingerprint_of(title, text) if simhash_value is None else simhash_value,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def _group(
    prefix: str,
    minutes: tuple[int, int, int],
    *,
    title: str = WIRE_TITLE,
    text: str = WIRE_TEXT,
) -> list[int]:
    """One document carried by three outlets, oldest first, reworded headlines."""
    return [
        _store(f"{prefix}_1", title=_VARIANTS[0].format(title=title), text=text, minutes_ago=minutes[0]),
        _store(f"{prefix}_2", title=_VARIANTS[1].format(title=title), text=text, minutes_ago=minutes[1]),
        _store(f"{prefix}_3", title=_VARIANTS[2].format(title=title), text=text, minutes_ago=minutes[2]),
    ]


def _links() -> dict[int, int | None]:
    with session_scope() as session:
        rows = session.execute(
            select(RawItemRow.id, RawItemRow.duplicate_of).order_by(RawItemRow.id)
        ).all()
    return {
        int(row.id): (None if row.duplicate_of is None else int(row.duplicate_of)) for row in rows
    }


def _links_by_source() -> dict[str, str | None]:
    """source_id -> source_id of its original. Id-free, so two corpora compare."""
    with session_scope() as session:
        rows = session.execute(
            select(RawItemRow.source_id, RawItemRow.duplicate_of, RawItemRow.id).order_by(RawItemRow.id)
        ).all()
        by_id = {int(row.id): row.source_id for row in rows}
    return {
        row.source_id: (None if row.duplicate_of is None else by_id[int(row.duplicate_of)])
        for row in rows
    }


def _fingerprints() -> dict[int, ContentFingerprint]:
    with session_scope() as session:
        rows = session.execute(
            select(RawItemRow.id, RawItemRow.title, RawItemRow.text, RawItemRow.simhash)
        ).all()
    return {
        int(row.id): ContentFingerprint.of(
            int(row.id), row.title, row.text, simhash_value=row.simhash
        )
        for row in rows
    }


def _moments() -> dict[int, datetime]:
    with session_scope() as session:
        rows = session.execute(
            select(RawItemRow.id, RawItemRow.published_at, RawItemRow.fetched_at)
        ).all()
    return {int(row.id): (row.published_at or row.fetched_at) for row in rows}


def _clear_links() -> None:
    with session_scope() as session:
        session.execute(update(RawItemRow).values(duplicate_of=None))


# ----------------------------------------------------------- the happy path
def test_links_a_copy_group_to_the_single_earliest_original(clean_items):
    oldest, middle, newest = _group("backfill_group", (90, 60, 10))

    report = backfill_reprints()

    assert report.rows_scanned == 3
    assert report.links_written == 2
    assert report.stopped_by == "complete"
    assert report.candidates_remaining == 0

    links = _links()
    # Both copies name the same document: one original per reprint storm, not a
    # chain that drifts with every cycle.
    assert links[oldest] is None
    assert links[middle] == oldest
    assert links[newest] == oldest


def test_every_written_link_is_a_pair_ingest_would_accept(clean_items):
    _group("backfill_pair", (120, 45, 5))
    _store("backfill_sibling", title=SIBLING_TITLE, text=SIBLING_TEXT, minutes_ago=30)
    _store("backfill_other", title=OTHER_TITLE, text=OTHER_TEXT, minutes_ago=20)

    backfill_reprints()

    links = _links()
    documents = _fingerprints()
    for row_id, original_id in links.items():
        if original_id is None:
            continue
        assert is_content_reprint(documents[row_id], documents[original_id]), (
            "a written link must be a pair the ingest predicate accepts: the "
            "backfill may not quietly run a looser rule than the inlet"
        )

    # ...and a complete pass leaves no comparable accepted pair behind.
    survivors = [row_id for row_id, original_id in links.items() if original_id is None]
    moments = _moments()
    lookback = timedelta(hours=INGEST_DEDUP_WINDOW_HOURS)
    for left, right in combinations(survivors, 2):
        if abs(moments[left] - moments[right]) > lookback:
            continue
        assert not is_content_reprint(documents[left], documents[right]), (
            f"{left} and {right} are two surviving copies of one document"
        )


def test_agrees_with_the_ingest_path_on_the_same_pairs(clean_items):
    """Ingest decides a verdict, the backfill re-derives exactly that verdict.

    The rows are written by `_persist_detailed` itself, so their fingerprints are
    whatever the inlet really stores; clearing `duplicate_of` then models the
    corpus that predates the rule, and the backfill has to put the same links back.
    """

    def item(source_id: str, title: str, text: str, minutes_ago: int) -> RawItem:
        return RawItem(
            source_id=source_id,
            external_id=f"{source_id}-guid",
            url=f"https://{source_id}.invalid/story",
            title=title,
            text=text,
            lang="ru",
            published_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
            niche_hint=["finance"],
            source_weight=0.6,
        )

    _persist_detailed([item("backfill_ingest_a", WIRE_TITLE, WIRE_TEXT, 60)])
    _persist_detailed([item("backfill_ingest_b", SIBLING_TITLE, SIBLING_TEXT, 40)])
    _persist_detailed(
        [item("backfill_ingest_c", "ЦБ поднял ключевую ставку до 19 процентов", WIRE_TEXT, 20)]
    )

    by_ingest = _links_by_source()
    assert by_ingest["backfill_ingest_c"] == "backfill_ingest_a"
    assert by_ingest["backfill_ingest_b"] is None

    _clear_links()
    report = backfill_reprints()

    assert report.links_written == 1
    assert _links_by_source() == by_ingest, (
        "the two paths must never disagree about the same pair of documents"
    )


def test_two_articles_on_one_topic_stay_two_documents(clean_items):
    """The Jaccard gate survives the trip into the backfill."""
    _store("backfill_a1", title=WIRE_TITLE, text=WIRE_TEXT, minutes_ago=90)
    _store("backfill_b1", title=SIBLING_TITLE, text=SIBLING_TEXT, minutes_ago=45)

    assert backfill_reprints().links_written == 0
    assert all(original is None for original in _links().values())


# --------------------------------------------------------------------- budget
class _FakeClock:
    """A clock that jumps, so the budget is tested without sleeping on it."""

    def __init__(self, step: float) -> None:
        self.step = step
        self.now = 0.0

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_time_budget_stops_mid_sweep_and_the_resume_finishes_it(clean_items):
    _group("backfill_first", (300, 240, 180))
    _group("backfill_second", (120, 60, 5), title=SECOND_TITLE, text=SECOND_TEXT)

    # step 3 s against a 7 s budget: two pages of two rows get through, the third
    # page is never read, and the pass reports the remainder instead of lying.
    bounded = backfill_reprints(budget_seconds=7.0, page_size=2, clock=_FakeClock(step=3.0))

    assert bounded.budget_exhausted is True
    assert bounded.stopped_by == "budget"
    assert 0 < bounded.rows_scanned < 6
    assert bounded.candidates_remaining == 6 - bounded.rows_scanned

    resumed = backfill_reprints(resume_after=bounded.next_cursor)

    assert resumed.stopped_by == "complete"
    assert resumed.rows_scanned == bounded.candidates_remaining

    originals = [row_id for row_id, original in _links().items() if original is None]
    assert len(originals) == 2, "one original per storm, across two bounded runs"
    assert resumed.links_written + bounded.links_written == 4


def test_row_cap_and_unbounded_pass_produce_the_identical_mapping(clean_items):
    """The point of a bounded job: chunked runs must not change the answer."""

    def corpus() -> None:
        _group("backfill_cap", (400, 350, 300))
        _store("backfill_loner", title=OTHER_TITLE, text=OTHER_TEXT, minutes_ago=250)
        _group("backfill_cap2", (200, 150, 60), title=SECOND_TITLE, text=SECOND_TEXT)

    corpus()
    chunks = []
    cursor: Cursor | None = None
    while True:
        chunk = backfill_reprints(max_rows=2, budget_seconds=None, resume_after=cursor)
        chunks.append(chunk)
        cursor = chunk.next_cursor
        if chunk.stopped_by == "complete":
            break
    assert chunks[0].stopped_by == "row_cap"
    assert chunks[0].rows_scanned == 2
    incremental = _links_by_source()

    _clear_links()
    with session_scope() as session:
        session.query(RawItemRow).delete()
    corpus()
    unbounded = backfill_reprints(budget_seconds=None)

    assert unbounded.links_written == sum(chunk.links_written for chunk in chunks)
    assert incremental == _links_by_source(), (
        "a row cap may decide when the sweep stops, never what it decides"
    )


def test_second_run_links_nothing(clean_items):
    _group("backfill_idempotent", (180, 90, 10))

    first = backfill_reprints()
    assert first.links_written == 2

    before = _links()
    second = backfill_reprints()

    assert second.links_written == 0
    assert second.links_found == 0
    assert second.candidates_remaining == 0
    assert second.rows_scanned == 1, "only the surviving original is looked at again"
    assert _links() == before, "re-running the backfill is a no-op, not a re-link"


def test_dry_run_reports_links_without_writing_them(clean_items):
    _group("backfill_dry", (180, 90, 10))

    preview = backfill_reprints(dry_run=True)

    assert preview.links_found == 2
    assert preview.links_written == 0
    assert all(original is None for original in _links().values()), (
        "a measurement pass must not touch the corpus"
    )


# ------------------------------------------------------------ ordering rules
def test_a_later_row_is_never_named_as_the_original(clean_items):
    """Feeds backdate: the row with the *smaller id* can be the newer document.

    The live corpus really looks like this — its first three rows have ids 1, 2, 3
    and publication times 16:14, 16:07, 16:00 — so ordering the sweep by id would
    have the oldest article point forward at its own reprint.
    """
    later = _store(
        "backfill_later",
        title=_VARIANTS[0].format(title=WIRE_TITLE),
        text=WIRE_TEXT,
        minutes_ago=45,
        fetched_minutes_ago=10,
    )
    earlier = _store(
        "backfill_earlier",
        title=_VARIANTS[1].format(title=WIRE_TITLE),
        text=WIRE_TEXT,
        minutes_ago=120,
        fetched_minutes_ago=30,
    )
    assert earlier > later, "the fixture must keep id order contradicting time order"

    backfill_reprints()

    links = _links()
    assert links[earlier] is None, "the earlier document stays the original"
    assert links[later] == earlier
    moments = _moments()
    for row_id, original_id in links.items():
        if original_id is None:
            continue
        assert moments[original_id] <= moments[row_id], "no link ever points forward in time"


def test_no_chain_of_links_closes_into_a_cycle(clean_items):
    """Similarity is symmetric; the *sweep direction* is what breaks the tie."""
    _group("backfill_cycle", (200, 100, 20))
    _store("backfill_extra", title=_VARIANTS[2].format(title=WIRE_TITLE), text=WIRE_TEXT, minutes_ago=5)

    report = backfill_reprints()

    assert report.links_written == 3
    links = _links()
    for row_id in links:
        seen = {row_id}
        current = row_id
        while links.get(current) is not None:
            current = int(links[current])
            assert current not in seen, "following duplicate_of must not return to a row"
            seen.add(current)
        assert links[current] is None, "every chain ends at a surviving original"


def test_existing_links_are_left_alone_and_reprints_are_never_originals(clean_items):
    oldest, middle, newest = _group("backfill_existing", (200, 100, 50))
    with session_scope() as session:
        # `middle` was flagged by another path — against a row that is *later* than
        # it. The backfill reads links as given: it never launders them, and never
        # uses a flagged row as an original for a third copy.
        session.execute(update(RawItemRow).where(RawItemRow.id == middle).values(duplicate_of=newest))

    report = backfill_reprints()

    links = _links()
    assert links[middle] == newest, "a link this sweep did not write is not rewritten"
    assert links[oldest] is None
    assert links[newest] == oldest, "the third copy points at the original, not at a flagged copy"
    assert report.rows_scanned == 2, "only the two unflagged rows are candidates"


# ---------------------------------------------------------------- missing data
def test_a_row_without_a_fingerprint_takes_part_in_neither_role(clean_items):
    """`simhash == 0` means "no fingerprint recorded", which is not a fingerprint.

    Treating 0 as comparable would make every legacy row identical to every other
    one — `hamming(0, x)` is just the popcount of x — and a day of the store would
    collapse onto one unrelated article.
    """
    legacy = _store("backfill_legacy", title=WIRE_TITLE, text=WIRE_TEXT, minutes_ago=200, simhash_value=0)
    also_legacy = _store(
        "backfill_legacy2", title=WIRE_TITLE, text=WIRE_TEXT, minutes_ago=100, simhash_value=0
    )
    fingerprinted = _store("backfill_fresh", title=WIRE_TITLE, text=WIRE_TEXT, minutes_ago=50)

    report = backfill_reprints()

    links = _links()
    assert links[legacy] is None
    assert links[also_legacy] is None
    assert links[fingerprinted] is None, (
        "the only earlier copy had no fingerprint, so nothing proves this row is a "
        "reprint of anything"
    )
    assert report.rows_scanned == 1, "the two legacy rows are never even scanned"
    assert report.unfingerprinted_rows == 2


def test_a_row_without_published_at_is_placed_by_its_fetch_clock(clean_items):
    """Feeds omit `published_at` all the time; the fallback is the whole point.

    The sweep orders documents by `published_at` *else* `fetched_at`, so such a
    row is still placed in time and can still be the earliest original. A row with
    neither — which this schema's NOT NULL on `fetched_at` makes impossible, and
    which the `undated_rows` counter guards anyway — would be outside the sweep
    rather than silently sorted to the front of it.
    """
    undated = _store("backfill_undated", title=WIRE_TITLE, text=WIRE_TEXT, minutes_ago=100)
    copy = _store(
        "backfill_dated",
        title=_VARIANTS[1].format(title=WIRE_TITLE),
        text=WIRE_TEXT,
        minutes_ago=50,
    )
    with session_scope() as session:
        session.execute(update(RawItemRow).where(RawItemRow.id == undated).values(published_at=None))

    report = backfill_reprints()

    links = _links()
    assert links[undated] is None, "the fetch clock puts it first"
    assert links[copy] == undated
    assert report.undated_rows == 0


# -------------------------------------------------------------------- window
def test_the_default_window_is_the_ingest_window(clean_items):
    """Outside the window the backfill leaves a pair alone, exactly like ingest.

    A wider default would let the two paths disagree about the same pair, which is
    the one failure this module exists to make impossible.
    """
    far = _store("backfill_far", title=WIRE_TITLE, text=WIRE_TEXT, minutes_ago=60 * 60)
    near = _store(
        "backfill_near",
        title=_VARIANTS[1].format(title=WIRE_TITLE),
        text=WIRE_TEXT,
        minutes_ago=10,
    )

    default = backfill_reprints()
    assert default.window_hours == INGEST_DEDUP_WINDOW_HOURS
    assert default.links_written == 0
    assert _links() == {far: None, near: None}

    widened = backfill_reprints(window_hours=72)
    assert widened.links_written == 1
    assert _links()[near] == far


def test_an_invalid_window_or_pool_is_refused_not_silently_wide(clean_items):
    _group("backfill_args", (60, 30, 5))

    with pytest.raises(ValueError):
        backfill_reprints(window_hours=0)
    with pytest.raises(ValueError):
        backfill_reprints(pool_limit=0)


# ------------------------------------------------------------------ plumbing
def test_reads_are_chunked_and_the_pool_stays_bounded(clean_items):
    for index in range(6):
        _store(
            f"backfill_pool_{index}",
            title=_VARIANTS[index % 3].format(title=f"{WIRE_TITLE} {index}"),
            text=WIRE_TEXT,
            minutes_ago=600 - 10 * index,
        )

    report = backfill_reprints(page_size=2, pool_limit=2)

    assert report.pages_read == 3, "six rows over pages of two, one page at a time"
    assert report.pool_peak <= 2, "the in-memory originals never outgrow the cap"
    assert report.rows_scanned == 6


def test_the_report_is_a_plain_dict_for_the_maintenance_journal(clean_items):
    _group("backfill_shape", (120, 60, 10))

    payload = backfill_reprints().as_dict()

    assert payload["rows_scanned"] == 3
    assert payload["links_written"] == 2
    assert payload["stopped_by"] == "complete"
    assert payload["budget_exhausted"] is False
    assert payload["window_hours"] == INGEST_DEDUP_WINDOW_HOURS
    assert set(payload) >= {"candidates_remaining", "unfingerprinted_rows", "elapsed_s"}
