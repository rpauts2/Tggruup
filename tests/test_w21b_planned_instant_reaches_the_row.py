"""W21-B: the minute the plan books has to be the minute the row carries.

The editorial plan reserves an instant (`slot_at`), W20-B writes that instant onto
the draft it reserved it for (`posts.plan_slot_at`, once, never again), and W19-D
made the queue obey the column. What did not reach the row was the *executor's*
half: `plan_execution` held an entry until its reserved minute arrived and then
handed it to `s7_publish.unpublish`, which could only stamp its own clock. The
released post therefore carried the tick, not the plan, and `/api/editorial/
plan/booking` — whose whole job since W20-C is to subtract `scheduled_at` from
`plan_slot_at` — could report nothing but `shifted` on that path, with a shift of
exactly one apply interval nobody had ordered.

The six claims below are the package, each one falsifiable:

1. the released row's `scheduled_at` IS the entry's `slot_at`, and the shipped
   W20-C reader, called with its own arguments, reads that row as `honoured`
   with a zero-second shift;
2. the endpoint's own counts move `awaiting_slot` → `honoured` for the row, which
   is the product statement in the numbers the panel shows;
3. `unpublish` without an instant is still the base call — the panel's requeue,
   the CLI and the state-machine matrix keep `scheduled_at = now`;
4. an instant the caller names is written verbatim, past or future: this
   scheduler does not re-slot a minute it was given;
5. two entries reserved for two minutes get their own minutes, in one conditional
   UPDATE per distinct instant — the batching is what keeps the write inside the
   scheduling path instead of turning into a column write;
6. a dry run predicts the very instant the real run stamps, so the verdict an
   operator reads before turning the flag on is about the same number.

Shared-database discipline: `tests/conftest.py` gives the suite one database, so
nothing here asserts an absolute count. Every channel, cluster, post and plan
snapshot is `w21b`-prefixed, every HTTP-level claim is a delta against the same
read taken before the rows were seeded, and teardown deletes exactly the ids this
file wrote.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from tgfarm import api as api_module
from tgfarm import plan_execution, s4_generation, s7_publish
from tgfarm.core.apiauth import ensure_token
from tgfarm.core.settings import reset_settings_cache
from tgfarm.db.models import Channel, Cluster, EditorialPlan, EditorialPlanEntry, Post
from tgfarm.db.session import session_scope

#: Same clock the executor's own suite uses: 04:30 UTC is 07:30 in Moscow, inside
#: the shipped cadence's live window, so nothing here trips an hour gate by
#: accident. The hour gate has its own file (`test_w21b_planned_slot_hours.py`).
MORNING_UTC = datetime(2026, 11, 5, 4, 30, tzinfo=UTC)
#: A reserved minute that has already arrived: the executor releases on sight.
DUE_SLOT = MORNING_UTC - timedelta(minutes=1)
ENDPOINT = "/api/editorial/plan/booking"


def _slug() -> str:
    return uuid.uuid4().hex[:8]


def _aware(value: datetime | None) -> datetime | None:
    """A stored stamp as an aware UTC instant — SQLite hands the column back naive."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class _Owned:
    """The ids this file wrote, named so that leaving means deleting exactly them."""

    def __init__(self) -> None:
        self.channel_ids: list[str] = []
        self.cluster_ids: list[str] = []
        self.post_ids: list[str] = []
        self.plan_public_ids: list[str] = []

    def forget(self) -> None:
        with session_scope() as session:
            plan_ids = (
                list(
                    session.execute(
                        select(EditorialPlan.id).where(
                            EditorialPlan.public_id.in_(self.plan_public_ids)
                        )
                    ).scalars().all()
                )
                if self.plan_public_ids
                else []
            )
            if plan_ids:
                session.execute(
                    delete(EditorialPlanEntry).where(EditorialPlanEntry.plan_id.in_(plan_ids))
                )
                session.execute(delete(EditorialPlan).where(EditorialPlan.id.in_(plan_ids)))
            if self.post_ids:
                session.execute(delete(Post).where(Post.public_id.in_(self.post_ids)))
            if self.cluster_ids:
                session.execute(delete(Cluster).where(Cluster.public_id.in_(self.cluster_ids)))
            if self.channel_ids:
                session.execute(delete(Channel).where(Channel.id.in_(self.channel_ids)))


@pytest.fixture()
def rows() -> Any:
    owned = _Owned()
    yield owned
    owned.forget()


