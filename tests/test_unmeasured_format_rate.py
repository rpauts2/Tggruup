"""A format that was never measured reports no rate, not a zero.

``s8_analytics.formats.format_performance`` counts two things per format — how many
posts were published and how many ever got a ``post_stats`` snapshot — and it
answered the *third* question («what did the measured ones earn») with a fabricated
number whenever that second count was zero:

* ``formats[fmt]["avg_engagement"]`` and ``measurement[fmt]["avg_engagement"]``
  came from ``... if measured else 0.0``, so a format with hundreds of published
  posts and not one snapshot — the ``vk``/``news`` shape of the live base on
  2026-11-05, read with ``mode=ro``: 372 published, 0 measured inside 30 days —
  printed the same ``0.0`` as a format that was measured often and engaged with
  nothing;
* ``measurement[fmt]["fresh_avg_engagement"]`` did the same over the live count, so
  a format whose every snapshot is older than ``FORMAT_EVIDENCE_STALE_AFTER_DAYS``
  claimed a *live* average of zero, and ``s12_learning`` re-fabricated that zero
  into the stored lesson with ``float(evidence.get("fresh_avg_engagement") or 0.0)``.

The file already owned the right helper — ``_rate(total, size)`` answers ``None``
for an empty denominator and says so in its own docstring — and the ``publication``
block has used it since P73. Both legacy averages now go through it too: that is the
P34 rule («absence is not zero») applied to the last fabricated numbers in this
dimension. What did *not* change is any count: ``published``, ``measured``, ``n``,
``fresh``, ``stale``, ``sample_size``, ``reliable``, ``state`` and the whole
``publication`` block mean what they meant, and the P73 gate is untouched.

Two consequences are pinned here rather than discovered late:

* the sort. ``-pair[1]["avg_engagement"]`` over a ``None`` is a ``TypeError``, so
  the key is None-safe and the decision is **unknown last** — a rate that was never
  measured has no rank, not the worst rank, and it must not tie-break against a
  format that earned a real zero by its name;
* the lesson. A stored lesson is the record of measurements, so it keeps a real
  ``0.0`` where snapshots exist and carries ``None`` where the live count is zero.
  A ``None`` there may not disturb ``_relative_quality``, ``_shrink_quality``, the
  note, or ``s4_generation.durable_format_preference``'s refusal.

Hermetic by construction: throwaway platform names, so the platform
``format_performance`` measures contains only these rows; format names no other file
can seed; an autouse fixture that deletes every row this file creates.

"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from tgfarm.db.models import Channel, LearningMemory, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s4_generation import durable_format_preference
from tgfarm.s8_analytics import formats as formats_module
from tgfarm.s8_analytics.formats import (
    EVIDENCE_MIXED,
    EVIDENCE_NEVER,
    EVIDENCE_STALE,
    EVIDENCE_UNRULLED,
    MIN_SAMPLE,
    format_performance,
    planner_history,
)
from tgfarm.s12_learning import learn_format_lessons, lessons

WINDOW_DAYS = 60
STALE_AFTER_DAYS = 7
FRESH = 0.5
DEAD = 30.0

#: The row shape ``/api/metrics/formats`` has always published per format: the keys
#: are a contract, only the value under ``avg_engagement`` became honest.
LEGACY_ROW_KEYS = {"format", "published", "measured", "avg_engagement", "reliable", "n"}

_seeded_channels: list[str] = []
_seeded_keys: list[str] = []


@pytest.fixture(autouse=True)
def _clean_seeded_rows(monkeypatch):
    """Throwaway rows, and a threshold no local ``.env`` can move."""
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


def _name(kind: str) -> str:
    """A platform or format name no other test file, now or later, can seed."""
    return f"{kind}_{uuid4().hex[:8]}".lower()[:32]


def _channel(platform: str) -> str:
    channel_id = f"p81_{uuid4().hex[:10]}"
    _seeded_channels.append(channel_id)
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Пустой замер", niche="tech", platform=platform))
    return channel_id


def _publish(
    channel_id: str,
    fmt: str,
    *,
    engagement: int | None,
    published_days_ago: float = 1.0,
    collected_days_ago: float = FRESH,
) -> None:
    """One published post, snapshotted or not — the whole difference this file is about."""
    with session_scope() as session:
        post = Post(
            public_id=f"p81_{uuid4().hex[:12]}",
            channel_id=channel_id,
            status="published",
            post_format=fmt,
            published_at=datetime.now(UTC) - timedelta(days=published_days_ago),
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


def _seed(
    platform: str,
    fmt: str,
    *,
    measured: int,
    unmeasured: int,
    engagement: int,
    collected_days_ago: float = FRESH,
) -> str:
    """`measured` snapshots and `unmeasured` silent publications of one format."""
    channel = _channel(platform)
    for _ in range(measured):
        _publish(channel, fmt, engagement=engagement, collected_days_ago=collected_days_ago)
    for _ in range(unmeasured):
        _publish(channel, fmt, engagement=None)
    return channel


def _lesson(channel_id: str, platform: str, fmt: str) -> dict:
    key = f"{platform}/{fmt}"
    _seeded_keys.append(key)
    row = next((item for item in lessons(channel_id, kind="format") if item["key"] == key), None)
    assert row is not None, f"no lesson stored for {key}"
    return row


# ------------------------------------------------------- the report: no rate, no lie
def test_published_but_never_measured_format_reports_no_rate():
    """Hundreds published, not one snapshot: the report has no average to give.

    ``published``/``measured``/``n``/``reliable`` keep their old answers — the
    refusal is only about the number the farm never took.
    """
    platform = _name("vk")
    fmt = _name("news")
    _seed(platform, fmt, measured=0, unmeasured=4, engagement=0)

    report = format_performance(WINDOW_DAYS, platform=platform)
    row = report["formats"][fmt]

    assert row["published"] == 4
    assert row["measured"] == 0 and row["n"] == 0
    assert row["avg_engagement"] is None
    assert row["reliable"] is False
    # the freshness block carries the same refusal for both of its averages...
    evidence = report["measurement"][fmt]
    assert evidence["avg_engagement"] is None
    assert evidence["fresh_avg_engagement"] is None
    assert evidence["fresh"] == 0 and evidence["stale"] == 0
    assert evidence["evidence_status"] == EVIDENCE_NEVER
    assert evidence["last_measured_at"] is None
    # ...and the publication block has said `None` here since P73; that is unchanged
    assert evidence["publication"]["sample_size"] == 0
    assert evidence["publication"]["avg_engagement"] is None
    assert evidence["publication"]["fresh_avg_engagement"] is None
    # the counts of the platform as a whole are what they always were
    assert report["published_total"] == 4
    assert report["measured_total"] == 0


def test_measured_format_that_earned_nothing_keeps_a_real_zero():
    """The other half of the rule: a measurement that came out empty IS a zero."""
    platform = _name("dzen")
    earned = _name("zero")
    silent = _name("a_never")
    _seed(platform, earned, measured=3, unmeasured=0, engagement=0)
    _seed(platform, silent, measured=0, unmeasured=3, engagement=0)

    report = format_performance(WINDOW_DAYS, platform=platform)

    assert report["formats"][earned]["measured"] == 3
    assert report["formats"][earned]["avg_engagement"] == 0.0
    assert report["formats"][earned]["reliable"] is True
    assert report["measurement"][earned]["avg_engagement"] == 0.0
    assert report["measurement"][earned]["fresh_avg_engagement"] == 0.0
    assert report["formats"][silent]["measured"] == 0
    assert report["formats"][silent]["avg_engagement"] is None
    assert report["measurement"][silent]["fresh_avg_engagement"] is None


def test_dead_snapshots_leave_a_window_rate_and_no_live_rate():
    """Measured, but every snapshot is behind the threshold.

    The first question («was it measured») still has a number; the second one
    («is it still live») has no live posts at all, and answering it with ``0.0``
    was a claim about the live half, not a refusal.
    """
    platform = _name("telegram")
    fmt = _name("stat")
    _seed(platform, fmt, measured=3, unmeasured=1, engagement=60, collected_days_ago=DEAD)

    report = format_performance(WINDOW_DAYS, platform=platform)
    row = report["formats"][fmt]
    evidence = report["measurement"][fmt]

    assert row["measured"] == 3 and row["published"] == 4
    assert row["avg_engagement"] == 60.0
    assert row["reliable"] is True and evidence["fresh_reliable"] is False
    assert evidence["fresh"] == 0 and evidence["stale"] == 3
    assert evidence["evidence_status"] == EVIDENCE_STALE
    assert evidence["avg_engagement"] == 60.0
    assert evidence["fresh_avg_engagement"] is None
    assert evidence["publication"]["fresh_sample_size"] == 0
    assert evidence["publication"]["fresh_avg_engagement"] is None
    assert evidence["last_measured_at"] is not None
    assert evidence["fresh_last_measured_at"] is None


def test_mixed_evidence_still_carries_its_real_live_average():
    """Nothing was loosened: with live snapshots the live average is a number."""
    platform = _name("pikabu")
    fmt = _name("flash")
    _seed(platform, fmt, measured=2, unmeasured=0, engagement=40, collected_days_ago=FRESH)
    _seed(platform, fmt, measured=2, unmeasured=0, engagement=4, collected_days_ago=DEAD)

    report = format_performance(WINDOW_DAYS, platform=platform)
    evidence = report["measurement"][fmt]

    assert evidence["evidence_status"] == EVIDENCE_MIXED
    assert evidence["fresh"] == 2 and evidence["stale"] == 2
    assert evidence["avg_engagement"] == 22.0
    assert evidence["fresh_avg_engagement"] == 40.0


# ------------------------------------------------------------------ the sort order
def test_a_format_without_a_rate_sorts_last_and_not_against_a_real_zero():
    """The documented decision: unknown last, and a real zero keeps its rank.

    Under the fabricated zero the unmeasured format averaged ``0.0`` as well, so it
    tied with the format that earned nothing and won the tie on its own name —
    a format nobody measured out-ranked a measured one. Now the measured zero comes
    second and the unknown third, whatever the names say.
    """
    platform = _name("ok")
    strong = _name("h_strong")
    earned_zero = _name("m_zero")
    never = _name("a_never")
    _seed(platform, never, measured=0, unmeasured=2, engagement=0)
    _seed(platform, earned_zero, measured=3, unmeasured=0, engagement=0)
    _seed(platform, strong, measured=3, unmeasured=0, engagement=30)

    report = format_performance(WINDOW_DAYS, platform=platform)

    assert list(report["formats"]) == [strong, earned_zero, never]
    # the freshness block walks the same order, as its comment promises
    assert list(report["measurement"]) == [strong, earned_zero, never]
    # and a readout that takes the first trustworthy row can never take the tail
    best = formats_module.best_format(days=WINDOW_DAYS)
    assert best is not None and best["avg_engagement"] is not None
    assert best["measured"] >= MIN_SAMPLE


# ------------------------------------------------------------ the planner feed
def test_planner_history_hands_the_planner_no_rate_for_an_unmeasured_format():
    """``planner_history`` already refused an empty sample; this pins that it still does.

    ``avg_engagement: None`` beside ``measured: 0`` is exactly the pair that keeps
    ``s10_format._as_engagement`` returning ``None`` and the format out of a plan.
    """
    platform = _name("reddit")
    never = _name("news")
    dead = _name("stat")
    _seed(platform, never, measured=0, unmeasured=2, engagement=0)
    _seed(platform, dead, measured=3, unmeasured=0, engagement=999, collected_days_ago=DEAD)

    history = planner_history(platform, days=WINDOW_DAYS)

    assert history[never]["measured"] == 0
    assert history[never]["published"] == 2
    assert history[never]["avg_engagement"] is None
    assert history[never]["sample_size"] == 0
    assert history[never]["evidence_status"] == EVIDENCE_NEVER
    # an unknown platform has no publication rule, so nothing is disproved here
    assert history[never]["publication_evidence"] == EVIDENCE_UNRULLED
    # measured but dead: no live sample, so no rate, and the dead count is named
    assert history[dead]["measured"] == 0 and history[dead]["stale_measured"] == 3
    assert history[dead]["avg_engagement"] is None


# --------------------------------------------------------------- the lesson write
def test_an_unmeasured_format_writes_no_lesson_and_is_named_as_a_blind_spot():
    platform = _name("vk")
    fmt = _name("analysis")
    channel = _seed(platform, fmt, measured=0, unmeasured=6, engagement=0)

    report = learn_format_lessons(channel, days=WINDOW_DAYS)

    assert report["written"] == 0
    assert report["not_measured"] == [fmt]
    assert report["formats"][fmt]["state"] == "not_measured"
    assert report["formats"][fmt]["published"] == 6
    assert lessons(channel, kind="format") == []
    assert report["measured_total"] == 0


def test_a_lesson_of_dead_snapshots_carries_no_live_average():
    """The trap the lesson writer kept: ``float(None or 0.0)`` stored a zero.

    The row still records the three measurements it has, and every reader that
    needs a number still gets one — the live average is the only absent value.
    """
    platform = _name("dzen")
    fmt = _name("news")
    channel = _seed(platform, fmt, measured=3, unmeasured=0, engagement=60, collected_days_ago=DEAD)

    run = learn_format_lessons(channel, days=WINDOW_DAYS)
    row = _lesson(channel, platform, fmt)
    value = dict(row["value"] or {})

    assert value["measured"] == 3 and value["published"] == 3
    assert value["avg_engagement"] == 60.0
    assert value["fresh_measured"] == 0 and value["stale_measured"] == 3
    assert value["fresh_avg_engagement"] is None
    assert value["evidence_status"] == EVIDENCE_STALE
    assert value["state"] == "reliable" and value["reliable"] is True
    # the values the score is built from stayed numbers, not `None`
    assert isinstance(value["relative_quality"], float)
    assert isinstance(value["quality"], float)
    assert 0.0 <= value["quality"] <= 1.0
    assert isinstance(value["baseline_engagement"], float)
    # the note states the absence in words and quotes no live average
    note = row["note"] or ""
    assert "не живое доказательство" in note
    assert "старше 7" in note
    # the stored JSON round-trips the null
    assert json.loads(json.dumps(value))["fresh_avg_engagement"] is None
    # and the durable reader refuses the row instead of tripping over it
    preference = durable_format_preference(channel, platform)
    assert preference["state"] == "stale_evidence"
    assert preference["history"] == {}
    assert preference["live_sample_size"] == 0
    assert run["written"] == 1


def test_a_mixed_lesson_keeps_its_real_live_average():
    """A live snapshot exists, so the lesson must carry its average, not ``None``."""
    platform = _name("telegram")
    fmt = _name("stat")
    _seed(platform, fmt, measured=2, unmeasured=0, engagement=40)
    channel = _seed(platform, fmt, measured=2, unmeasured=0, engagement=4, collected_days_ago=DEAD)

    learn_format_lessons(channel, days=WINDOW_DAYS)
    value = dict(_lesson(channel, platform, fmt)["value"] or {})

    assert value["measured"] == 4
    assert value["avg_engagement"] == 22.0
    assert value["fresh_measured"] == 2
    assert value["fresh_avg_engagement"] == 40.0
    assert value["evidence_status"] == EVIDENCE_MIXED


def test_an_unmeasured_format_does_not_dilute_the_platform_baseline():
    """Five silent posts cannot move ``20.0``: the denominator was never the question.

    ``baseline_engagement`` is the mean over measured posts. The unmeasured format
    contributed a fabricated ``0.0`` multiplied by its own ``measured: 0`` before
    this change and contributes nothing after it, so the value is identical — which
    is what «removing a fabricated number, not redefining a denominator» means.
    """
    platform = _name("vk")
    measured = _name("news")
    silent = _name("stat")
    channel = _channel(platform)
    for value in (10, 20, 30):
        _publish(channel, measured, engagement=value)
    _seed(platform, silent, measured=0, unmeasured=5, engagement=0)

    report = learn_format_lessons(channel, days=WINDOW_DAYS)
    value = dict(_lesson(channel, platform, measured)["value"] or {})
    window_average = format_performance(WINDOW_DAYS, platform=platform)

    assert window_average["formats"][measured]["avg_engagement"] == 20.0
    assert value["baseline_engagement"] == 20.0
    assert value["relative_quality"] == pytest.approx(0.5)
    assert report["baseline_engagement"] == 20.0
    assert report["measured_total"] == 3
    assert report["published_total"] == 8
    assert report["not_measured"] == [silent]
    assert report["formats"][silent]["state"] == "not_measured"


# ------------------------------------------------------------------ the HTTP edge
def test_the_formats_endpoint_sends_null_for_a_format_it_never_measured():
    """The panel's contract is the report's: absence crosses HTTP as ``null``.

    ``tgfarm/api.py`` returns ``format_performance`` verbatim, so a ``0.0`` here is
    a printed ``0.0`` in the card. The UI line that reads ``row.avg_engagement ||
    0`` belongs to the coordinator, not to this block.
    """
    from fastapi.testclient import TestClient

    from tgfarm.api import create_app
    from tgfarm.core.apiauth import ensure_token

    platform = _name("pikabu")
    fmt = _name("news")
    # a delta over what the unscoped report already held for this format name:
    # `format_performance` measures a platform, not a channel, and the session
    # database is shared with every other test file
    before = format_performance(30)["formats"].get(fmt, {}).get("published", 0)
    _seed(platform, fmt, measured=0, unmeasured=3, engagement=0)

    with TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    ) as client:
        body = client.get("/api/metrics/formats", params={"days": 30}).json()

    row = body["formats"][fmt]
    assert row["published"] == before + 3
    assert row["measured"] == 0
    assert row["avg_engagement"] is None
    assert body["measurement"][fmt]["fresh_avg_engagement"] is None
    assert set(row) == LEGACY_ROW_KEYS
