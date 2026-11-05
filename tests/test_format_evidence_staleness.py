"""Measured is not the same question as *still live* (evidence staleness).

`FORMAT_PERFORMANCE_PLANNER` lets a measured format outrank the rules, and
`FORMAT_PERFORMANCE_WINDOW_DAYS` filters by *post* time — so nothing asked when
the measurement behind a post was taken. Live, `post_stats` was last written
2026-10-29 while the window is 60 days wide: the planner would have ranked
formats from dead snapshots, and S12 would have *persisted* that ranking into
`LearningMemory`, where it keeps steering after the data ages further.

The project already refuses unmeasured posts as proof (`measured`, `reliable`,
`{"measured": 0, "published": 50}` = zero proof). These tests pin the second
refusal, in the vocabulary `s8_analytics.freshness` already uses (ok / stale /
never, plus `mixed` for a split inside one format):

* a stale measurement is still reported — it is never silently deleted;
* it never moves a format choice: the plan falls back to the previous strategy
  (rules), not to a fabricated priority;
* fresh evidence behaves exactly as it did before this gate existed;
* `FORMAT_PERFORMANCE_PLANNER=false` is byte-identical, staleness or not;
* the report rows the panel reads keep the keys they have always had.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from tgfarm.config import Persona
from tgfarm.core.schemas import Cluster, ItemScored
from tgfarm.db.models import Channel, LearningMemory, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s4_generation import durable_format_preference, format_plan_for
from tgfarm.s8_analytics import formats as formats_module
from tgfarm.s10_format import LENGTH_BUDGET, MIN_HISTORY_SAMPLE, plan_for_cluster, plan_with_history
from tgfarm.s12_learning import best_format_lesson, learn_format_lessons, lessons

RU = Persona(id="ru", title="Канал", niche="business", min_chars=150, max_chars=700)
TOPIC = "ЦБ сохранил ставку на уровне 19%"

#: The report rows the panel and `/api/metrics/formats` have always published.
LEGACY_ROW_KEYS = {"format", "published", "measured", "avg_engagement", "reliable", "n"}
LEGACY_REPORT_KEYS = {
    "window_days", "since", "platform", "formats",
    "measured_total", "published_total", "min_sample",
}

#: Keys this wave added. Purely additive: an assertion below pins that nothing
#: was renamed or redefined.
NEW_REPORT_KEYS = {
    "measurement", "fresh_measured_total", "stale_measured_total",
    "evidence_status", "stale_after_days",
}

WINDOW_DAYS = 60
STALE_AFTER_DAYS = 7
FRESH = 0.5
DEAD = 30.0

_seeded_channels: list[str] = []
_seeded_keys: list[str] = []


def _unique(platform: str) -> str:
    """A platform name nobody else's fixture writes to (the suite shares one DB)."""
    return f"{platform}_{uuid4().hex[:8]}".lower()[:32]


@pytest.fixture(autouse=True)
def _clean_and_pin_threshold(monkeypatch):
    """Throwaway rows, and a threshold that no local `.env` can move."""
    monkeypatch.setattr(formats_module, "_configured_stale_after_days", lambda: STALE_AFTER_DAYS)
    yield
    with session_scope() as session:
        session.execute(delete(LearningMemory).where(LearningMemory.key.in_(_seeded_keys)))
        session.execute(
            delete(LearningMemory).where(LearningMemory.channel_id.in_(_seeded_channels))
        )
        post_ids = list(
            session.scalars(select(Post.id).where(Post.channel_id.in_(_seeded_channels))).all()
        )
        if post_ids:
            session.execute(delete(PostStat).where(PostStat.post_id.in_(post_ids)))
            session.execute(delete(Post).where(Post.id.in_(post_ids)))
        session.execute(delete(Channel).where(Channel.id.in_(_seeded_channels)))
    _seeded_channels.clear()
    _seeded_keys.clear()


def _channel(platform: str) -> str:
    channel_id = f"ev_{uuid4().hex[:8]}"
    _seeded_channels.append(channel_id)
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Доказательства", niche="business", platform=platform))
    return channel_id


