"""P27 — `_dedup_pass` obeys exactly one contract for `duplicate_of`: the ingest one.

A copy links to the **earliest surviving original**. Concretely: never to a row
that is itself flagged (chains resolve to their root first), never forward in
time, never into a cycle, never at a dangling link, and with a deterministic
survivor — the window is walked oldest-first with `id` as the tie-breaker, so
the outcome does not depend on the order SQLite happened to return rows in.
These regressions would all have failed against the old newest-first pass,
which kept whichever document it met first (so it flagged the *original* of a
group), produced mutual A↔B pairs and links that point at the future.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tgfarm.core.text import simhash
from tgfarm.db.models import Cluster
from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.session import session_scope
from tgfarm.db.vector import get_index, pack, unpack
from tgfarm.llm.embedding import get_embedder
from tgfarm.s2_trends import MAX_DUP_CHAIN_HOPS, _dedup_pass, resolve_duplicate_root

WIRE_TITLE = "ЦБ поднял ключевую ставку до 19 процентов годовых"
WIRE_TEXT = (
    "Банк России повысил ключевую ставку на 100 базисных пунктов, до 19 процентов годовых. "
    "Регулятор объяснил решение ускорением инфляционных ожиданий и рисками перегрева спроса."
)


def _now() -> datetime:
    return datetime.now(UTC)


def _wipe() -> None:
    with session_scope() as session:
        session.query(RawItemRow).delete()
        session.query(Cluster).delete()


@pytest.fixture()
def clean_store():
    _wipe()
    get_index().clear()
    yield
    _wipe()
    get_index().clear()


def _seed(
    source_id: str,
    title: str,
    *,
    row_id: int | None = None,
    text: str = WIRE_TEXT,
    published_at: datetime | None = None,
    fetched_at: datetime | None = None,
    duplicate_of: int | None = None,
    embedding_text: str | None = None,
) -> int:
    blob = None
    if embedding_text is not None:
        blob = pack(get_embedder().embed(embedding_text))
    values = {
        "source_id": source_id,
        "external_id": f"{source_id}-ext",
        "url": f"https://{source_id}.invalid/story",
        "title": title,
        "text": text,
        "lang": "ru",
        "published_at": published_at,
        "fetched_at": fetched_at if fetched_at is not None else _now(),
        "niche_hint": ["finance"],
        "source_weight": 0.6,
        "embedding": blob,
        "simhash": simhash(f"{title} {text[:600]}"),
        "duplicate_of": duplicate_of,
    }
    if row_id is not None:
        values["id"] = row_id
    with session_scope() as session:
        row = RawItemRow(**values)
        session.add(row)
        session.flush()
        return int(row.id)


def _register_in_index(row_id: int, text: str) -> None:
    get_index().add(row_id, unpack(pack(get_embedder().embed(text))))


def _links() -> dict[int, int | None]:
    with session_scope() as session:
        rows = session.execute(
            select(RawItemRow.id, RawItemRow.duplicate_of).order_by(RawItemRow.id)
        ).all()
    return {int(item_id): dup for item_id, dup in rows}


# -------------------------------------------------------------- pure root resolver
def test_resolve_walks_a_chain_to_its_root():
    links = {11: None, 12: 11, 13: 12}
    assert resolve_duplicate_root(13, links.get) == 11
    assert resolve_duplicate_root(12, links.get) == 11
    assert resolve_duplicate_root(11, links.get) == 11, "a survivor is its own root"


def test_resolve_refuses_a_mutual_pair():
    links = {21: 22, 22: 21}
    assert resolve_duplicate_root(21, links.get) is None


def test_resolve_refuses_a_self_link():
    links = {31: 31}
    assert resolve_duplicate_root(31, links.get) is None


def test_resolve_refuses_a_dangling_chain():
    def parent_of(row_id: int) -> int | None:
        if row_id == 41:
            return 42
        raise LookupError(row_id)  # the store does not have 42

    assert resolve_duplicate_root(41, parent_of) is None


def test_resolve_stops_at_the_hop_cap_instead_of_hanging():
    deep = {i: i - 1 for i in range(1, 61)}  # a 60-deep chain
    assert MAX_DUP_CHAIN_HOPS < 60
    assert resolve_duplicate_root(60, deep.get) is None
    short = {i: i - 1 for i in range(2, MAX_DUP_CHAIN_HOPS + 1)}
    assert resolve_duplicate_root(MAX_DUP_CHAIN_HOPS, short.get) == 1


# ----------------------------------------------------------------- (a) oldest wins
def test_three_editions_leave_the_oldest_as_the_only_survivor(clean_store):
    now = _now()
    # seeded oldest-first — the old newest-first scan flagged exactly this shape's
    # original and kept the newest reprint.
    oldest = _seed("ed_old", WIRE_TITLE, published_at=now - timedelta(hours=3))
    middle = _seed("ed_mid", WIRE_TITLE, published_at=now - timedelta(hours=2))
    newest = _seed("ed_new", WIRE_TITLE, published_at=now - timedelta(hours=1))

    assert _dedup_pass() == 2
    links = _links()
    assert links[oldest] is None
    assert links[newest] == oldest
    assert links[middle] == oldest


def test_time_not_insertion_order_decides_the_survivor(clean_store):
    """Same group, seeded newest-first: the survivor is still the oldest document,
    even though it is the last row that was inserted."""
    now = _now()
    newest = _seed("rev_new", WIRE_TITLE, published_at=now - timedelta(hours=1))
    middle = _seed("rev_mid", WIRE_TITLE, published_at=now - timedelta(hours=2))
    oldest = _seed("rev_old", WIRE_TITLE, published_at=now - timedelta(hours=3))

    assert _dedup_pass() == 2
    links = _links()
    assert links[oldest] is None
    assert links[newest] == oldest
    assert links[middle] == oldest


def test_no_link_points_at_a_flagged_row(clean_store):
    """The broken shape this contract forbids: C→B→A instead of C→A and B→A."""
    now = _now()
    for index, hours in enumerate((4, 3, 2, 1)):
        _seed(
            f"storm_{index}",
            WIRE_TITLE,
            published_at=now - timedelta(hours=hours),
            text=WIRE_TEXT if index else WIRE_TEXT + " Обновлено в 18:00.",
        )
    assert _dedup_pass() == 3
    links = _links()
    for target in links.values():
        assert target is None or links[target] is None, (
            "every link names a surviving root, never another copy"
        )


# ------------------------------------------------- (b) equal timestamps, no flip
def test_equal_timestamps_survive_both_insertion_orders(clean_store):
    moment = _now() - timedelta(minutes=30)
    rows = {"first": 101, "second": 102, "third": 103}

    def run_in(order: list[str]) -> int:
        _wipe()
        for name in order:
            _seed(
                f"tie_{name}",
                WIRE_TITLE,
                row_id=rows[name],
                published_at=moment,
                fetched_at=moment,
            )
        assert _dedup_pass() == 2
        links = _links()
        survivors = [item_id for item_id, dup in links.items() if dup is None]
        assert survivors == [rows["first"]], "the tie is broken by id, not by luck"
        assert {d for d in links.values() if d is not None} == {rows["first"]}
        return survivors[0]

    assert run_in(["first", "second", "third"]) == run_in(["third", "second", "first"])


# --------------------------------------- (c) a pre-existing chain resolves to its root
def test_vector_match_through_a_chain_links_the_root_not_the_middle(clean_store):
    old = _now() - timedelta(days=10)
    vec_text = "один и тот же документ, перепечатанный вторым изданием"
    # A → B → C, all far outside the window; C survived.
    c = _seed(
        "chain_c", "Прежняя сводка новостей рынка", row_id=201,
        published_at=old, fetched_at=old,
    )
    b = _seed(
        "chain_b", "Отчёт о квартальных цифрах", row_id=202,
        published_at=old, fetched_at=old, duplicate_of=c, embedding_text=vec_text,
    )
    a = _seed(
        "chain_a", "Заметка про рынок труда", row_id=203,
        published_at=old, fetched_at=old, duplicate_of=b, embedding_text=vec_text,
    )

    # the new copy shares only its embedding with A — its title and fingerprint
    # match nothing, so only the vector path can find it.
    n = _seed(
        "chain_new", "Прямая линия президента закончилась",
        published_at=_now() - timedelta(hours=2), embedding_text=vec_text,
    )
    _register_in_index(a, vec_text)

    assert _dedup_pass() == 1
    links = _links()
    assert links[n] == c, "the link names the root C, not the flagged row A or B"
    assert links[a] == b and links[b] == c, "this pass rewrites nothing it did not flag"
    assert links[c] is None


# ----------------------------------------------------- (d) cycles cannot hang it
def test_mutual_pair_terminates_and_no_third_link_joins_it(clean_store):
    old = _now() - timedelta(days=9)
    vec_text = "история про энергоблок, которого никто не чинил"
    a = _seed(
        "cycle_a", "На станции начался капитальный ремонт", row_id=301,
        published_at=old, fetched_at=old, embedding_text=vec_text,
    )
    b = _seed(
        "cycle_b", "Местные жители ждут новых автобусов", row_id=302,
        published_at=old, fetched_at=old, duplicate_of=a, embedding_text=vec_text,
    )
    with session_scope() as session:
        session.get(RawItemRow, a).duplicate_of = b  # mutual A↔B
    n = _seed(
        "cycle_new", "Футбольная сборная объявила состав",
        published_at=_now() - timedelta(hours=3), embedding_text=vec_text,
    )
    _register_in_index(a, vec_text)

    assert _dedup_pass() == 0  # terminates, and finds nothing linkable
    links = _links()
    assert links[n] is None, "an unresolvable cycle must not swallow the new document"
    assert links[a] == b and links[b] == a, "the pass added no third link into the cycle"


# --------------------------------- (e) only match is a flagged row → stay a survivor
def test_self_linked_flagged_match_leaves_the_row_a_survivor(clean_store):
    old = _now() - timedelta(days=8)
    vec_text = "запись реестра, которая ссылается сама на себя"
    x = _seed(
        "self_x", "Город закрыл набережную на ремонт", row_id=401,
        text="Набережную закрыли на ремонт до конца сезона, обход пустили по третьей улице.",
        published_at=old, fetched_at=old, embedding_text=vec_text,
    )
    with session_scope() as session:
        session.get(RawItemRow, x).duplicate_of = x  # self-referential link

    n = _seed(
        "self_new", "Оперный театр показал премьеру балета", row_id=402,
        text="Оперный театр открыл сезон балетной премьерой при аншлаге.",
        published_at=_now() - timedelta(hours=4), embedding_text=vec_text,
    )
    _register_in_index(x, vec_text)

    assert _dedup_pass() == 0
    assert _links()[n] is None


def test_root_that_would_be_forward_in_time_is_not_linked(clean_store):
    """X (flagged, outside the window) resolves to root R — but R appears *after*
    the candidate row, and a link into the future is exactly what 563 production
    rows had. The candidate must stay a survivor instead."""
    old = _now() - timedelta(days=7)
    vec_text = "корреспондент перепечатал заметку раньше, чем ту опубликовали"
    now = _now()
    r = _seed(
        "fwd_r", "Мэрия отложила вступление правил в силу", row_id=501,
        text="Мэрия перенесла дату вступления новых правил благоустройства на весну.",
        published_at=now - timedelta(hours=1), fetched_at=now - timedelta(hours=1),
    )
    x = _seed(
        "fwd_x", "Погода на выходных будет тёплой", row_id=502,
        text="Синоптики обещают тёплые выходные без осадков в центральной полосе.",
        published_at=old, fetched_at=old, duplicate_of=r, embedding_text=vec_text,
    )
    n = _seed(
        "fwd_n", "Театр кукол открыл новый сезон", row_id=503,
        text="Театр кукол открыл сезон постановкой для самых маленьких зрителей.",
        published_at=now - timedelta(hours=2), fetched_at=now, embedding_text=vec_text,
    )
    _register_in_index(x, vec_text)

    assert _dedup_pass() == 0
    links = _links()
    assert links[n] is None, "linking N to R would point forward in time"
    assert links[r] is None
    assert links[x] == r  # the pre-existing forward row is left for the repair pass