@pytest.fixture()
def flag_on(monkeypatch):
    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "true")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture(autouse=True)
def only_my_plan_is_active():
    """One reader, one snapshot: this file's own.

    `get_active_plan` orders by `created_at desc` and the suite shares one
    database, so a snapshot another file left behind can win a same-second tie.
    Foreign snapshots are parked for the duration of one test and put back.
    """
    with session_scope() as session:
        foreign_ids = list(
            session.execute(
                select(EditorialPlan.id).where(EditorialPlan.status == "active")
            ).scalars().all()
        )
        for plan_id in foreign_ids:
            session.get(EditorialPlan, plan_id).status = "superseded"
    yield
    with session_scope() as session:
        for plan_id in foreign_ids:
            row = session.get(EditorialPlan, plan_id)
            if row is not None:
                row.status = "active"


@pytest.fixture()
def quiet_budget(monkeypatch):
    """The AI ledger as this file states it, so another file's rows cannot move it."""
    from tgfarm.llm import policy

    monkeypatch.setattr(policy, "charged_today", lambda moment=None: 0)
    yield


@pytest.fixture()
def switches_on(monkeypatch):
    monkeypatch.setattr("tgfarm.platforms.is_platform_enabled", lambda platform: True)
    yield


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(
        api_module.create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    )


# ------------------------------------------------------------------ row builders
def _channel(owned: _Owned, *, timezone: str = "Europe/Moscow", posts_per_day: int = 5) -> str:
    channel_id = f"w21b-ch-{_slug()}"
    owned.channel_ids.append(channel_id)
    with session_scope() as session:
        session.add(
            Channel(
                id=channel_id,
                platform="telegram",
                title=channel_id,
                niche="",
                enabled=True,
                timezone=timezone,
                posts_per_day=posts_per_day,
            )
        )
    return channel_id


def _cluster(owned: _Owned) -> str:
    public_id = f"w21b-cl-{_slug()}"
    owned.cluster_ids.append(public_id)
    with session_scope() as session:
        session.add(
            Cluster(
                public_id=public_id,
                label=f"Тема w21b {_slug()}",
                niche="",
                status="ready",
                heat=0.5,
                last_seen_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
    return public_id


def _post(owned: _Owned, channel_id: str, cluster_id: str, *, booked: datetime | None = None,
          status: str = "gated", scheduled_at: datetime | None = None) -> str:
    public_id = f"w21b-post-{_slug()}"
    owned.post_ids.append(public_id)
    with session_scope() as session:
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                cluster_public_id=cluster_id,
                hook="Заголовок поста бронированного слота.",
                body="Тело поста; слот забронирован редакционным планом.",
                status=status,
                generation_provider="online-model",
                scheduled_at=scheduled_at,
                plan_slot_at=booked,
                created_at=datetime.now(UTC),
            )
        )
    return public_id


def _plan(owned: _Owned, entries: list[dict]) -> str:
    public_id = f"w21b-plan-{_slug()}"
    owned.plan_public_ids.append(public_id)
    with session_scope() as session:
        plan = EditorialPlan(
            public_id=public_id,
            created_at=datetime.now(UTC),
            horizon_days=7,
            status="active",
            summary={"total": len(entries)},
        )
        session.add(plan)
        session.flush()
        for entry in entries:
            session.add(
                EditorialPlanEntry(
                    plan_id=plan.id,
                    cluster_public_id=entry["cluster_public_id"],
                    title="Тема",
                    niche="",
                    action=entry["action"],
                    platform=entry.get("platform"),
                    channel_id=entry.get("channel_id"),
                    format="news",
                    priority=1,
                    confidence=0.5,
                    reasons=["тест W21-B"],
                    slot_at=entry.get("slot_at"),
                    payload={},
                )
            )
    return public_id


def _schedule_entry(owned: _Owned, channel_id: str, *,
                    slot_at: datetime = DUE_SLOT) -> dict[str, Any]:
    return {
        "cluster_public_id": _cluster(owned),
        "action": "schedule",
        "channel_id": channel_id,
        "platform": "telegram",
        "slot_at": slot_at,
    }


def _read(public_id: str) -> SimpleNamespace:
    with session_scope() as session:
        row = session.execute(select(Post).where(Post.public_id == public_id)).scalar_one()
        session.expunge_all()
    return SimpleNamespace(
        status=row.status,
        scheduled_at=_aware(row.scheduled_at),
        plan_slot_at=_aware(row.plan_slot_at),
        published_at=_aware(row.published_at),
    )