def _publish(
    channel_id: str,
    fmt: str,
    *,
    engagement: int | None,
    days_ago: float = 1.0,
    collected_days_ago: float = FRESH,
) -> None:
    """A post published `days_ago`, snapshotted `collected_days_ago`.

    The two ages are deliberately separate arguments: the window filters by the
    first, and the whole point of this wave is that the second decides whether the
    row is evidence at all.
    """
    with session_scope() as session:
        post = Post(
            public_id=f"ev_{uuid4().hex[:10]}",
            channel_id=channel_id,
            status="published",
            post_format=fmt,
            published_at=datetime.now(UTC) - timedelta(days=days_ago),
            hook="Заголовок",
            body="Текст",
        )
        session.add(post)
        session.flush()
        if engagement is not None:
            session.add(
                PostStat(
                    post_id=post.id,
                    reactions=engagement,
                    comments=0,
                    forwards=0,
                    collected_at=datetime.now(UTC) - timedelta(days=collected_days_ago),
                )
            )


def _cluster(n_items: int = 3) -> Cluster:
    return Cluster(
        id=f"cl-{uuid4().hex[:6]}",
        label=TOPIC,
        items=[
            ItemScored(
                source_id=f"s{i}",
                external_id=f"x{i}-{uuid4().hex[:6]}",
                url=f"https://example.invalid/s{i}/{uuid4().hex[:6]}",
                title=f"Заголовок новости номер {i}",
                text="текст источника " * 60,
                media_kind=None,
            )
            for i in range(n_items)
        ],
        heat=0.1,
    )


def _plan(channel_id: str, platform: str, cluster: Cluster) -> SimpleNamespace:
    return SimpleNamespace(
        persona=RU, channel_id=channel_id, cluster_public_id=cluster.id, platform=platform
    )


def _choose(channel_id: str, platform: str, cluster: Cluster, history, *, flag: bool):
    return format_plan_for(
        _plan(channel_id, platform, cluster),
        cluster_view=cluster,
        topic=TOPIC,
        media_kinds=set(),
        has_video=False,
        history=history,
        use_performance=flag,
    )


def _alternative_to(base_fmt: str) -> str:
    for fmt in ("stat", "flash", "news", "analysis"):
        if fmt != base_fmt and LENGTH_BUDGET[fmt][1] <= RU.max_chars:
            return fmt
    raise AssertionError("no alternative format fits this persona")


def _stored_value(channel_id: str, platform: str, fmt: str) -> dict:
    key = f"{platform}/{fmt}"
    _seeded_keys.append(key)
    row = next(
        (item for item in lessons(channel_id, kind="format") if item["key"] == key), None
    )
    assert row is not None, f"no lesson stored for {key}"
    return dict(row["value"] or {})


# ------------------------------------------------------- the report tells both truths
def test_dead_snapshot_is_measured_but_not_live_evidence():
    platform = _unique("vk")
    channel = _channel(platform)
    for _ in range(3):
        _publish(channel, "stat", engagement=80, collected_days_ago=DEAD)

    report = formats_module.format_performance(WINDOW_DAYS, platform=platform)
    row = report["formats"]["stat"]
    evidence = report["measurement"]["stat"]

    # «was it ever measured» keeps its old answer — the panel would otherwise
    # silently lose 3 measured posts.
    assert row["measured"] == 3 and row["n"] == 3 and row["reliable"] is True
    assert row["avg_engagement"] == 80.0
    # and the new question is answered next to it, not instead of it.
    assert evidence["fresh"] == 0 and evidence["stale"] == 3
    assert evidence["evidence_status"] == "stale"
    # No live snapshot means no live average — not a live average of zero, which is
    # what this line used to print. The three measurements themselves are still
    # there, named by `measured`/`stale` and by `row["avg_engagement"] == 80.0`
    # above: the report refuses the second question without lying about the first.
    assert evidence["fresh_avg_engagement"] is None
    assert evidence["last_measured_at"] is not None
    assert evidence["fresh_last_measured_at"] is None
    assert report["stale_after_days"] == STALE_AFTER_DAYS
    assert report["fresh_measured_total"] == 0
    assert report["stale_measured_total"] == 3
    assert report["evidence_status"] == "stale"


def test_live_snapshot_is_reported_ok_and_counts_the_same():
    platform = _unique("dzen")
    channel = _channel(platform)
    for _ in range(3):
        _publish(channel, "flash", engagement=12, collected_days_ago=FRESH)

    report = formats_module.format_performance(WINDOW_DAYS, platform=platform)
    evidence = report["measurement"]["flash"]

    assert evidence["evidence_status"] == "ok"
    assert evidence["fresh"] == report["formats"]["flash"]["measured"] == 3
    assert evidence["stale"] == 0
    # nothing was left out, so the live average *is* the reported average.
    assert evidence["fresh_avg_engagement"] == report["formats"]["flash"]["avg_engagement"]
    assert report["evidence_status"] == "ok"


