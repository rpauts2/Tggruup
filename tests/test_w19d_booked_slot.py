"""The scheduler obeys the slot the editorial plan booked.

``Post.plan_slot_at`` (W19-C) stores the instant a plan entry reserved for this
post, and it survives every later overwrite of ``scheduled_at``. Storing it was
half the job; the other half is that ``schedule_ready_posts`` never read it. It
computes its own slot from the cadence — ``_interval_slot(now, last, ...)`` or
``plan_slot(now, channel_id=...)`` — and writes that, so a topic booked for 21:00
could be published whenever the cadence happened to land.

The rule this file pins is narrow: *where the plan booked an instant, the
scheduler publishes at that instant; where it cannot, it refuses instead of
quietly moving the post.* Three things bound that rule, and each is a test:

1. a booking in the future is honoured exactly, not earlier;
2. a booking outside the channel's legal local hours is refused with the
   codebase's existing blocked-schedule outcome — the post stays ``gated`` with
   ``scheduled_at`` NULL, and the refusal names itself;
3. platform capability outranks the booking: Dzen publishes through a browser
   queue, so a booked instant must never be rewritten as a promised publish time.

Determinism: every test freezes the clock and seeds the sampler, so a slot here
is a fact about the rule rather than about the minute the suite ran. Rows are
seeded under the ``w19d_`` prefix with a ``created_at`` older than anything in
the table and read back with ``limit=1`` — the scheduling layer has no per-post
filter, so an absolute count would be a count of the suite's residue.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope
from tgfarm.plan_execution import REASON_LOCAL_HOURS
from tgfarm.s7_publish import GATE_DZEN_NEEDS_ONLINE_MODEL, empty_gate_report

#: The instant the scheduler believes it is. A Wednesday in the shipped
#: distribution's live window, so no test trips a dead-zone accident.
FROZEN_NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
#: Older than every row any other test file can write, so `limit=1` is provably
#: about the post seeded below and not about the suite's leftovers.
OLDEST = datetime(1971, 1, 1, tzinfo=UTC)

_seeded_channels: list[str] = []
_seeded_public_ids: list[str] = []


@pytest.fixture(autouse=True)
def _leave_no_residue():
    yield
    with session_scope() as session:
        if _seeded_public_ids:
            session.execute(delete(Post).where(Post.public_id.in_(_seeded_public_ids)))
        if _seeded_channels:
            session.execute(delete(Channel).where(Channel.id.in_(_seeded_channels)))
    _seeded_channels.clear()
    _seeded_public_ids.clear()


class _FrozenDatetime(datetime):
    """``datetime`` with a clock that does not move.

    Subclassed rather than stubbed so ``datetime.combine`` / ``datetime.min`` —
    both used inside ``schedule_ready_posts`` — keep working.
    """

    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW.astimezone(tz) if tz is not None else FROZEN_NOW


def _slug() -> str:
    return uuid4().hex[:8]


def _seed(channel_id: str, *, platform: str, public_id: str, booked: datetime | None) -> None:
    with session_scope() as session:
        session.merge(
            Channel(
                id=channel_id,
                platform=platform,
                title="w19d channel",
                niche="general",
                enabled=True,
                timezone="UTC",
                posts_per_day=6,
            )
        )
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                hook="Хук забронированного слота.",
                body="Тело поста; слот забронирован редакционным планом.",
                status="gated",
                created_at=OLDEST,
                generation_provider="online-model",
                plan_slot_at=booked,
            )
        )
    _seeded_channels.append(channel_id)
    _seeded_public_ids.append(public_id)


def _read(public_id: str) -> SimpleNamespace:
    with session_scope() as session:
        row = session.execute(select(Post).where(Post.public_id == public_id)).scalar_one()
        session.expunge_all()
    return SimpleNamespace(
        status=row.status,
        scheduled_at=row.scheduled_at,
        plan_slot_at=row.plan_slot_at,
        plan_entry_id=row.plan_entry_id,
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class _DelegatingSchedule:
    """The shipped schedule object with only `cadence` swapped.

    A booking is gated by the cadence owner's own window, so a test that wants a
    narrower window has to change `cadence` and nothing else — hours, weights and
    jitter stay the shipped ones or the slot under test is not the one the farm
    would have produced.
    """

    def __init__(self, schedule, cadence: dict) -> None:
        self._schedule = schedule
        self.cadence = cadence

    def __getattr__(self, name):
        return getattr(self._schedule, name)


_REAL_RANDOM = random.Random


def _seeded_random(*_args, **_kwargs):
    """`random.Random` with a fixed stream, resolved against the real class.

    Bound to the module-level original rather than to `random.Random`, which this
    patch replaces: a factory that calls itself is not a seed, it is a recursion.
    """
    return _REAL_RANDOM(1234)


def _install_world(monkeypatch, *, cadence: dict | None = None) -> None:
    """Pin the four facts `schedule_ready_posts` reads from the outside world."""
    from tgfarm import s7_publish

    real_config = s7_publish.get_config()
    schedule = real_config.schedule
    if cadence is not None:
        schedule = _DelegatingSchedule(real_config.schedule, cadence)
    config = SimpleNamespace(
        persona=lambda _channel_id: SimpleNamespace(enabled=True, posts_per_day=6),
        schedule=schedule,
        personas=real_config.personas,
    )
    monkeypatch.setattr(s7_publish, "get_config", lambda: config)
    monkeypatch.setattr(
        s7_publish,
        "get_settings",
        lambda: SimpleNamespace(
            approval_required=False,
            dzen_requires_llm=False,
            vk_prime_hours="",
            vk_prime_share=0.0,
        ),
    )
    monkeypatch.setattr(s7_publish, "datetime", _FrozenDatetime)
    # The Dzen/VK budget is one global counter over the whole platform; in a suite
    # that shares one database it is another file's rows, never this rule's
    # subject. Stubbed to "nothing spent today" so the booking rule is what fails
    # a cell, not the platform ceiling.
    monkeypatch.setattr(s7_publish, "_posts_platform_today", lambda session, platform, now: 0)
    # Every sampler draws from a fresh seeded stream, so a slot is reproducible
    # without depending on how many posts the batch happened to contain.
    monkeypatch.setattr(random, "Random", _seeded_random)


# --------------------------------------------------------------------- behaviour 1
def test_a_booked_slot_in_the_future_is_published_at_that_instant(monkeypatch):
    """The plan said 21:00; the post is queued for 21:00, not earlier.

    Red before the change because the scheduler ignores the column: it writes the
    slot its own cadence produced, which is a different instant.
    """
    from tgfarm import s7_publish

    _install_world(monkeypatch)
    booked = FROZEN_NOW + timedelta(hours=9)
    channel_id = f"w19d_book_{_slug()}"
    public_id = f"w19d_p_{_slug()}"
    _seed(channel_id, platform="telegram", public_id=public_id, booked=booked)

    gates = empty_gate_report()
    scheduled = s7_publish.schedule_ready_posts(limit=1, blocked=gates)
    assert scheduled == 1, gates

    after = _read(public_id)
    assert after.status == "scheduled", after
    assert _aware(after.scheduled_at) == booked, (
        f"booked {booked}, scheduled {_aware(after.scheduled_at)}"
    )
    assert _aware(after.scheduled_at) > FROZEN_NOW, "a future booking must not be pulled back"

    # Not a coincidence: the cadence on the same seed produces a different instant,
    # so the assertion above can only pass by reading the booking.
    cadence_slot = s7_publish._interval_slot(
        FROZEN_NOW, None, {"min_gap_s": 300, "max_gap_s": 620}, random.Random(1234), 6
    )
    assert cadence_slot != booked, cadence_slot


def test_a_booked_slot_in_the_past_is_published_at_the_booked_instant(monkeypatch):
    """An overdue booking stays the booked instant; it is not re-slotted forward.

    The scheduler is not asked to re-decide the plan. Writing the past instant
    makes the post due on the next tick, which is the plan's own meaning for a
    slot that already passed.
    """
    from tgfarm import s7_publish

    _install_world(monkeypatch)
    booked = FROZEN_NOW - timedelta(minutes=40)
    channel_id = f"w19d_over_{_slug()}"
    public_id = f"w19d_p_{_slug()}"
    _seed(channel_id, platform="telegram", public_id=public_id, booked=booked)

    gates = empty_gate_report()
    assert s7_publish.schedule_ready_posts(limit=1, blocked=gates) == 1, gates

    after = _read(public_id)
    assert after.status == "scheduled", after
    assert _aware(after.scheduled_at) == booked, after


# --------------------------------------------------------------------- behaviour 2
def test_a_booking_outside_the_legal_local_hours_is_refused_not_moved(monkeypatch):
    """21:00 is outside the window: the post is held, and the hold names itself.

    Two separate lies are being prevented. The post must not be *moved* — a
    scheduler that quietly re-slots the plan's decision makes the booking
    column meaningless and the plan unsound — and it must not sit in the queue
    silently: the blocked-schedule outcome this module already has is
    `_blocked(...)`, counted into the caller's `blocked` ledger.
    """
    from tgfarm import s7_publish

    _install_world(monkeypatch, cadence={"mode": "interval", "min_gap_s": 300, "max_gap_s": 620,
                                         "active_hours": [9, 18]})
    booked = FROZEN_NOW.replace(hour=21, minute=30)
    channel_id = f"w19d_ill_{_slug()}"
    public_id = f"w19d_p_{_slug()}"
    _seed(channel_id, platform="telegram", public_id=public_id, booked=booked)

    gates = empty_gate_report()
    scheduled = s7_publish.schedule_ready_posts(limit=1, blocked=gates)

    assert scheduled == 0, gates
    after = _read(public_id)
    assert after.status == "gated", f"a refused booking must not leave the gated state: {after}"
    assert after.scheduled_at is None, f"the post was silently moved to {after.scheduled_at}"
    assert _aware(after.plan_slot_at) == booked, "the refusal must not rewrite the booking"
    # The refusal is recorded with the code the local-hours rule already uses in
    # the plan-execution layer, so one fact keeps one spelling across the two
    # schedulers. A reason outside the published ledger is the honest case here:
    # the closed 10-code gate vocabulary is a UI contract this change may not edit.
    assert gates.get(REASON_LOCAL_HOURS) == 1, gates


def test_a_booking_inside_the_legal_local_hours_is_not_refused(monkeypatch):
    """The other side of the gate: a legal booking still gets published.

    Without this the refusal above could be satisfied by holding every booking.
    """
    from tgfarm import s7_publish

    _install_world(monkeypatch, cadence={"mode": "interval", "min_gap_s": 300, "max_gap_s": 620,
                                         "active_hours": [9, 18]})
    booked = FROZEN_NOW.replace(hour=14, minute=15)
    channel_id = f"w19d_legal_{_slug()}"
    public_id = f"w19d_p_{_slug()}"
    _seed(channel_id, platform="telegram", public_id=public_id, booked=booked)

    gates = empty_gate_report()
    assert s7_publish.schedule_ready_posts(limit=1, blocked=gates) == 1, gates
    after = _read(public_id)
    assert _aware(after.scheduled_at) == booked, after
    assert not any(count for code, count in gates.items() if code == REASON_LOCAL_HOURS), gates


# --------------------------------------------------------------------- behaviour 3
def test_a_dzen_booking_does_not_become_a_promised_publish_time(monkeypatch):
    """Capability outranks the booking: the Dzen queue still drains at `now`.

    Dzen has no scheduling API — the editor is a browser, and `publish_due`
    drains its queue one post per `DZEN_MIN_GAP_S`. Writing the plan's instant
    into `scheduled_at` would claim a publish time the platform does not honour,
    and would make a due-post query hand the row to the browser hours early.
    """
    from tgfarm import s7_publish

    _install_world(monkeypatch)
    booked = FROZEN_NOW + timedelta(hours=9)
    channel_id = f"w19d_dzen_{_slug()}"
    public_id = f"w19d_p_{_slug()}"
    _seed(channel_id, platform="dzen", public_id=public_id, booked=booked)

    gates = empty_gate_report()
    assert s7_publish.schedule_ready_posts(limit=1, blocked=gates) == 1, gates

    after = _read(public_id)
    assert after.status == "scheduled", after
    assert _aware(after.scheduled_at) == FROZEN_NOW, (
        f"dzen must keep the browser queue, got {_aware(after.scheduled_at)}"
    )
    assert _aware(after.scheduled_at) != booked
    # The booking is still the plan's fact — it is just not a publish promise.
    assert _aware(after.plan_slot_at) == booked, after


def test_the_dzen_offline_model_gate_still_wins_over_a_booking(monkeypatch):
    """A booking cannot schedule material that can never ship.

    Guards the ordering: capability is checked before the booking is honoured, so
    the pre-existing Dzen gate keeps its current outcome and its current code.
    """
    from tgfarm import s7_publish

    _install_world(monkeypatch)
    monkeypatch.setattr(
        s7_publish,
        "get_settings",
        lambda: SimpleNamespace(
            approval_required=False,
            dzen_requires_llm=True,
            vk_prime_hours="",
            vk_prime_share=0.0,
        ),
    )
    booked = FROZEN_NOW + timedelta(hours=9)
    channel_id = f"w19d_dzenoff_{_slug()}"
    public_id = f"w19d_p_{_slug()}"
    _seed(channel_id, platform="dzen", public_id=public_id, booked=booked)
    with session_scope() as session:
        row = session.execute(select(Post).where(Post.public_id == public_id)).scalar_one()
        row.generation_provider = "offline"

    gates = empty_gate_report()
    assert s7_publish.schedule_ready_posts(limit=1, blocked=gates) == 0, gates
    after = _read(public_id)
    assert after.status == "gated" and after.scheduled_at is None, after
    assert gates[GATE_DZEN_NEEDS_ONLINE_MODEL] == 1, gates