def _booked_columns(public_id: str) -> SimpleNamespace:
    """The exact seven columns `/api/editorial/plan/booking` selects for one row.

    Read back from the database rather than handed to the reader as a fixture, so
    the claim is about the row the executor wrote.
    """
    with session_scope() as session:
        row = session.execute(
            select(
                Post.public_id,
                Post.status,
                Post.plan_slot_at,
                Post.scheduled_at,
                Post.published_at,
                Channel.platform,
                Channel.timezone,
            )
            .outerjoin(Channel, Channel.id == Post.channel_id)
            .where(Post.public_id == public_id)
        ).one()
    return SimpleNamespace(
        public_id=row[0],
        status=row[1],
        plan_slot_at=_aware(row[2]),
        scheduled_at=_aware(row[3]),
        published_at=_aware(row[4]),
        platform=row[5],
        timezone=row[6],
    )


def _classified(public_id: str) -> dict[str, Any]:
    """The shipped W20-C classifier, called the way `_booking_fidelity` calls it."""
    gate = {
        "code": s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS,
        "attribution": "tick",
        **api_module._booking_refusal_ledger(
            api_module.pipeline.last_publish_decision(),
            s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS,
        ),
    }
    return api_module._booking_row_view(
        _booked_columns(public_id),
        gate=gate,
        gated_status=s7_publish.STATUS_GATED,
        hours_judge=plan_execution.local_hours_allow,
        config=api_module.get_config(),
    )


def _count(payload: dict[str, Any], code: str) -> int:
    value = payload["states"][code]["count"]
    assert isinstance(value, int), f"{code} was not counted: {value!r}"
    return value


# ------------------------------------------------------- 1 and 2: honoured, at last
def test_the_released_row_is_honoured_in_the_booking_read(
    flag_on, rows, quiet_budget, switches_on
):
    """Plan → booking → executor → the W20-C reader says `honoured`, shift 0.

    No step is stubbed and no column is written by hand: `book_plan_slots`' own
    per-topic sibling `apply_plan_booking` puts the entry's minute on the draft,
    `apply_plan` releases it, and the shipped classifier is asked about the row
    as it now reads. At the base commit this view was `shifted` by exactly one
    apply interval, because the transition stamped its own clock.
    """
    channel_id = _channel(rows)
    entry = _schedule_entry(rows, channel_id)
    post_id = _post(rows, channel_id, entry["cluster_public_id"])
    _plan(rows, [entry])

    booking = s4_generation.apply_plan_booking(entry["cluster_public_id"])
    assert booking["state"] == s4_generation.PLAN_BOOKING_BOOKED, booking
    assert _read(post_id).plan_slot_at == DUE_SLOT, "the booking did not reach the row"

    report = plan_execution.apply_plan(now=MORNING_UTC)
    assert report["applied"] == 1, report

    row = _read(post_id)
    assert row.status == "scheduled"
    assert row.scheduled_at == DUE_SLOT, (
        f"исполнитель поставил {row.scheduled_at} вместо забронированного {DUE_SLOT}"
    )
    assert row.plan_slot_at == DUE_SLOT, "бронь плана не имеет права переписываться"

    view = _classified(post_id)
    assert view["state"] == api_module.BOOKING_HONOURED, view
    assert view["shift_seconds"] == 0, view
    assert view["direction"] is None, view


def test_the_booking_endpoint_moves_the_row_from_awaiting_to_honoured(
    client, flag_on, rows, quiet_budget, switches_on
):
    """The same fact in the numbers the panel prints, as a delta of one row.

    Before: booked, unslotted, gated — `awaiting_slot`. After the executor runs:
    `honoured`. The counts are read before and after in the same request pair,
    so this is a statement about the row this test created and not about the
    suite's shared table.
    """
    channel_id = _channel(rows)
    entry = _schedule_entry(rows, channel_id)
    post_id = _post(rows, channel_id, entry["cluster_public_id"])
    _plan(rows, [entry])
    assert s4_generation.apply_plan_booking(entry["cluster_public_id"])[
        "state"
    ] == s4_generation.PLAN_BOOKING_BOOKED

    before = client.get(ENDPOINT).json()
    assert before["readable"] is True, before
    assert _classified(post_id)["state"] == api_module.BOOKING_AWAITING, (
        "до прогона исполнительного ряда здесь быть не должно: бронь есть, слота нет"
    )

    report = plan_execution.apply_plan(now=MORNING_UTC)
    assert report["applied"] == 1, report

    after = client.get(ENDPOINT).json()
    assert _count(after, "honoured") == _count(before, "honoured") + 1, (
        "исполнитель не добавил ни одной прочитанной брони"
    )
    assert _count(after, "awaiting_slot") == _count(before, "awaiting_slot") - 1, (
        "пост должен уйти из ожидания слота, а не добавиться к нему"
    )
    my_rows = [item for item in (after["samples"] or []) if item["post_id"] == post_id]
    if my_rows:
        # The sample window is twelve rows over the whole store, so presence is
        # not guaranteed; when the row is served it has to be the honoured one.
        assert my_rows[0]["state"] == "honoured", my_rows[0]