def test_mixed_evidence_is_named_mixed_and_not_blended_into_one_number():
    platform = _unique("reddit")
    channel = _channel(platform)
    for _ in range(3):
        _publish(channel, "news", engagement=90, collected_days_ago=FRESH)
    for _ in range(2):
        _publish(channel, "news", engagement=5, collected_days_ago=DEAD)

    report = formats_module.format_performance(WINDOW_DAYS, platform=platform)
    evidence = report["measurement"]["news"]

    assert evidence["evidence_status"] == "mixed"
    assert evidence["fresh"] == 3 and evidence["stale"] == 2
    # the legacy number stays the honest blend of everything measured...
    assert report["formats"]["news"]["avg_engagement"] == pytest.approx(56.0)
    # ...and the live number is the live half alone, so a decision cannot be
    # dragged toward the dead snapshots.
    assert evidence["fresh_avg_engagement"] == pytest.approx(90.0)
    assert report["evidence_status"] == "mixed"


def test_the_threshold_is_what_separates_live_from_dead():
    """Nothing is hardcoded: the same 6-day-old snapshots are live or dead by knob.

    `post_stats.collected_at` is NOT NULL in this schema, so the missing-timestamp
    branch in `format_performance` is defensive and cannot be reached from the DB;
    what can be reached is the age test, and it follows the setting exactly.
    """
    platform = _unique("telegram")
    channel = _channel(platform)
    for _ in range(3):
        _publish(channel, "stat", engagement=70, collected_days_ago=6)

    live = formats_module.format_performance(WINDOW_DAYS, platform=platform)
    tight = formats_module.format_performance(WINDOW_DAYS, platform=platform, stale_after_days=5)

    assert live["stale_after_days"] == STALE_AFTER_DAYS
    assert live["measurement"]["stat"]["evidence_status"] == "ok"
    assert tight["stale_after_days"] == 5
    assert tight["measurement"]["stat"]["evidence_status"] == "stale"
    # both agree on what was measured; only the veto differs
    assert tight["formats"]["stat"]["measured"] == live["formats"]["stat"]["measured"] == 3
    assert formats_module.planner_history(platform, days=WINDOW_DAYS)["stat"]["measured"] == 3
    assert formats_module.planner_history(
        platform, days=WINDOW_DAYS, stale_after_days=5
    )["stat"]["measured"] == 0


# --------------------------------------------------- the planner gate (S8 feed → S10)
def test_stale_evidence_does_not_move_the_format():
    platform = _unique("vk")
    channel = _channel(platform)
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    strong = _alternative_to(base.fmt)
    for _ in range(5):
        _publish(channel, strong, engagement=999, collected_days_ago=DEAD)

    history = formats_module.planner_history(platform, days=WINDOW_DAYS)
    chosen = _choose(channel, platform, cluster, history, flag=True)

    assert history[strong]["measured"] == 0
    assert history[strong]["stale_measured"] == 5
    assert history[strong]["reliable"] is False
    assert chosen.fmt == base.fmt
    assert "старше порога" in chosen.reason
    assert "история:" not in chosen.reason


def test_fresh_evidence_moves_the_format_as_it_always_did():
    platform = _unique("dzen")
    channel = _channel(platform)
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    strong = _alternative_to(base.fmt)
    for _ in range(5):
        _publish(channel, strong, engagement=40, collected_days_ago=FRESH)

    history = formats_module.planner_history(platform, days=WINDOW_DAYS)
    chosen = _choose(channel, platform, cluster, history, flag=True)

    assert history[strong]["measured"] == 5
    assert chosen.fmt == strong
    assert "история" in chosen.reason
    assert "старше порога" not in chosen.reason


