"""S2 acceleration + the dedup-survival rule for every independence metric.

Two contracts are pinned here.

* Interest is compared window over window, so a *rising* story is separated from
  a single-window *burst*, from steady-high volume and from decay — and a burst
  is not promoted as a trend.
* `duplicate_of` is not a cosmetic flag any more. Every statistic that claims
  independent coverage (distinct outlets in `velocity`, `cross_platform`,
  `item_count`, the members handed to S3) is computed over the items that
  survived the dedup pass, and the measured values are persisted on those items
  and read back as the heat inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from tgfarm import s2_trends
from tgfarm.core.settings import get_settings
from tgfarm.core.text import simhash
from tgfarm.db.models import Cluster
from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.session import session_scope

BODY = (
    "Регулятор повысил ставку на 100 базисных пунктов. Аналитики ожидали большего, "
    "реакция рынка оказалась сдержанной, а депозиты начали медленно дорожать."
)


def _now() -> datetime:
    return datetime.now(UTC)


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


def _make_cluster(public_id: str, *, heat: float = 0.0, status: str = "forming") -> int:
    with session_scope() as session:
        cluster = Cluster(
            public_id=public_id,
            label="Ставка",
            heat=heat,
            status=status,
            last_seen_at=_now(),
        )
        session.add(cluster)
        session.flush()
        return int(cluster.id)


def _seed(
    *,
    cluster_id: int | None = None,
    source_id: str,
    title: str,
    text: str = BODY,
    hours_ago: float,
    weight: float = 0.9,
    duplicate_of: int | None = None,
    simhash_value: int | None = None,
    with_embedding: bool = False,
) -> int:
    moment = _now() - timedelta(hours=hours_ago)
    blob = None
    if with_embedding:
        from tgfarm.db.vector import pack
        from tgfarm.llm.embedding import get_embedder

        blob = pack(get_embedder().embed(f"{title}\n{text}"))
    with session_scope() as session:
        row = RawItemRow(
            source_id=source_id,
            external_id=f"{source_id}-{title[:20]}-{hours_ago}",
            url=f"https://{source_id}.invalid/{abs(hash(title))}-{hours_ago}",
            title=title,
            text=text,
            lang="ru",
            published_at=moment,
            fetched_at=moment if hours_ago <= 12 else _now(),
            niche_hint=["finance"],
            source_weight=weight,
            embedding=blob,
            simhash=simhash_value if simhash_value is not None else simhash(f"{title} {text[:600]}"),
            duplicate_of=duplicate_of,
            cluster_id=cluster_id,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def _seed_many(
    prefix: str,
    title: str,
    count: int,
    *,
    cluster_id: int | None = None,
    hours_ago: float = 1.0,
    with_embedding: bool = False,
) -> list[int]:
    return [
        _seed(
            cluster_id=cluster_id,
            source_id=f"{prefix}_{index}",
            title=title,
            hours_ago=hours_ago,
            with_embedding=with_embedding,
        )
        for index in range(count)
    ]


def _row(item_id: int) -> RawItemRow:
    with session_scope() as session:
        return session.get(RawItemRow, item_id)


def _cluster(public_id: str) -> Cluster:
    with session_scope() as session:
        return session.execute(
            select(Cluster).where(Cluster.public_id == public_id)
        ).scalar_one()


# ------------------------------------------------------------------- the pure rule
def test_unknown_when_there_is_no_previous_window():
    phase, rate = s2_trends.classify_acceleration(5, None)
    assert phase == "unknown"
    assert rate is None, "no baseline is reported as unknown, not as a made-up rate"


def test_measured_empty_baseline_with_items_now_is_a_spike():
    assert s2_trends.classify_acceleration(5, 0) == ("spike", None)


def test_two_window_increase_is_rising():
    phase, rate = s2_trends.classify_acceleration(6, 2)
    assert phase == "rising"
    assert rate == pytest.approx(2.0)


def test_steady_high_volume_is_not_rising():
    """Six items becoming six items is a loud story, not an accelerating one."""
    assert s2_trends.classify_acceleration(6, 6) == ("steady", pytest.approx(0.0))
    phase, _ = s2_trends.classify_acceleration(9, 8)
    assert phase == "steady"


def test_falling_volume_is_decaying():
    phase, rate = s2_trends.classify_acceleration(1, 6)
    assert phase == "decaying"
    assert rate == pytest.approx(-5 / 6)


def test_empty_windows_do_not_blow_up_or_crash():
    assert s2_trends.classify_acceleration(0, 0) == ("steady", pytest.approx(0.0))
    assert s2_trends.classify_acceleration(0, None) == ("unknown", None)
    phase, rate = s2_trends.classify_acceleration(0, 3)
    assert (phase, rate) == ("decaying", pytest.approx(-1.0))


def test_a_small_jump_is_measured_but_not_called_a_trend():
    phase, rate = s2_trends.classify_acceleration(2, 1)
    assert phase == "steady", "one item becoming two is noise whatever the ratio"
    assert rate == pytest.approx(1.0), "the measured change is still reported"


# ----------------------------------------------------- duplicates out of the metrics
def test_reprint_storm_metrics_see_one_independent_outlet(clean_store):
    title = "ЦБ поднял ключевую ставку до 19 процентов годовых"
    _seed_many("storm", title, 5, with_embedding=True)

    assert s2_trends._dedup_pass() == 4
    assert s2_trends._cluster_pass() == 1
    assert s2_trends._score_pass() == 1

    with session_scope() as session:
        cluster = session.execute(select(Cluster)).scalar_one()
        items = session.execute(
            select(RawItemRow).order_by(RawItemRow.id)
        ).scalars().all()

    survivor = next(i for i in items if i.duplicate_of is None)
    copies = [i for i in items if i.duplicate_of is not None]
    assert len(copies) == 4
    assert {c.duplicate_of for c in copies} == {survivor.id}

    # five outlets reprinting one document are one outlet, not five
    assert cluster.item_count == 1
    assert survivor.velocity == pytest.approx(0.25)
    assert survivor.cross_platform == pytest.approx(0.25)
    for copy in copies:
        assert copy.velocity == 0.0
        assert copy.cross_platform == 0.0
        assert copy.recency == 0.0


def test_items_already_clustered_before_being_flagged_are_zeroed(clean_store):
    """The legacy-database shape: clustering ran before dedup, so the copies are
    already members of the cluster. They must drop out of every metric."""
    title = "ЦБ поднял ключевую ставку до 19 процентов годовых"
    cluster_id = _make_cluster("legacy-storm")
    ids = _seed_many("legacy", title, 4, cluster_id=cluster_id)

    assert s2_trends._dedup_pass() == 3
    assert s2_trends._score_pass() == 1

    with session_scope() as session:
        cluster = session.get(Cluster, cluster_id)
        items = session.execute(
            select(RawItemRow).where(RawItemRow.cluster_id == cluster_id)
        ).scalars().all()

    survivor = next(i for i in items if i.duplicate_of is None)
    assert survivor.id in ids
    assert cluster.item_count == 1
    assert survivor.velocity == pytest.approx(0.25)
    assert [i.velocity for i in items if i.duplicate_of is not None] == [0.0, 0.0, 0.0]
    assert [i.cross_platform for i in items if i.duplicate_of is not None] == [0.0, 0.0, 0.0]


def test_heat_is_read_from_the_persisted_measurements(clean_store):
    """The stored per-item signals are the heat inputs, not decoration."""
    cluster_id = _make_cluster("readback")
    _seed(cluster_id=cluster_id, source_id="read_a", title="Первый материал", hours_ago=1)
    _seed(cluster_id=cluster_id, source_id="read_b", title="Второй материал", hours_ago=2)

    assert s2_trends._score_pass() == 1
    with session_scope() as session:
        cluster = session.get(Cluster, cluster_id)
        measured_heat = cluster.heat
        values = session.execute(
            select(
                RawItemRow.source_weight,
                RawItemRow.velocity,
                RawItemRow.recency,
                RawItemRow.cross_platform,
            ).where(RawItemRow.cluster_id == cluster_id)
        ).all()
        assert max(v for _, v, _, _ in values) > 0.0

    expected = round(
        s2_trends.W_SOURCE * max(r.source_weight for r in values)
        + s2_trends.W_VELOCITY * max(r.velocity for r in values)
        + s2_trends.W_RECENCY * max(r.recency for r in values)
        + s2_trends.W_CROSS * max(r.cross_platform for r in values)
        + s2_trends.W_DEFICIT * 0.5,
        4,
    )
    assert measured_heat == pytest.approx(expected)

    # zero the columns and score without re-measuring: heat follows the columns
    with session_scope() as session:
        session.execute(
            update(RawItemRow)
            .where(RawItemRow.cluster_id == cluster_id)
            .values(velocity=0.0, recency=0.0, cross_platform=0.0)
        )
    assert s2_trends._score_pass(measure=False) == 1
    after = _cluster("readback")
    assert after.heat == pytest.approx(
        round(s2_trends.W_SOURCE * 0.9 + s2_trends.W_DEFICIT * 0.5, 4)
    )
    assert after.heat < measured_heat


def test_ready_clusters_hand_s3_only_surviving_items(clean_store):
    title = "ЦБ поднял ключевую ставку до 19 процентов годовых"
    for source in "abc":
        _seed(source_id=f"gate_{source}", title=title, hours_ago=1)
    cluster_id = _make_cluster("gate", heat=0.95, status="ready")
    ids = [_seed(source_id=f"gate_{s}", title=title, hours_ago=1) for s in "xyz"]
    with session_scope() as session:
        session.execute(
            update(RawItemRow)
            .where(RawItemRow.id.in_(ids[1:]))
            .values(duplicate_of=ids[0], cluster_id=cluster_id)
        )
        session.execute(
            update(RawItemRow)
            .where(RawItemRow.id == ids[0])
            .values(cluster_id=cluster_id, text=BODY * 3)
        )

    picked = s2_trends.ready_clusters(min_items=1)
    assert [p[0] for p in picked] == ["gate"]
    members = picked[0][3]
    assert [m.id for m in members] == [ids[0]]
    with_survivors_and_copies = s2_trends.ready_clusters(min_items=1, include_duplicates=True)[0][3]
    assert sorted(m.id for m in with_survivors_and_copies) == sorted(ids)

    # `min_items` is a claim about independent coverage, so one survivor is not two
    assert s2_trends.ready_clusters(min_items=2) == []


# --------------------------------------------------------------- window behaviour
def test_two_window_increase_is_measured_as_rising(clean_store):
    cluster_id = _make_cluster("rising")
    for index, source in enumerate(("prev_a", "prev_b")):
        _seed(cluster_id=cluster_id, source_id=source, title=f"Прошлое {index}", hours_ago=8)
    for index, source in enumerate(("now_a", "now_b", "now_c", "now_d", "now_e", "now_f")):
        _seed(cluster_id=cluster_id, source_id=source, title=f"Сейчас {index}", hours_ago=1)

    assert s2_trends._score_pass() == 1
    cluster = _cluster("rising")
    assert cluster.trend_phase == "rising"
    assert cluster.acceleration == pytest.approx(2.0)


def test_steady_high_volume_is_not_reclassified_as_rising(clean_store):
    cluster_id = _make_cluster("steady")
    for index in range(6):
        _seed(cluster_id=cluster_id, source_id=f"before_{index}", title=f"Тема раньше {index}", hours_ago=8)
    for index in range(6):
        _seed(cluster_id=cluster_id, source_id=f"after_{index}", title=f"Тема теперь {index}", hours_ago=1)

    assert s2_trends._score_pass() == 1
    cluster = _cluster("steady")
    assert cluster.trend_phase == "steady"
    assert cluster.acceleration == pytest.approx(0.0)
    # steady volume is still a trend: it is promotable on heat
    assert cluster.heat >= get_settings().heat_threshold
    assert s2_trends.promote_ready() == 1


def test_single_window_burst_is_a_spike_and_is_not_promoted(clean_store):
    cluster_id = _make_cluster("burst")
    for index in range(2):
        _seed(cluster_id=cluster_id, source_id=f"history_{index}", title=f"История {index}", hours_ago=20)
    for index in range(4):
        _seed(cluster_id=cluster_id, source_id=f"burst_{index}", title=f"Всплеск {index}", hours_ago=1)

    assert s2_trends._score_pass() == 1
    cluster = _cluster("burst")
    assert cluster.trend_phase == "spike", "interest with an empty preceding window is a burst"
    assert cluster.acceleration is None, "a burst has no window-over-window rate to report"
    assert cluster.item_count == 6
    assert cluster.heat >= get_settings().heat_threshold

    assert s2_trends.promote_ready() == 0
    assert _cluster("burst").status == "forming"
    assert s2_trends.promote_ready(allow_spikes=True) == 1


def test_nothing_to_compare_against_is_reported_unknown_not_spike(clean_store):
    cluster_id = _make_cluster("cold")
    for index in range(3):
        _seed(cluster_id=cluster_id, source_id=f"cold_{index}", title=f"Холодный старт {index}", hours_ago=1)

    assert s2_trends._score_pass() == 1
    cluster = _cluster("cold")
    assert cluster.trend_phase == "unknown"
    assert cluster.acceleration is None
    assert s2_trends.promote_ready() == 1, "unknown shape is judged on heat alone"


def test_decaying_story_is_measured_as_decaying(clean_store):
    cluster_id = _make_cluster("fading")
    for index in range(5):
        _seed(cluster_id=cluster_id, source_id=f"was_{index}", title=f"Было {index}", hours_ago=8)
    _seed(cluster_id=cluster_id, source_id="now_only", title="Осталось одно", hours_ago=1)

    assert s2_trends._score_pass() == 1
    cluster = _cluster("fading")
    assert cluster.trend_phase == "decaying"
    assert cluster.acceleration == pytest.approx((1 - 5) / 5)


def test_empty_cluster_and_empty_window_survive_measurement(clean_store):
    _make_cluster("empty")

    assert s2_trends._measure_pass() == 0
    cluster = _cluster("empty")
    assert cluster.trend_phase == "unknown"
    assert cluster.acceleration is None

    assert s2_trends._score_pass() == 0
    assert s2_trends.promote_ready() == 0


def test_legacy_rows_without_fingerprint_are_not_deduped(clean_store):
    """A missing simhash must not read as 'identical to everything'."""
    _seed(source_id="nofp_a", title="Пенсионный фонд начал новые выплаты", hours_ago=1, simhash_value=0)
    _seed(source_id="nofp_b", title="Курский агрохолдинг собрал урожай пшеницы", hours_ago=2, simhash_value=0)

    assert s2_trends._dedup_pass() == 0, (
        "hamming(0, x) is a meaningless distance, and two unrelated titles are not "
        "duplicates of each other just because nobody fingerprinted them"
    )
    # the same rows in the measurement path: no fingerprint, no crash, no verdict
    assert s2_trends._score_pass() == 0


def test_public_shape_helper_over_survivors(clean_store):
    items = [
        _seed(source_id=f"pub_{index}", title=f"Тема {index}", hours_ago=hours)
        for index, hours in enumerate((8, 9, 1, 1, 1))
    ]
    rows = [_row(item_id) for item_id in items]

    phase, rate = s2_trends.acceleration_of(rows)
    assert phase == "rising"
    assert rate == pytest.approx((3 - 2) / 2)