# --------------------------------------------------- 3: `at=None` keeps the base
def test_unpublish_without_an_instant_still_stamps_its_own_clock(rows):
    """Every other caller of the transition is untouched by W21-B.

    `tgfarm/api.py`'s requeue, `tgfarm/cli.py` and
    `tests/test_publishing_state_machine.py` all call `unpublish(ids)` and mean
    "start queueing this post now". A default that drifted would silently move
    the operator's own slot, so the default is pinned here rather than assumed.
    """
    channel_id = _channel(rows)
    cluster_id = _cluster(rows)
    post_id = _post(rows, channel_id, cluster_id, status="gated")

    started = datetime.now(UTC)
    assert s7_publish.unpublish([post_id]) == 1
    stamped = _read(post_id).scheduled_at

    assert _read(post_id).status == "scheduled"
    delta = abs((stamped - started).total_seconds())
    assert delta < 120, f"без `at` переход должен ставить текущий час, а не {delta:.0f} с."


def test_unpublish_writes_the_instant_it_is_given_past_or_future(rows):
    """A named instant is written verbatim — this scheduler does not re-slot.

    The same discipline `schedule_ready_posts` shows a booking
    (`W19-D`): an overdue instant makes the post due on the next tick, a future
    one waits for it. `unpublish` is not the place that decides whether a minute
    is legal, and it must not become one.
    """
    channel_id = _channel(rows)
    past_cluster, future_cluster = _cluster(rows), _cluster(rows)
    past_id = _post(rows, channel_id, past_cluster)
    future_id = _post(rows, channel_id, future_cluster)
    past = MORNING_UTC - timedelta(hours=6)
    future = MORNING_UTC + timedelta(hours=6)

    assert s7_publish.unpublish([past_id], at=past) == 1
    assert s7_publish.unpublish([future_id], at=future) == 1

    assert _read(past_id).scheduled_at == past
    assert _read(future_id).scheduled_at == future


# ------------------------------------------------- 5: one UPDATE per distinct minute
def test_two_entries_reserved_for_two_minutes_get_their_own_minutes(
    flag_on, rows, quiet_budget, switches_on, monkeypatch
):
    """Batching by instant, so the write stays the scheduling path's.

    Two entries, two reserved minutes, one apply run: each row reads its own
    minute and the transition is called once per distinct instant. A single bulk
    UPDATE with one shared value would either lose a minute or push this module
    into writing the column itself — the thing
    `test_plan_execution_flag.py::test_the_module_never_writes_a_post_state_by_hand`
    refuses.
    """
    channel_id = _channel(rows)
    first = _schedule_entry(rows, channel_id, slot_at=MORNING_UTC - timedelta(minutes=9))
    second = _schedule_entry(rows, channel_id, slot_at=MORNING_UTC - timedelta(minutes=3))
    first_post = _post(rows, channel_id, first["cluster_public_id"])
    second_post = _post(rows, channel_id, second["cluster_public_id"])
    _plan(rows, [first, second])

    calls: list[tuple[list[str], datetime | None]] = []
    real_unpublish = s7_publish.unpublish

    def spy(public_ids, *, at=None):
        calls.append((sorted(public_ids), at))
        return real_unpublish(public_ids, at=at)

    monkeypatch.setattr(s7_publish, "unpublish", spy)
    report = plan_execution.apply_plan(now=MORNING_UTC)

    assert report["applied"] == 2, report
    assert _read(first_post).scheduled_at == first["slot_at"]
    assert _read(second_post).scheduled_at == second["slot_at"]
    assert sorted(calls) == sorted(
        [([first_post], first["slot_at"]), ([second_post], second["slot_at"])]
    )
    assert len(calls) == 2, "одна минута — один условный UPDATE"