def test_a_fresh_window_is_identical_with_or_without_the_new_keys():
    """The gate changes nothing when the evidence is live.

    The same evidence handed over in the old four-key shape and in the new
    self-declaring shape must produce the same decision, reason and confidence —
    otherwise this would be a second planning strategy, not a veto.
    """
    platform = _unique("pikabu")
    channel = _channel(platform)
    cluster = _cluster()
    strong = _alternative_to(plan_for_cluster(cluster, RU).fmt)
    for _ in range(4):
        _publish(channel, strong, engagement=33, collected_days_ago=FRESH)

    gated = formats_module.planner_history(platform, days=WINDOW_DAYS)
    legacy = {
        fmt: {
            "measured": row["measured"],
            "published": row["published"],
            "avg_engagement": row["avg_engagement"],
            "reliable": row["reliable"],
        }
        for fmt, row in gated.items()
    }
    kwargs = {"platform_id": platform, "media_kinds": set(), "topic": TOPIC, "cluster": cluster}
    with_markers = plan_with_history(RU, history=gated, **kwargs)
    without_markers = plan_with_history(RU, history=legacy, **kwargs)

    assert with_markers["format"] == without_markers["format"] == strong
    assert with_markers["source"] == without_markers["source"] == "history"
    assert with_markers["reason"] == without_markers["reason"]
    assert with_markers["confidence"] == without_markers["confidence"]
    assert with_markers["min_chars"] == without_markers["min_chars"]
    assert with_markers["max_chars"] == without_markers["max_chars"]
    # the panel additionally learns that nothing was vetoed; the decision is the same
    assert with_markers["history"][strong]["evidence_status"] == "ok"
    assert "evidence_status" not in without_markers["history"][strong]


def test_the_planner_itself_vetoes_a_row_that_declares_its_data_dead():
    """The gate lives in the planner, not only in the feed that normally sets it.

    Any future caller that builds history by hand cannot smuggle dead evidence in
    by forgetting the fresh-only projection.
    """
    dead = {"flash": {"measured": 9, "published": 9, "avg_engagement": 99.0,
                      "evidence_status": "stale", "stale_measured": 9}}
    alive = {"flash": {"measured": 9, "published": 9, "avg_engagement": 99.0,
                       "evidence_status": "ok", "stale_measured": 0}}
    simple = {"flash": {"measured": 9, "published": 9, "avg_engagement": 99.0, "stale": True}}
    silent = {"flash": {"measured": 9, "published": 9, "avg_engagement": 99.0}}

    vetoed = plan_with_history(RU, platform_id="telegram", media_kinds=[], topic=TOPIC, history=dead)
    assert vetoed["format"] != "flash"
    assert vetoed["source"] == "base"
    assert "старше порога" in vetoed["reason"]
    assert vetoed["history"]["flash"]["evidence_status"] == "stale"
    assert vetoed["history"]["flash"]["stale_measured"] == 9

    # `stale: True` is honoured too, and a row that says nothing is the caller's
    # responsibility exactly as before.
    assert plan_with_history(
        RU, platform_id="telegram", media_kinds=[], topic=TOPIC, history=simple
    )["format"] != "flash"
    assert plan_with_history(
        RU, platform_id="telegram", media_kinds=[], topic=TOPIC, history=alive
    )["format"] == "flash"
    assert plan_with_history(
        RU, platform_id="telegram", media_kinds=[], topic=TOPIC, history=silent
    )["format"] == "flash"


def test_mixed_evidence_steers_on_its_live_half_only():
    platform = _unique("facebook")
    channel = _channel(platform)
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    strong = _alternative_to(base.fmt)
    for _ in range(3):
        _publish(channel, strong, engagement=999, collected_days_ago=FRESH)
    for _ in range(4):
        _publish(channel, strong, engagement=1, collected_days_ago=DEAD)

    history = formats_module.planner_history(platform, days=WINDOW_DAYS)
    chosen = _choose(channel, platform, cluster, history, flag=True)

    assert history[strong]["evidence_status"] == "mixed"
    assert history[strong]["measured"] == 3
    assert history[strong]["stale_measured"] == 4
    assert history[strong]["avg_engagement"] == pytest.approx(999.0)
    assert chosen.fmt == strong
    # Confidence comes from the three live measurements, not from all seven: seven
    # would be 0.5 + 0.05*7 = 0.85, three is 0.65.
    assert chosen.confidence == pytest.approx(0.65)


def test_live_sample_below_the_threshold_still_does_not_decide():
    """Nothing was loosened: two live posts are still no evidence."""
    platform = _unique("x")
    channel = _channel(platform)
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    strong = _alternative_to(base.fmt)
    for _ in range(2):
        _publish(channel, strong, engagement=999, collected_days_ago=FRESH)

    chosen = _choose(
        channel, platform, cluster, formats_module.planner_history(platform, days=WINDOW_DAYS),
        flag=True,
    )

    assert chosen.fmt == base.fmt
    assert MIN_HISTORY_SAMPLE == 3


