"""The backlog card answers «что происходит с очередью» from three clocks.

Live measurement (2026-11-05, read-only copy of `tgfarm.db`) of why the old
question was unanswerable: Дзен had created 5358 posts in a week, rejected 4710
and published 14, with 365 `failed` rows carrying 1890 retries between them; VK
held 267 `scheduled` posts whose bookings were mostly in the future. A query
against `status = 'scheduled'` — the obvious one — reads that forward buffer as
four idle days, and the count of posts waiting was nowhere at all.

The tests below are written for the one constraint this suite shares: `conftest`
gives every test file the same session database, so a card that groups rows by
`channels.platform` is measured per platform, never in absolute totals. Each test
seeds channels on a platform name it invented, asserts inside that group, and
deletes its rows on the way out. Relations that span groups (`totals` versus the
sum of the platforms) are asserted as relations, not as numbers.

The clock is injected (`_backlog(now=…)`) rather than read from the machine, so
an age band means the same thing on every run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from tgfarm import api
from tgfarm.db.models import Channel, ChannelStat, Post
from tgfarm.db.session import session_scope

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)

_seeded_channels: list[str] = []
_seeded_public_ids: list[str] = []


def _hours(delta: float) -> datetime:
    """A stamp `delta` hours from the injected clock; negative is in the past."""
    return NOW + timedelta(hours=delta)


def _channel(*, platform: str, enabled: bool = True) -> str:
    channel_id = f"blc_{uuid4().hex[:10]}"
    _seeded_channels.append(channel_id)
    with session_scope() as session:
        session.add(
            Channel(
                id=channel_id,
                title="Backlog test",
                niche="test",
                platform=platform,
                enabled=enabled,
            )
        )
    return channel_id


def _post(
    channel_id: str,
    status: str = "draft",
    *,
    created_h: float | None = None,
    booked_h: float | None = None,
    published_h: float | None = None,
    attempts: int = 0,
) -> str:
    public_id = f"bl{uuid4().hex[:10]}"
    _seeded_public_ids.append(public_id)
    with session_scope() as session:
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                hook="Заголовок",
                body="Тело поста.",
                status=status,
                created_at=_hours(created_h) if created_h is not None else None,
                scheduled_at=_hours(booked_h) if booked_h is not None else None,
                published_at=_hours(published_h) if published_h is not None else None,
                attempts=attempts,
            )
        )
    return public_id


def _platform(**kwargs) -> str:
    """A platform name nobody else in the suite can be using."""
    return f"bl{uuid4().hex[:8]}"


def _one(platform: str) -> dict:
    report = api._backlog(now=NOW)
    matches = [item for item in report["platforms"] if item["platform"] == platform]
    assert len(matches) == 1, f"the card must group rows under exactly one {platform!r} entry"
    return matches[0]


def _stock(entry: dict, status: str) -> dict:
    rows = [row for row in entry["stock"] if row["status"] == status]
    assert len(rows) == 1, f"expected one {status!r} bucket, got {rows!r}"
    return rows[0]


@pytest.fixture(autouse=True)
def _drop_own_rows():
    """Leave no residue: the suite shares one session database."""
    yield
    with session_scope() as session:
        if _seeded_public_ids:
            session.execute(
                delete(Post).where(Post.public_id.in_(_seeded_public_ids))
            )
        if _seeded_channels:
            session.execute(
                delete(Channel).where(Channel.id.in_(_seeded_channels))
            )
    _seeded_channels.clear()
    _seeded_public_ids.clear()


# ------------------------------------------------------------------ the premise
def test_a_future_booking_is_stock_not_a_days_work():
    """The exact mistake the card replaces: `scheduled` is not "moved today".

    One post booked two hours ago and one booked for tomorrow are the same status
    and opposite facts. Only the first is a booking the day actually made.
    """
    platform = _platform()
    channel = _channel(platform=platform)
    _post(channel, "scheduled", created_h=-26, booked_h=-2)
    _post(channel, "scheduled", created_h=-2, booked_h=30)

    entry = _one(platform)

    assert entry["flow"]["booked"] == 1
    assert entry["week"]["booked"] == 1
    assert _stock(entry, "scheduled")["count"] == 2, "both still wait, that is the stock half"


def test_bookings_outside_the_comparison_window_are_in_neither_clock():
    platform = _platform()
    channel = _channel(platform=platform)
    _post(channel, "scheduled", created_h=-1, booked_h=24 * 20)

    entry = _one(platform)

    assert entry["flow"]["booked"] == 0
    assert entry["week"]["booked"] == 0


def test_output_rides_on_the_publication_stamp():
    platform = _platform()
    channel = _channel(platform=platform)
    _post(channel, "published", created_h=-30, booked_h=-28, published_h=-3)

    entry = _one(platform)

    assert entry["flow"]["published"] == 1
    assert entry["week"]["published"] == 1
    assert entry["stock"] == [], "a published post is no longer waiting for anything"


# ----------------------------------------------------------------------- stock
def test_age_bands_partition_the_bucket_without_gaps_or_overlap():
    platform = _platform()
    channel = _channel(platform=platform)
    for age in (-2.0, -30.0, -96.0, -200.0):
        _post(channel, "draft", created_h=age)

    row = _stock(_one(platform), "draft")

    assert row["count"] == 4
    assert row["ages"] == {"lt_24h": 1, "lt_72h": 1, "lt_168h": 1, "ge_168h": 1}
    assert sum(row["ages"].values()) == row["count"]
    assert row["oldest_age_hours"] == pytest.approx(200.0, abs=0.1)


def test_a_post_dated_into_the_future_has_no_negative_age():
    """`created_at` is NOT NULL, so a missing stamp is not reachable — but a
    stamp ahead of the clock is (an imported row, a skewed machine). Such a post
    is not negative hours old, and the bands must still add up to the bucket."""
    platform = _platform()
    channel = _channel(platform=platform)
    _post(channel, "draft", created_h=5)

    row = _stock(_one(platform), "draft")

    assert row["count"] == 1
    assert row["oldest_age_hours"] == 0.0, "clamped, not -5"
    assert row["ages"] == {"lt_24h": 1, "lt_72h": 0, "lt_168h": 0, "ge_168h": 0}
    assert sum(row["ages"].values()) == row["count"]


def test_overdue_needs_every_row_in_the_bucket_to_carry_a_booking():
    """A too-small count is worse than no count: it reads as a measured quiet."""
    platform = _platform()
    channel = _channel(platform=platform)
    _post(channel, "scheduled", created_h=-50, booked_h=-20)
    _post(channel, "scheduled", created_h=-40)  # waiting, but never booked

    row = _stock(_one(platform), "scheduled")

    assert row["count"] == 2
    assert row["overdue"] is None
    assert row["unbooked"] == 1


def test_a_bucket_of_bookings_that_are_all_on_time_reports_zero_not_none():
    """The other half: when every row is readable, `0` is a real measurement."""
    platform = _platform()
    channel = _channel(platform=platform)
    _post(channel, "scheduled", created_h=-5, booked_h=24)

    row = _stock(_one(platform), "scheduled")

    assert row["overdue"] == 0
    assert row["unbooked"] == 0


def test_a_draft_is_never_late():
    """Pre-booking states have no deadline, so they carry no `overdue` at all."""
    platform = _platform()
    channel = _channel(platform=platform)
    _post(channel, "draft", created_h=-400)

    row = _stock(_one(platform), "draft")

    assert row["overdue"] is None
    assert row["oldest_age_hours"] == pytest.approx(400.0, abs=0.1)


def test_failed_and_unknown_posts_carry_their_retries():
    """The live Дзен shape: 365 failed rows with 1890 attempts between them."""
    platform = _platform()
    channel = _channel(platform=platform)
    _post(channel, "failed", created_h=-70, attempts=4)
    _post(channel, "failed", created_h=-71, attempts=2)
    _post(channel, "publish_unknown", created_h=-60)

    entry = _one(platform)

    failed = _stock(entry, "failed")
    assert failed["count"] == 2
    assert failed["retries"] == 6
    assert failed["retrying"] == 2
    assert entry["waiting"] == 3


def test_publish_unknown_counts_as_waiting_for_an_operator():
    """`s7_publish` clears the booking and leaves no publication stamp, and the
    heartbeat watchdog writes the same shape, so nothing moves it without a
    human. `s2_trends.explain` already calls it in-flight; the queue card may not
    be the one place that quietly drops it."""
    platform = _platform()
    channel = _channel(platform=platform)
    _post(channel, "publish_unknown", created_h=-100)

    entry = _one(platform)

    assert "publish_unknown" in api.BACKLOG_WAITING
    row = _stock(entry, "publish_unknown")
    assert row["count"] == 1
    assert entry["waiting"] == 1
    assert entry["flow"]["published"] == 0, "unknown is not evidence that it went out"


def test_the_waiting_set_covers_every_state_the_farm_calls_in_flight():
    """Two lists of the same fact, in two modules, must not drift apart."""
    from tgfarm.s2_trends.explain import IN_FLIGHT_STATUSES

    missing = set(IN_FLIGHT_STATUSES) - set(api.BACKLOG_WAITING)
    assert not missing, f"the queue card ignores states the farm is still working on: {missing}"
    assert "published" not in api.BACKLOG_WAITING
    assert "rejected" not in api.BACKLOG_WAITING, "a rejection is an outcome, not a wait"


# ----------------------------------------------------------------------- intake
def test_a_rejection_is_read_as_the_fate_of_a_post_created_in_the_window():
    """`rejected` overwrites the status, so it has no event clock of its own."""
    platform = _platform()
    channel = _channel(platform=platform)
    _post(channel, "rejected", created_h=-4)
    _post(channel, "rejected", created_h=-24 * 5)

    entry = _one(platform)

    assert entry["flow"]["created"] == 1, "only the one created inside the flow window"
    assert entry["flow"]["rejected"] == 1
    assert entry["week"]["created"] == 2
    assert entry["week"]["rejected"] == 2


def test_intake_splits_into_the_states_the_posts_are_actually_in():
    platform = _platform()
    channel = _channel(platform=platform)
    _post(channel, "draft", created_h=-3)
    _post(channel, "gated", created_h=-4)
    _post(channel, "scheduled", created_h=-5, booked_h=6)

    flow = _one(platform)["flow"]

    assert flow["created"] == 3
    assert flow["draft"] == 1
    assert flow["gated"] == 1
    assert flow["rejected"] == 0, "a real zero: none of the three was rejected"


def test_the_lead_is_a_ratio_when_there_is_output_and_nothing_when_there_is_not():
    """An unanswerable question is not infinity and not 0×."""
    quiet = _platform()
    quiet_channel = _channel(platform=quiet)
    for _ in range(3):
        _post(quiet_channel, "rejected", created_h=-2)

    busy = _platform()
    busy_channel = _channel(platform=busy)
    for _ in range(14):
        _post(busy_channel, "rejected", created_h=-2)
    # Written days ago, sent this week: the intake and the output clocks are
    # separate columns of the same row, which is the whole reason the card reads
    # three of them.
    for _ in range(7):
        _post(busy_channel, "published", created_h=-24 * 30, published_h=-1)

    quiet_entry = _one(quiet)
    assert quiet_entry["rates"]["lead"] is None
    assert quiet_entry["rates"]["published_per_day"] == 0.0, "a real zero: nothing was sent"

    busy_entry = _one(busy)
    assert busy_entry["week"]["created"] == 14
    assert busy_entry["week"]["published"] == 7
    assert busy_entry["rates"]["created_per_day"] == pytest.approx(2.0)
    assert busy_entry["rates"]["published_per_day"] == pytest.approx(1.0)
    assert busy_entry["rates"]["lead"] == pytest.approx(2.0)


def test_channels_without_posts_are_reported_with_their_switch_position():
    """A platform whose channels are all disabled must not read as a broken farm."""
    platform = _platform()
    _channel(platform=platform, enabled=False)
    _channel(platform=platform, enabled=False)

    entry = _one(platform)

    assert entry["channels"] == 2
    assert entry["channels_enabled"] == 0
    assert entry["waiting"] == 0
    assert entry["stock"] == []
    assert entry["oldest_age_hours"] is None


# ------------------------------------------------------------------- relations
def test_totals_are_the_sum_of_the_platforms_and_nothing_else():
    platform = _platform()
    channel = _channel(platform=platform)
    _post(channel, "draft", created_h=-2)
    _post(channel, "scheduled", created_h=-3, booked_h=-4)

    report = api._backlog(now=NOW)

    by_platform = {item["platform"]: item for item in report["platforms"]}
    assert platform in by_platform, "a platform with rows must appear in the report"
    assert by_platform[platform]["waiting"] == 2
    assert report["totals"]["waiting"] == sum(
        item["waiting"] for item in report["platforms"]
    )
    for key in ("created", "rejected", "booked", "published"):
        assert report["totals"][key] == sum(item["flow"][key] for item in report["platforms"])


def test_the_overdue_total_stays_unknown_when_any_bucket_is_unreadable():
    """Half-known is not the smaller known number: a queue that cannot be summed
    is reported as not summed."""
    platform = _platform()
    channel = _channel(platform=platform)
    _post(channel, "scheduled", created_h=-5)  # waiting with no booking time

    report = api._backlog(now=NOW)

    assert report["totals"]["overdue"] is None


# ------------------------------------------------------------------ the edges
def test_the_blocking_reason_is_the_ledgers_own_decision(monkeypatch):
    """Not a second guess computed from the counters above."""
    sentinel = {"reason": "gated", "gates": {"rules_check": 2}}
    calls: list[int] = []

    def fake() -> dict:
        calls.append(1)
        return sentinel

    monkeypatch.setattr(api.pipeline, "last_publish_decision", fake)

    assert api._backlog(now=NOW)["publication"] is sentinel
    assert calls == [1], "the ledger is read once per snapshot"


def test_a_failed_read_reports_the_error_and_shows_no_numbers(monkeypatch):
    boom = SQLAlchemyError(
        "no such column: posts.scheduled_at " + ("x" * 400)
    )

    class _Broken:
        def __enter__(self):
            raise boom

        def __exit__(self, *excinfo):
            return False

    monkeypatch.setattr(api, "session_scope", lambda: _Broken())
    redacted: list[str] = []
    real = api.redact_sensitive_text

    def spy(value, **kwargs):
        redacted.append(str(value))
        return real(value, **kwargs)

    monkeypatch.setattr(api, "redact_sensitive_text", spy)

    report = api._backlog(now=NOW)

    assert report["error"], "the card must say the read failed"
    assert report["platforms"] == [], "and show no half-built numbers"
    assert redacted, "the message goes through the redactor before it reaches the panel"
    assert len(report["error"]) <= 200


def test_the_endpoint_serves_the_card():
    from fastapi.testclient import TestClient

    from tgfarm.api import create_app
    from tgfarm.core.apiauth import HEADER, ensure_token

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as test_client:
        response = test_client.get("/api/backlog", headers={HEADER: ensure_token()})

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "generated_at",
        "flow_hours",
        "compare_days",
        "publication",
        "totals",
        "platforms",
        "unresolved",
        "error",
    }
    assert payload["error"] is None
    assert payload["flow_hours"] == api.BACKLOG_FLOW_HOURS


def test_reject_rate_and_avg_er_report_nothing_instead_of_zero():
    """P34, applied to the KPI row.

    `reject_rate` had `or 1` in its denominator, so an empty `posts` table
    reported a measured 0% rejection, and `avg_er` had `float(avg_er or 0.0)`,
    so `AVG(err)` over no rows — the engagement nobody measured — reported 0,00%.
    Both must be `None`, and the panel must print «нет отчёта».

    The session database is shared, so the denominators are rarely empty here.
    The test therefore asserts the equivalence with the same aggregates the
    payload reads: `None` exactly when the aggregate is `None`, otherwise the
    rounded value. It is the fabricated zero that is not allowed, in either
    direction.
    """
    with session_scope() as session:
        by_status = dict(
            session.execute(select(Post.status, func.count()).group_by(Post.status)).all()
        )
        total_posts = sum(by_status.values())
        measured_avg = session.execute(
            select(func.avg(ChannelStat.err))
        ).scalar_one()

    kpis = api._status_payload()["kpis"]

    if total_posts:
        assert kpis["reject_rate"] == pytest.approx(
            round(by_status.get("rejected", 0) / total_posts, 3)
        )
    else:
        assert kpis["reject_rate"] is None
    if measured_avg is None:
        assert kpis["avg_er"] is None, "no rows measured is not 0,00% engagement"
    else:
        assert kpis["avg_er"] == pytest.approx(round(float(measured_avg), 4))