def test_entries_reserved_for_the_same_minute_share_one_update(
    flag_on, rows, quiet_budget, switches_on, monkeypatch
):
    """The batching is by instant, not by entry: one minute, one statement.

    Without this the loop degrades to a per-post UPDATE per entry, which is a
    different write shape from the one the transition documents.
    """
    channel_id = _channel(rows)
    shared = MORNING_UTC - timedelta(minutes=4)
    first = _schedule_entry(rows, channel_id, slot_at=shared)
    second = _schedule_entry(rows, channel_id, slot_at=shared)
    first_post = _post(rows, channel_id, first["cluster_public_id"])
    second_post = _post(rows, channel_id, second["cluster_public_id"])
    _plan(rows, [first, second])

    calls: list[tuple[list[str], datetime | None]] = []
    real_unpublish = s7_publish.unpublish

    def spy(public_ids, *, at=None):
        calls.append((sorted(public_ids), at))
        return real_unpublish(public_ids, at=at)

    monkeypatch.setattr(s7_publish, "unpublish", spy)
    report = plan_execution.apply_plan(now=MORNING_UTC)

    assert report["applied"] == 2, report
    assert calls == [(sorted([first_post, second_post]), shared)]
    assert _read(first_post).scheduled_at == shared
    assert _read(second_post).scheduled_at == shared


# ---------------------------------------------- 6: the prediction names the number
def test_a_dry_run_predicts_the_very_instant_the_real_run_stamps(
    flag_on, rows, quiet_budget, switches_on
):
    """`would_schedule` and `scheduled_at` are the same minute, row by row.

    The dry run's `judged.plan_slot_at` is the entry's reserved instant, and
    since W21-B that is also the instant the real run hands to the transition.
    A report that predicted the decision but not the time would leave an operator
    turning the flag on without knowing when anything would be published.
    """
    channel_id = _channel(rows)
    first = _schedule_entry(rows, channel_id, slot_at=MORNING_UTC - timedelta(minutes=12))
    second = _schedule_entry(rows, channel_id, slot_at=MORNING_UTC - timedelta(minutes=5))
    first_post = _post(rows, channel_id, first["cluster_public_id"])
    second_post = _post(rows, channel_id, second["cluster_public_id"])
    _plan(rows, [first, second])

    report = plan_execution.evaluate_plan(now=MORNING_UTC)
    assert report["dry_run"] is True
    assert report["would_schedule"] == 2, report
    predicted = {
        row["cluster_public_id"]: _aware(datetime.fromisoformat(row["judged"]["plan_slot_at"]))
        for row in report["entries"]
    }
    assert _read(first_post).scheduled_at is None, "сухой прогон не пишет"
    assert _read(second_post).scheduled_at is None

    applied = plan_execution.apply_plan(now=MORNING_UTC)
    assert applied["applied"] == 2, applied
    assert _read(first_post).scheduled_at == predicted[first["cluster_public_id"]]
    assert _read(second_post).scheduled_at == predicted[second["cluster_public_id"]]


def test_a_row_the_booking_never_reached_is_still_released_at_the_plans_minute(
    flag_on, rows, quiet_budget, switches_on
):
    """The executor's instant comes from the entry, so it does not need a booking.

    Booking the row is S4's job (W20-B/W21-A), not this loop's, and the two can
    run in either order. With no `plan_slot_at` on the row the release minute is
    still the entry's — the W20-C reader simply has no booking to compare it
    against and leaves the post in `no_booking`, which is a different statement
    from `shifted`: it says the plan never reserved this row, not that it moved.
    """
    channel_id = _channel(rows)
    entry = _schedule_entry(rows, channel_id)
    post_id = _post(rows, channel_id, entry["cluster_public_id"])
    _plan(rows, [entry])

    report = plan_execution.apply_plan(now=MORNING_UTC)
    assert report["applied"] == 1, report

    row = _read(post_id)
    assert row.scheduled_at == DUE_SLOT
    assert row.plan_slot_at is None, "исполнитель не имеет права писать бронь"

    view = _classified(post_id)
    assert view["shift_seconds"] is None, (
        "без брони вычитание несуществующего слота — не измерение"
    )