# ------------------------------------------------------------ the flag stays inert
def test_flag_off_is_byte_identical_even_when_the_history_is_dead(monkeypatch):
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    dead_history = {
        "stat": {"measured": 0, "published": 40, "avg_engagement": 0.0, "evidence_status": "stale"}
    }

    def boom(*args, **kwargs):  # noqa: ANN001, ARG001 - must never be reached
        raise AssertionError("the durable lesson must not be consulted with the flag off")

    monkeypatch.setattr("tgfarm.s4_generation.durable_format_preference", boom)
    off = _choose("ch_vk", "vk", cluster, dead_history, flag=False)

    assert (off.fmt, off.min_chars, off.max_chars, off.confidence) == (
        base.fmt,
        base.min_chars,
        base.max_chars,
        base.confidence,
    )
    assert off.reason == base.reason


def test_formats_endpoint_rows_keep_their_published_keys():
    """The panel reads `/api/metrics/formats`; its keys are a contract."""
    from fastapi.testclient import TestClient

    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    ) as client:
        response = client.get("/api/metrics/formats", params={"days": WINDOW_DAYS})
        assert response.status_code == 200
        body = response.json()

    assert set(body) >= LEGACY_REPORT_KEYS
    assert set(body) >= NEW_REPORT_KEYS
    for row in body["formats"].values():
        assert set(row) == LEGACY_ROW_KEYS


def test_report_rows_are_the_legacy_shape_even_for_dead_evidence():
    platform = _unique("telegram")
    channel = _channel(platform)
    _publish(channel, "digest", engagement=7, collected_days_ago=DEAD)
    _publish(channel, "digest", engagement=None)

    report = formats_module.format_performance(WINDOW_DAYS, platform=platform)

    assert set(report) >= LEGACY_REPORT_KEYS
    assert set(report["formats"]["digest"]) == LEGACY_ROW_KEYS
    assert report["formats"]["digest"] == {
        "format": "digest",
        "published": 2,
        "measured": 1,
        "avg_engagement": 7.0,
        "reliable": False,
        "n": 1,
    }


# ----------------------------------------------- the durable lesson path (S12 → S4)
def test_lesson_from_dead_evidence_is_written_and_marked_not_trusted():
    """Chosen policy: record it, refused by the reader — not deleted.

    Dropping the row would erase the blind spot from the one store whose job is to
    remember blind spots; marking it lets a human read «measured 3, all dead».
    """
    platform = _unique("vk")
    channel = _channel(platform)
    for _ in range(3):
        _publish(channel, "stat", engagement=60, collected_days_ago=DEAD)

    report = learn_format_lessons(channel, days=WINDOW_DAYS)
    value = _stored_value(channel, platform, "stat")

    assert report["written"] == 1
    assert report["evidence_status"] == "stale"
    assert report["stale_measured_total"] == 3
    assert report["formats"]["stat"]["evidence_status"] == "stale"
    # the legacy meaning of the row is untouched...
    assert value["measured"] == 3 and value["state"] == "reliable" and value["reliable"] is True
    # ...and the live view is stated next to it
    assert value["fresh_measured"] == 0 and value["stale_measured"] == 3
    assert value["evidence_status"] == "stale"
    assert value["evidence_stale_after_days"] == STALE_AFTER_DAYS
    # and the note says plainly that a plan is not built on it
    row = next(item for item in lessons(channel, kind="format") if item["key"] == f"{platform}/stat")
    assert "не живое доказательство" in (row["note"] or "")

    # The lesson is still a record, so `best_format_lesson` may return it; the
    # refusal is the planner's, and it is explicit.
    assert best_format_lesson(channel, platform=platform) is not None
    preference = durable_format_preference(channel, platform)
    assert preference["state"] == "stale_evidence"
    assert preference["history"] == {}
    assert preference["live_sample_size"] == 0
    assert "старше" in preference["detail"]


def test_dead_lesson_falls_back_to_the_rules_not_a_fabricated_priority():
    platform = _unique("dzen")
    channel = _channel(platform)
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    for _ in range(4):
        _publish(channel, "analysis", engagement=999, collected_days_ago=DEAD)
    learn_format_lessons(channel, days=WINDOW_DAYS)

    chosen = _choose(channel, platform, cluster, {}, flag=True)

    assert chosen.fmt == base.fmt
    assert "только правила" in chosen.reason
    assert "живых измерений нет" in chosen.reason
    assert "история" not in chosen.reason


