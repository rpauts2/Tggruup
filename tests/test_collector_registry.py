"""One registry, two readers: the scheduler and the coverage report may not drift.

P52 measured the drift, not guessed it: ``s8_analytics/coverage.py`` claimed only
telegram was on a schedule while ``pipeline._build_scheduler`` registered
``metrics-and-learning`` (which runs the Дзен and VK collectors inside
``s8_analytics.run()``) and ``telegraph-metrics``, and ``COLLECTORS`` held
``telegraph: None`` beside a registered job. The dicts were hand-copied prose from
the registration; now the registration itself is the data
(``pipeline.collector_job_specs()``) and coverage derives every membership from
it. These tests are the guard: a collector added on one side only must fail here,
and the ids/intervals of the hand-written era must stay byte-identical.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from tgfarm import pipeline
from tgfarm.db.models import Channel as ChannelRow
from tgfarm.db.models import CrosspostLog, PostStat, TelegraphMetricSnapshot
from tgfarm.db.models import Post as PostRow
from tgfarm.db.session import session_scope
from tgfarm.s8_analytics import coverage
from tgfarm.s8_analytics.coverage import STATUS_PARTIAL

#: The collector jobs the hand-written `_build_scheduler` registered on `0e70eff`.
#: Renaming one here and there is exactly the drift this file exists to catch, so
#: the expectations are spelled out and never recomputed from the specs.
BASELINE_COLLECTOR_JOBS = {
    "metrics-and-learning": 1,
    "startup-metrics-and-learning": None,
    "telegram-post-metrics": 6,
    "startup-telegram-post-metrics": None,
    "telegraph-metrics": 6,
    "startup-telegraph-metrics": None,
}

_ANALYTICS_TABLES = (
    PostStat.__table__,
    CrosspostLog.__table__,
    PostRow.__table__,
    ChannelRow.__table__,
)


@pytest.fixture()
def scheduler():
    built = pipeline._build_scheduler()
    try:
        yield built
    finally:
        if built.running:
            built.shutdown(wait=False)


@pytest.fixture(autouse=True)
def _isolated_analytics_tables():
    """Same snapshot/restore the coverage panel suite uses: leave the fleet as found."""
    with session_scope() as session:
        snapshot = {
            table.name: [dict(row) for row in session.execute(select(table)).mappings().all()]
            for table in _ANALYTICS_TABLES
        }
        for table in _ANALYTICS_TABLES:
            session.execute(delete(table))
    yield
    with session_scope() as session:
        for table in _ANALYTICS_TABLES:
            session.execute(delete(table))
        for table in reversed(_ANALYTICS_TABLES):
            rows = snapshot.get(table.name) or []
            if rows:
                session.execute(table.insert(), rows)


# ------------------------------------------------------- registry vs scheduler


def test_registered_collector_jobs_are_exactly_the_baseline_ids(scheduler):
    """Job ids on the scheduler that belong to collectors, and nothing else moved."""
    spec_ids = {
        job_id
        for spec in pipeline.collector_job_specs()
        for job_id in (spec.job_id, spec.startup_job_id)
    }
    assert spec_ids == set(BASELINE_COLLECTOR_JOBS), "a collector job id changed shape"
    for job_id in spec_ids:
        assert scheduler.get_job(job_id) is not None, f"{job_id} is in the registry but not registered"


def test_intervals_names_and_behaviour_are_byte_identical_to_the_handwritten_blocks(scheduler):
    for spec in pipeline.collector_job_specs():
        job = scheduler.get_job(spec.job_id)
        assert job is not None
        assert job.func is spec.tick
        assert job.trigger.interval == timedelta(hours=spec.interval_hours)
        assert job.name == spec.name
        assert job.max_instances == 1
        assert job.coalesce is True
        assert job.misfire_grace_time == 3600
        twin = scheduler.get_job(spec.startup_job_id)
        assert twin is not None
        assert twin.func is spec.tick
        assert twin.name == spec.startup_name
        assert twin.max_instances == 1
        assert twin.misfire_grace_time == 3600


def test_no_collector_tick_is_registered_outside_the_registry(scheduler):
    """The drift of wave 10's opposite direction: a job added by hand.

    If `_build_scheduler` gets its own `add_job(_telegram_metrics_tick, ...)`
    again, the coverage audit would keep answering from the specs and the two
    would disagree - which is the bug, no matter which side holds the truth.
    Every job bound to a collector tick must come from a spec.
    """
    tick_to_spec_ids = {}
    for spec in pipeline.collector_job_specs():
        tick_to_spec_ids.setdefault(spec.tick, set()).update({spec.job_id, spec.startup_job_id})
    rogue = [
        job.id
        for job in scheduler.get_jobs()
        if job.func in tick_to_spec_ids and job.id not in tick_to_spec_ids[job.func]
    ]
    assert not rogue, f"collector ticks registered outside collector_job_specs(): {rogue}"
    bound = [job for job in scheduler.get_jobs() if job.func in tick_to_spec_ids]
    assert len(bound) == 2 * len(pipeline.collector_job_specs())


def test_specs_are_unique_and_do_not_collide_with_other_jobs(scheduler):
    specs = pipeline.collector_job_specs()
    ids = [job_id for spec in specs for job_id in (spec.job_id, spec.startup_job_id)]
    assert len(ids) == len(set(ids)), "two specs share one job id"
    platforms = [platform for spec in specs for platform in spec.platforms]
    assert len(platforms) == len(set(platforms)), "one platform claimed by two specs"
    other_ids = {job.id for job in scheduler.get_jobs() if job.id not in set(ids)}
    assert not set(ids) & other_ids


# ------------------------------------------------------ registry vs real world


def test_every_named_collector_module_and_entry_actually_exists():
    """A spec that names a reader which does not exist is a fake scheduled collector.

    Planting one (platform "mastodon", module "tgfarm.s8_analytics.mastodon_web")
    fails here first: the audit may only promise what a farm-owned module can do.
    """
    for spec in pipeline.collector_job_specs():
        for target in spec.targets:
            module = importlib.import_module(target.module)
            attribute = target.entry.split(".")[-1]
            assert callable(getattr(module, attribute, None)), (
                f"{target.platform}: {target.module} has no callable {attribute}"
            )


def test_feeds_post_stats_matches_the_tables_the_writers_touch():
    by_platform = {
        platform: spec
        for spec in pipeline.collector_job_specs()
        for platform in spec.platforms
    }
    assert by_platform["telegram"].feeds_post_stats is True
    assert by_platform["dzen"].feeds_post_stats is True
    assert by_platform["vk"].feeds_post_stats is True
    telegraph = by_platform["telegraph"]
    assert telegraph.feeds_post_stats is False
    # The table name the reason string prints must be the ORM's real table.
    assert telegraph.snapshot_table == TelegraphMetricSnapshot.__tablename__


# ---------------------------------------------------- coverage derived from it


def test_coverage_membership_equals_the_registry():
    specs = pipeline.collector_job_specs()
    platforms = {platform for spec in specs for platform in spec.platforms}
    assert set(coverage.SCHEDULED_COLLECTORS) == platforms
    assert set(coverage.COLLECTORS) == platforms
    # Every owned collector is on a schedule today: an entry here would mean a
    # module the report knows but `pipeline` never registers.
    assert coverage.ON_DEMAND_COLLECTORS == {}
    session_platforms = {
        target.platform
        for spec in specs
        for target in spec.targets
        if any(gate.kind == "session" for gate in spec.gates_for(target.platform))
    }
    credential_platforms = {
        target.platform
        for spec in specs
        for target in spec.targets
        if any(gate.kind == "credential" for gate in spec.gates_for(target.platform))
    }
    assert set(coverage.SESSION_GATED_COLLECTORS) == session_platforms == {"telegram"}
    assert set(coverage.BLOCKED_REASONS) == credential_platforms


def test_scheduled_wording_names_the_job_ids_and_never_reinvents_them():
    for spec in pipeline.collector_job_specs():
        for platform in spec.platforms:
            wording = coverage.SCHEDULED_COLLECTORS[platform]
            assert spec.job_id in wording
            assert spec.startup_job_id in wording
            assert spec.target(platform).entry in wording


def test_the_telegram_sentence_survived_the_rewrite_verbatim():
    """`s8_analytics/freshness.py` quotes this string in another panel.

    The exact text is what the freshness reason used before P52; the derivation
    must reproduce it, or two panels describe one platform two ways.
    """
    assert coverage.SCHEDULED_COLLECTORS["telegram"] == (
        "задача планировщика `telegram-post-metrics` (и разовая "
        "`startup-telegram-post-metrics`) зовёт tg_sync.collect_post_stats сама, "
        "а вручную - `tgfarm telegram stats` и POST /api/telegram/stats"
    )
    assert coverage.SESSION_GATED_COLLECTORS["telegram"] == (
        "нужна авторизованная пользовательская сессия Telegram: без неё "
        "задача честно отвечает `no_session` и ничего не записывает "
        "(выполните `tgfarm telegram login`)"
    )


# ------------------------------------------------------------ gate derivation


def _ready_gate(gate: pipeline.CollectorGate, *, ready: bool) -> pipeline.CollectorGate:
    return replace(gate, ready=lambda: ready)


def _patched_specs(monkeypatch, *, ready_for):
    """Re-register the specs with each gate's check replaced by a fixed answer.

    `ready_for` maps gate kind -> bool. Patching `collector_job_specs` is enough:
    `metrics_collector_jobs` and the coverage derivation both call it by name,
    so no test depends on the real machine's configuration.
    """
    originals = pipeline.collector_job_specs()

    def patched() -> tuple[pipeline.CollectorJobSpec, ...]:
        out = []
        for spec in originals:
            new = replace(
                spec,
                job_gates=tuple(
                    _ready_gate(gate, ready=ready_for(gate.kind)) for gate in spec.job_gates
                ),
                targets=tuple(
                    replace(
                        target,
                        gates=tuple(
                            _ready_gate(gate, ready=ready_for(gate.kind))
                            for gate in target.gates
                        ),
                    )
                    for target in spec.targets
                ),
            )
            out.append(new)
        return tuple(out)

    monkeypatch.setattr(pipeline, "collector_job_specs", patched)


def test_state_is_the_three_silences_apart(monkeypatch):
    _patched_specs(monkeypatch, ready_for=lambda kind: True)
    assert coverage.collector_state("dzen") == coverage.STATE_RUNNING
    assert coverage.collector_state("telegram") == coverage.STATE_RUNNING
    # Registered and able, but its snapshots never reach post_stats: telegraph
    # stays its own kind of silence.
    assert coverage.collector_state("telegraph") == coverage.STATE_OTHER_TABLE
    assert coverage.collector_state("max") == coverage.STATE_ABSENT

    _patched_specs(monkeypatch, ready_for=lambda kind: kind != "mode")
    assert coverage.collector_state("dzen") == coverage.STATE_GATED
    assert coverage.collector_state("telegram") == coverage.STATE_GATED

    _patched_specs(monkeypatch, ready_for=lambda kind: kind != "session")
    blocked = coverage.blocking_gates("telegram")
    assert [gate.kind for gate in blocked] == ["session"], "must name the session as the stopper"


def test_the_reason_names_the_flag_that_stops_the_job(monkeypatch):
    from datetime import UTC, datetime
    from uuid import uuid4

    _patched_specs(monkeypatch, ready_for=lambda kind: kind not in ("mode",))
    suffix = uuid4().hex[:8]
    channel_id = f"regdzen_{suffix}"
    with session_scope() as session:
        session.add(
            ChannelRow(id=channel_id, title="reg", niche="news", platform="dzen")
        )
        for index in range(2):
            post = PostRow(
                public_id=f"regd{suffix}{index}",
                channel_id=channel_id,
                status="published",
                published_at=datetime.now(UTC),
                hook=f"Заголовок регистрации {index} достаточной длины для теста",
                body="Текст",
            )
            session.add(post)
            session.flush()
            if index == 0:
                session.add(PostStat(post_id=post.id, views=10))

    report = coverage.measurement_coverage()
    row = report["platforms"]["dzen"]
    assert row["status"] == STATUS_PARTIAL
    assert row["collector_state"] == coverage.STATE_GATED
    assert "metrics-and-learning" in row["reason"]
    assert "TGFARM_MODE" in row["reason"], "a gated job must name the stopping flag"


def test_coverage_still_never_writes_after_the_rewrite():
    """The old write test runs against dzen; this one runs against the gates."""
    from datetime import UTC, datetime
    from uuid import uuid4

    suffix = uuid4().hex[:8]
    with session_scope() as session:
        session.add(ChannelRow(id=f"regvw_{suffix}", title="r", niche="news", platform="vk"))
        post = PostRow(
            public_id=f"regvw{suffix}",
            channel_id=f"regvw_{suffix}",
            status="published",
            published_at=datetime.now(UTC),
            hook="Заголовок coverage write-guard достаточной длины для теста",
            body="Текст",
        )
        session.add(post)

    def counts():
        with session_scope() as session:
            return (
                len(session.execute(select(PostRow.id)).all()),
                len(session.execute(select(PostStat.id)).all()),
                len(session.execute(select(CrosspostLog.id)).all()),
            )

    before = counts()
    coverage.measurement_coverage()
    coverage.coverage_table()
    assert counts() == before


def test_app_js_reads_the_state_field_the_report_emits():
    """The panel consumes `collector_state`; the field may never go unread again.

    P51 lesson, kept mechanical: every new payload key names its consumer here or
    in the UI shape-lock suite, and this test is that name - it checks the renderer
    actually reads the key and maps it to chips.
    """
    from pathlib import Path

    script_path = Path(__file__).resolve().parents[1] / "tgfarm" / "ui" / "app.js"
    script = script_path.read_text(encoding="utf-8")
    body_start = script.index("function renderMetricsCoverage(payload)")
    body = script[body_start : script.index("const PUBLICATION_REASON", body_start)]
    assert "row.collector_state" in body, "the renderer no longer reads the derived kind"
    assert "COVERAGE_GAP_BY_STATE" in body
    gap_table_start = script.index("const COVERAGE_GAP = {")
    gap_table = script[gap_table_start:body_start]
    for state in (
        coverage.STATE_ABSENT,
        coverage.STATE_OTHER_TABLE,
        coverage.STATE_GATED,
        coverage.STATE_RUNNING,
    ):
        assert re.search(rf"{state}\s*:", gap_table), f"{state} has no chip in the gap table"