def test_mixed_lesson_steers_on_its_live_measurements():
    platform = _unique("linkedin")
    channel = _channel(platform)
    cluster = _cluster()
    base = plan_for_cluster(cluster, RU)
    strong = _alternative_to(base.fmt)
    for _ in range(3):
        _publish(channel, strong, engagement=999, collected_days_ago=FRESH)
    for _ in range(2):
        _publish(channel, strong, engagement=1, collected_days_ago=DEAD)
    learn_format_lessons(channel, days=WINDOW_DAYS)

    preference = durable_format_preference(channel, platform)

    assert preference["state"] == "trusted"
    assert preference["format"] == strong
    row = preference["history"][strong]
    assert row["measured"] == 3
    assert row["avg_engagement"] == pytest.approx(999.0)
    assert row["evidence_status"] == "mixed"
    assert row["stale_measured"] == 2
    assert "живых 3 из 5" in preference["detail"]

    chosen = _choose(channel, platform, cluster, {}, flag=True)
    assert chosen.fmt == strong
    assert "999" in chosen.reason
    assert "600.4" not in chosen.reason  # the blended average must not travel


def test_dead_measurements_cannot_top_up_a_lessons_sample():
    """2 live + 3 dead is two measurements, not five."""
    platform = _unique("reddit")
    channel = _channel(platform)
    strong = "stat"
    for _ in range(2):
        _publish(channel, strong, engagement=999, collected_days_ago=FRESH)
    for _ in range(3):
        _publish(channel, strong, engagement=999, collected_days_ago=DEAD)
    learn_format_lessons(channel, days=WINDOW_DAYS)

    preference = durable_format_preference(channel, platform)

    assert preference["state"] == "unusable"
    assert preference["history"] == {}
    assert "опирается на 2 измерений" in preference["detail"]


def test_a_lesson_that_declares_nothing_keeps_its_old_rights():
    """Rows written before this wave have no marker, and must not change today.

    The next learning cycle rewrites them with the marker, so the gate closes on
    its own without editing stored memory behind the operator's back.
    """
    platform = _unique("threads")
    channel = _channel(platform)
    for _ in range(3):
        _publish(channel, "news", engagement=50, collected_days_ago=FRESH)
    learn_format_lessons(channel, days=WINDOW_DAYS)
    with session_scope() as session:
        row = session.execute(
            select(LearningMemory).where(
                LearningMemory.channel_id == channel, LearningMemory.kind == "format"
            )
        ).scalar_one()
        value = dict(row.value or {})
        for key in ("evidence_status", "fresh_measured", "stale_measured", "fresh_avg_engagement"):
            value.pop(key, None)
        row.value = value

    preference = durable_format_preference(channel, platform)

    assert preference["state"] == "trusted"
    assert preference["history"]["news"]["measured"] == 3
    assert "evidence_status" not in preference["history"]["news"]


# ------------------------------------------------------------------- the new knob
def test_the_threshold_is_a_settings_knob_not_a_magic_number():
    from tgfarm.core.settings import Settings

    field = Settings.model_fields["format_evidence_stale_after_days"]

    assert field.alias == "FORMAT_EVIDENCE_STALE_AFTER_DAYS"
    # default 7 days: off the planner path entirely, so it cannot move a plan
    # today while FORMAT_PERFORMANCE_PLANNER stays false.
    assert field.default == 7
    assert formats_module.evidence_stale_after_days(90) == 90
    assert formats_module.evidence_stale_after_days(0) == 1
    assert formats_module.evidence_stale_after_days(10_000) == 365
    assert formats_module.DEFAULT_EVIDENCE_STALE_AFTER_DAYS == field.default


def test_the_untrusted_states_are_the_freshness_vocabulary():
    assert formats_module.EVIDENCE_UNTRUSTED == frozenset({"stale", "never", "blocked"})
    assert formats_module.evidence_status_for(1, 0) == "ok"
    assert formats_module.evidence_status_for(1, 1) == "mixed"
    assert formats_module.evidence_status_for(0, 1) == "stale"
    assert formats_module.evidence_status_for(0, 0) == "never"
