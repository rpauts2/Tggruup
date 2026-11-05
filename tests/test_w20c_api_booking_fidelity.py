"""W20-C API contract: the slot the plan booked, against the slot the farm used.

The plan reserves an instant and writes it once — `posts.plan_slot_at`, set by
`s4_generation.persist_draft` and never touched again. The queue slot
`posts.scheduled_at` is rewritten by every requeue, retry and unpublish, and
`posts.published_at` is what the platform actually took. Since `0d8fd6f`
`schedule_ready_posts` honours a booking, and since `d848eee` a booking outside
the channel's legal local hours is refused under the registered gate
`GATE_SLOT_OUTSIDE_LOCAL_HOURS`; what no read did until now is subtract the
columns, so an operator could not tell whether the plan's schedule means anything.

`GET /api/editorial/plan/booking` is that subtraction, and this file pins that it
answers six questions that are not variations of each other:

* no booking (`plan_slot_at` NULL) — neither a zero nor a match;
* booked and honoured — including what `published_at` showed for a sent post;
* booked and moved — signed, with the direction and the magnitude;
* booked and refused — named by the gate code the ledger actually carries, never
  as a generic "blocked";
* booked and not slotted yet — waiting, which says nothing about the future;
* a booked row whose stamp is not a moment — its own state, not "waiting".

Measured 2026-11-05 through a read-only connection (`file:tgfarm.db?mode=ro`), and
pinned by the last two tests here: the live store has **no `plan_slot_at` column
at all** (`editorial_plans` = 0, `posts` has 33 columns and neither plan one), so
the honest answer on the base today is "the read did not happen", and an empty
card of zeros would be a lie about a measurement nobody took (P34).

Shared-database discipline: `tests/conftest.py` gives every file one session DB, so
nothing here asserts an absolute count. Each test reads the payload before it seeds
and asserts the delta of the state it created, every row carries a `w20cbook`
prefix, and the autouse fixture deletes only the ids that test wrote.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.exc import ProgrammingError

from tgfarm import plan_execution, s7_publish
from tgfarm.core.apiauth import ensure_token
from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope

#: The six states the read may answer with, and the only six it may answer with.
BOOKING_STATES = (
    "no_booking",
    "honoured",
    "shifted",
    "refused",
    "awaiting_slot",
    "unread_row",
)
ENDPOINT = "/api/editorial/plan/booking"

STATUS_LEN = 16


def _slug() -> str:
    return uuid4().hex[:8]


def _status() -> str:
    raw = f"w20c{_slug()}"
    assert len(raw) <= STATUS_LEN, raw
    return raw


def _moment(*, days: int = 0, hours: int = 0, minutes: int = 0) -> datetime:
    """An instant on the clock, at whole seconds, in UTC.

    Microseconds are dropped because `_iso_stamp` serves stamps at second
    precision and compares them that way; a fixture with a sub-second part could
    only test the reader's rounding, not its judgement.
    """
    return (datetime.now(UTC) + timedelta(days=days, hours=hours, minutes=minutes)).replace(
        microsecond=0
    )


@pytest.fixture(scope="module")
def client():
    from tgfarm.api import create_app

    return TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    )


class _Owned:
    """The ids this file wrote, named so that leaving means deleting exactly them."""

    def __init__(self) -> None:
        self.channel_ids: list[str] = []
        self.post_ids: list[str] = []

    def forget(self) -> None:
        with session_scope() as session:
            if self.post_ids:
                session.execute(delete(Post).where(Post.public_id.in_(self.post_ids)))
            if self.channel_ids:
                session.execute(delete(Channel).where(Channel.id.in_(self.channel_ids)))


@pytest.fixture()
def owned() -> _Owned:
    rows = _Owned()
    yield rows
    rows.forget()


# ----------------------------------------------------------------------- seeding
def _channel(
    owned: _Owned,
    *,
    platform: str = "telegram",
    timezone: str = "UTC",
) -> str:
    channel_id = f"w20cbookchan{_slug()}"
    owned.channel_ids.append(channel_id)
    with session_scope() as session:
        session.add(
            Channel(
                id=channel_id,
                title="W20-C канал",
                niche="test",
                platform=platform,
                timezone=timezone,
                enabled=True,
                posts_per_day=6,
            )
        )
    return channel_id


def _post(
    owned: _Owned,
    channel_id: str,
    *,
    status: str | None = None,
    booked: datetime | None = None,
    scheduled: datetime | None = None,
    published: datetime | None = None,
) -> str:
    """A post as the pipeline leaves it: the booking written once, the rest after."""
    public_id = f"w20cbookpost{_slug()}"
    with session_scope() as session:
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                hook="Заголовок поста с бронью плана",
                body="Тело поста.",
                status=status or _status(),
                plan_slot_at=booked,
                scheduled_at=scheduled,
                published_at=published,
                created_at=datetime.now(UTC),
            )
        )
    owned.post_ids.append(public_id)
    return public_id


# ------------------------------------------------------------------------ reads
def _payload(client: TestClient) -> dict[str, Any]:
    response = client.get(ENDPOINT)
    assert response.status_code == 200, response.text
    return response.json()


def _count(payload: dict[str, Any], code: str) -> int:
    value = payload["states"][code]["count"]
    assert isinstance(value, int), f"{code} was not counted: {value!r}"
    return value


def _row(payload: dict[str, Any], public_id: str) -> dict[str, Any]:
    """The served line for one post of this file, found by the id it was written with."""
    rows = [item for item in (payload["samples"] or []) if item["post_id"] == public_id]
    assert len(rows) == 1, (
        f"{public_id} is not named exactly once in the samples: "
        f"{[item['post_id'] for item in (payload['samples'] or [])]}"
    )
    return rows[0]


def _delta(client: TestClient, seed) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Read, seed, read again — so every claim below is about this test's own row."""
    before = _payload(client)
    public_id = seed()
    return before, _payload(client), public_id


# ------------------------------------------------------------------ fake world
class _Schedule:
    """The cadence the local-hours rule reads, with only `cadence` under control."""

    def __init__(self, cadence: dict | None) -> None:
        self.cadence = cadence

    def hours(self):
        return range(24)

    def hour_probability(self, hour: int) -> float:
        return 1.0


def _world(monkeypatch, *, cadence: dict | None, decision: dict | None) -> None:
    """Pin the two outside facts of the read: the cadence window and the tick ledger.

    `tgfarm.api` reads the cadence through `get_config()` and the refusals through
    `pipeline.last_publish_decision()` — the same ledger `/api/status` and
    `/api/backlog` show. Patching those two seams keeps the read's own logic, and
    in a suite that shares one database it keeps another file's rows out of the
    answer.
    """
    from tgfarm import api, pipeline

    monkeypatch.setattr(api, "get_config", lambda: SimpleNamespace(schedule=_Schedule(cadence)))
    monkeypatch.setattr(pipeline, "last_publish_decision", lambda: decision)


# --------------------------------------------------------------- payload contract
def test_the_read_answers_six_states_and_names_the_registered_gate(client, owned):
    """The key set is the contract; a collapsed or invented state breaks it here."""
    payload = _payload(client)
    assert set(payload) == {
        "readable",
        "reason",
        "reason_detail",
        "precision",
        "gate",
        "population",
        "states",
        "shift",
        "samples",
        "note",
    }, sorted(payload)
    assert tuple(payload["states"]) == BOOKING_STATES, list(payload["states"])
    assert payload["precision"] == "second"
    # The refusal state has to speak the ledger's own code, and `empty_gate_report()`
    # is what makes a code registered: a gate absent there is invisible in the
    # journal, which is the hole `d848eee` closed.
    gates = s7_publish.empty_gate_report()
    assert len(gates) == 11, gates
    assert payload["gate"]["code"] == s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS
    assert payload["gate"]["code"] in gates
    assert payload["gate"]["attribution"] == "tick", (
        "the refusal count is the publish tick's, never an individual post's"
    )


def test_the_read_writes_nothing_and_answers_twice_the_same_way(client, owned):
    """A reader of the booking must not become a second scheduler.

    No wall clock enters the payload either: two reads over an unchanged table
    return the same bytes, which is only true because nothing here is stamped
    "now" — a changing stamp would hide a changing judgement.
    """
    def seed() -> str:
        return _post(owned, _channel(owned), booked=_moment(days=1))

    _before, after, public_id = _delta(client, seed)
    first = _payload(client)
    second = _payload(client)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    with session_scope() as session:
        rows = session.execute(
            select(func.count()).select_from(Post).where(Post.public_id == public_id)
        ).scalar_one()
    assert rows == 1, "the read wrote or deleted a post"
    assert _count(after, "honoured") == _count(first, "honoured")


# ------------------------------------------------------ state 1: no booking at all
def test_a_post_without_a_booking_is_not_counted_as_a_match_or_a_zero(client, owned):
    """`plan_slot_at IS NULL` is the absence of a reservation.

    It is not "0 bookings honoured" and it is not a match: folding it into either
    would let a farm that has never consulted the plan report a perfect record.
    """
    def seed() -> str:
        return _post(owned, _channel(owned), booked=None, scheduled=_moment(days=1))

    before, after, public_id = _delta(client, seed)
    assert _count(after, "no_booking") - _count(before, "no_booking") == 1
    assert _count(after, "honoured") == _count(before, "honoured")
    assert _count(after, "shifted") == _count(before, "shifted")
    assert _count(after, "refused") == _count(before, "refused")
    # A post with no booking is not named in the samples at all: the read only
    # classifies rows that carry a reservation, and an unbooked row appearing in
    # the sample would be the reader inventing a booking to compare.
    assert all(item["post_id"] != public_id for item in (after["samples"] or []))
    assert after["population"]["unbooked"] - before["population"]["unbooked"] == 1


# -------------------------------------------------------------- state 2: honoured
def test_a_booking_the_queue_used_is_honoured_with_no_shift_measured(client, owned):
    slot = _moment(days=2, hours=3)
    channel_id = _channel(owned)

    def seed() -> str:
        return _post(owned, channel_id, booked=slot, scheduled=slot, status="scheduled")

    before, after, public_id = _delta(client, seed)
    assert _count(after, "honoured") - _count(before, "honoured") == 1
    row = _row(after, public_id)
    assert row["state"] == "honoured"
    assert row["shift_seconds"] == 0
    assert row["shift_minutes"] == 0
    assert row["direction"] is None, "a matched slot may not borrow the shift's words"
    assert row["plan_slot_at"] == row["scheduled_at"]
    assert row["published_at"] is None, "nothing was sent, so the fact stays unnamed"


def test_a_sent_post_says_what_published_at_showed_against_the_booking(client, owned):
    """The operator's real question ends at the send, not at the queue.

    `scheduled_at == plan_slot_at` says the slot was respected; `published_at` is
    the one stamp that says what the platform got, and its distance from the
    booking travels beside it instead of replacing it.
    """
    slot = _moment(days=1, hours=1)
    sent = slot + timedelta(minutes=10)
    channel_id = _channel(owned)

    def seed() -> str:
        return _post(
            owned,
            channel_id,
            status="published",
            booked=slot,
            scheduled=slot,
            published=sent,
        )

    _before, after, public_id = _delta(client, seed)
    row = _row(after, public_id)
    assert row["state"] == "honoured"
    assert row["published_shift_seconds"] == 600
    assert row["published_shift_minutes"] == 10.0


# ---------------------------------------------------------------- state 3: shifted
def test_a_moved_booking_is_reported_signed_with_its_direction(client, owned):
    """Two moves in two directions, and the payload must not lose the sign.

    `scheduled_at` is overwritten by a requeue, a retry or an unpublish; that is
    the whole reason the booking is its own column. Reading only the magnitude
    would print the same sentence for a post pushed a day late and one pulled a
    day forward.
    """
    booked_later = _moment(days=3)
    booked_earlier = _moment(days=4)
    channel_id = _channel(owned)

    def seed_later() -> str:
        return _post(
            owned,
            channel_id,
            status="scheduled",
            booked=booked_later,
            scheduled=booked_later + timedelta(minutes=90),
        )

    _b, after_later, id_later = _delta(client, seed_later)
    row_later = _row(after_later, id_later)
    assert row_later["state"] == "shifted"
    assert row_later["shift_seconds"] == 5400
    assert row_later["shift_minutes"] == 90.0
    assert row_later["direction"] == "later"

    def seed_earlier() -> str:
        return _post(
            owned,
            channel_id,
            status="scheduled",
            booked=booked_earlier,
            scheduled=booked_earlier - timedelta(minutes=45),
        )

    _b2, after_earlier, id_earlier = _delta(client, seed_earlier)
    row_earlier = _row(after_earlier, id_earlier)
    assert row_earlier["shift_seconds"] == -2700
    assert row_earlier["shift_minutes"] == -45.0
    assert row_earlier["direction"] == "earlier"
    assert row_later["shift_seconds"] != row_earlier["shift_seconds"]
    assert after_later["shift"]["later"] >= 1
    assert after_earlier["shift"]["earlier"] >= 1
    assert row_later["plan_slot_at"] != row_later["scheduled_at"], (
        "the fixture did not move the slot, so nothing was proven"
    )


def test_a_shift_under_a_minute_keeps_its_seconds_and_is_not_a_match(client, owned):
    """Rounding a measured move to `0 minutes` would print "nothing moved".

    The read compares at second precision — the precision it serves — so a
    30-second move is a move, and it travels as `shift_seconds: 30` even though
    minutes cannot express it (P34).
    """
    booked = _moment(days=5)
    channel_id = _channel(owned)

    def seed() -> str:
        return _post(
            owned,
            channel_id,
            status="scheduled",
            booked=booked,
            scheduled=booked + timedelta(seconds=30),
        )

    _before, after, public_id = _delta(client, seed)
    row = _row(after, public_id)
    assert row["state"] == "shifted"
    assert row["shift_seconds"] == 30
    assert row["shift_minutes"] == 0.5
    assert row["direction"] == "later"


def test_a_state_built_from_one_row_says_so(client, owned):
    """A sample of one may not read like a finding about the fleet."""
    slot = _moment(days=6)
    channel_id = _channel(owned)

    def seed() -> str:
        return _post(owned, channel_id, booked=slot, scheduled=slot + timedelta(hours=2))

    before, after, public_id = _delta(client, seed)
    _row(after, public_id)
    # `single_row` is the server's own flag, so the panel cannot "forget" it the
    # way a client-side `count === 1` check can be dropped in a later edit.
    shifted = after["states"]["shifted"]
    assert shifted["count"] >= _count(before, "shifted") + 1
    assert shifted["basis"] in ("population", "sample")
    for code, view in after["states"].items():
        assert set(view) == {"count", "basis", "single_row"}, code
        assert view["single_row"] == (view["count"] == 1), code
        if view["count"] == 1:
            assert view["basis"] in ("population", "sample"), code


# --------------------------------------------------------------- state 5: waiting
def test_a_booking_that_simply_has_not_been_slotted_is_not_a_refusal(
    client, owned, monkeypatch
):
    """`scheduled_at IS NULL` has several causes, and "refused" is only one of them.

    A tick that never ran, an earlier gate (`daily_cap`, `approval_required`) and a
    post still waiting to be handed over all leave the same pair of columns. Only
    the ledger can name a refusal, so without one the state stays "waiting" —
    which is the honest reading and says nothing about what will happen next.
    """
    _world(monkeypatch, cadence=None, decision=None)
    booked = _moment(days=1)
    channel_id = _channel(owned)

    def seed() -> str:
        return _post(owned, channel_id, status="gated", booked=booked, scheduled=None)

    before, after, public_id = _delta(client, seed)
    assert _count(after, "awaiting_slot") - _count(before, "awaiting_slot") == 1
    assert _count(after, "refused") == _count(before, "refused"), (
        "a refusal was claimed with no publish tick behind it"
    )
    row = _row(after, public_id)
    assert row["state"] == "awaiting_slot"
    assert row["refusal_gate_code"] is None
    assert row["refusal_basis"] is None
    assert row["shift_seconds"] is None, "nothing moved, so no distance may be printed"
    assert row["scheduled_at"] is None


# --------------------------------------------------------------- state 4: refused
def _illegal_slot() -> datetime:
    """A booking at 21:30 UTC — outside the 9..18 window `_world` installs."""
    return _moment(days=1).replace(hour=21, minute=30, second=0, microsecond=0)


def test_a_refused_booking_is_named_by_the_gate_the_ledger_carries(
    client, owned, monkeypatch
):
    """The refusal state, spelled with the scheduler's own words.

    Two halves have to agree before this read calls a booking refused: the rule
    that refuses it, read through `plan_execution.local_hours_allow` — the very
    callable `schedule_ready_posts` imports, not a copy — and the publish tick's
    ledger counting at least one refusal under `slot_outside_local_hours`. Neither
    alone is enough: the rule predicts, the ledger counts a tick and names no post.
    """
    _world(monkeypatch, cadence={"mode": "interval", "active_hours": [9, 18]},
           decision={"reason": "gated", "gates": {s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS: 2}})
    booked = _illegal_slot()
    channel_id = _channel(owned)

    def seed() -> str:
        return _post(owned, channel_id, status="gated", booked=booked, scheduled=None)

    before, after, public_id = _delta(client, seed)
    assert _count(after, "refused") - _count(before, "refused") == 1
    assert _count(after, "awaiting_slot") == _count(before, "awaiting_slot"), (
        "the refusal collapsed back into the waiting state"
    )
    row = _row(after, public_id)
    assert row["state"] == "refused"
    assert row["refusal_gate_code"] == s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS
    assert row["refusal_gate_code"] == "slot_outside_local_hours"
    assert row["refusal_basis"] == "rule_and_publish_tick_ledger"
    assert row["plan_slot_at"] is not None, "a refusal does not unbook the post"
    assert row["scheduled_at"] is None
    assert after["gate"]["count"] == 2
    assert str(after["gate"]["count"]) in after["note"]
    assert "не отдельному посту" in after["note"] or "принадлежит тику" in after["note"]


def test_the_same_illegal_booking_is_not_a_refusal_when_the_tick_named_zero(
    client, owned, monkeypatch
):
    """A journalled zero is a measurement; the absence of a tick is not either.

    `pipeline._publish_decision` drops a gate whose count is zero before writing,
    so a tick that ran and refused nothing gives this read a measured `0` — and
    with it, "still waiting" is the only honest state for the same row that was
    `refused` one test above.
    """
    _world(monkeypatch, cadence={"mode": "interval", "active_hours": [9, 18]},
           decision={"reason": "gated", "gates": {"daily_cap": 1}})
    booked = _illegal_slot()
    channel_id = _channel(owned)

    def seed() -> str:
        return _post(owned, channel_id, status="gated", booked=booked, scheduled=None)

    before, after, public_id = _delta(client, seed)
    assert after["gate"]["count"] == 0, "a measured zero read as no measurement"
    assert _count(after, "refused") == _count(before, "refused")
    assert _row(after, public_id)["state"] == "awaiting_slot"
    assert after["gate"]["tick_reason"] == "gated"


def test_no_ledger_at_all_is_not_reported_as_a_zero_of_refusals(
    client, owned, monkeypatch
):
    """`count: null` is "no publish tick has journalled", never "the tick refused none"."""
    _world(monkeypatch, cadence={"mode": "interval", "active_hours": [9, 18]}, decision=None)
    booked = _illegal_slot()
    channel_id = _channel(owned)

    def seed() -> str:
        return _post(owned, channel_id, status="gated", booked=booked, scheduled=None)

    _before, after, public_id = _delta(client, seed)
    assert after["gate"]["count"] is None
    assert after["gate"]["tick_reason"] is None
    assert _row(after, public_id)["state"] == "awaiting_slot"
    assert "не утверждается" in after["note"]


def test_a_dzen_booking_is_never_reported_as_refused(client, owned, monkeypatch):
    """The scheduler does not judge a Dzen booking against the slot at all.

    `schedule_ready_posts` writes `scheduled_at = now` for Dzen whatever the plan
    booked, so refusing an instant this branch never writes would send the
    operator looking for a decision the farm never made.
    """
    _world(monkeypatch, cadence={"mode": "interval", "active_hours": [9, 18]},
           decision={"reason": "gated", "gates": {s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS: 3}})
    booked = _illegal_slot()
    channel_id = _channel(owned, platform="dzen")

    def seed() -> str:
        return _post(owned, channel_id, status="gated", booked=booked, scheduled=None)

    before, after, public_id = _delta(client, seed)
    assert _count(after, "refused") == _count(before, "refused")
    assert _row(after, public_id)["state"] == "awaiting_slot"


def test_a_post_whose_channel_row_is_gone_is_not_claimed_to_be_refused(client, monkeypatch):
    """No platform means no judgement, because the guess is what would decide it.

    `_backlog` reports an orphaned post as `unknown` rather than guessing
    `telegram`; here the same rule protects a stronger claim — the reader would be
    refusing a booking on a platform it never read.

    Reached through the classifier, not through `INSERT`: the schema enforces
    `posts.channel_id -> channels.id`, so a channel-less post cannot be written
    through the typed model at all (the insert this test first tried died with
    FOREIGN KEY constraint failed). Both calls below differ in one field, `platform`,
    so the assertion fails the moment that guard is dropped — and passes vacuously
    nowhere, because the first call pins that the row really is refusable.
    """
    from tgfarm import api

    _world(monkeypatch, cadence={"mode": "interval", "active_hours": [9, 18]},
           decision={"reason": "gated", "gates": {s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS: 1}})
    gate = {"code": s7_publish.GATE_SLOT_OUTSIDE_LOCAL_HOURS, "count": 1, "attribution": "tick"}
    # The window is handed in rather than left to `local_hours_allow`'s own
    # `get_config()` fallback: an assertion about an illegal instant must not depend
    # on whichever cadence this machine's config.json happens to carry.
    config = SimpleNamespace(schedule=_Schedule({"mode": "interval", "active_hours": [9, 18]}))

    def classify(platform: str | None) -> dict[str, Any]:
        row = SimpleNamespace(
            public_id="w20cbookorphan",
            status="gated",
            plan_slot_at=_illegal_slot(),
            scheduled_at=None,
            published_at=None,
            platform=platform,
            timezone="UTC",
        )
        return api._booking_row_view(
            row,
            gate=gate,
            gated_status=s7_publish.STATUS_GATED,
            hours_judge=plan_execution.local_hours_allow,
            config=config,
        )

    named = classify("telegram")
    assert named["state"] == "refused", (
        named,
        "the fixture no longer reaches the refusal branch, so this test proves nothing",
    )
    orphan = classify(None)
    assert orphan["platform"] is None
    assert orphan["state"] == "awaiting_slot", orphan
    assert orphan["refusal_gate_code"] is None
    assert orphan["refusal_basis"] is None


# ------------------------------------------------------------- state 6: unread row
def test_a_booking_that_is_not_a_moment_is_its_own_state(client, owned):
    """Not "waiting", not "honoured": the column holds something unreadable.

    Reached through the classifier rather than through `INSERT`, because the typed
    column refuses junk on the way in — which is the point: the state exists so
    that a row this reader cannot parse is counted and not silently dropped.
    """
    from tgfarm import api

    row = SimpleNamespace(
        public_id="w20cbookjunk",
        status="gated",
        plan_slot_at="не дата",
        scheduled_at=None,
        published_at=None,
        platform="telegram",
        timezone="UTC",
    )
    view = api._booking_row_view(
        row,
        gate={"code": "slot_outside_local_hours", "count": 0, "attribution": "tick"},
        gated_status=s7_publish.STATUS_GATED,
        hours_judge=lambda *args, **kwargs: True,
        config=None,
    )
    assert view["state"] == "unread_row"
    assert view["plan_slot_at"] is None, "a junk stamp may not be printed as a booking"
    assert view["shift_seconds"] is None
    assert view["refusal_gate_code"] is None
    assert _count(_payload(client), "unread_row") == 0, (
        "a real row was counted as unread by another test's fixture"
    )


# ----------------------------------------------------------------- P34: unreadables
def _unread_payload(monkeypatch, message: str, code: str) -> dict[str, Any]:
    """Force the read's own failure the way the live store fails it today."""
    from tgfarm import api

    def explode():
        raise ProgrammingError("SELECT posts.plan_slot_at", {}, Exception(message))

    monkeypatch.setattr(api, "session_scope", explode)
    response = api._booking_fidelity()
    assert response["readable"] is False
    assert response["reason"] == code, response
    return response


def test_a_store_without_the_booking_column_says_so_instead_of_printing_zeros(
    client, owned, monkeypatch
):
    """The live base, measured 2026-11-05, and the answer it must get.

    `pragma table_info(posts)` on `tgfarm.db` lists 33 columns with neither
    `plan_slot_at` nor `plan_entry_id` (they arrive with `db/bootstrap.py`'s
    additive ALTER on the first run of a new build), and `editorial_plans` holds 0
    rows. A card that printed `honoured: 0, shifted: 0, refused: 0` there would
    report a schedule that matched nothing as a schedule that matched everything
    it was asked about. No count is printed that was not measured (P34).
    """
    payload = _unread_payload(monkeypatch, "no such column: posts.plan_slot_at", "plan_columns_missing")
    for code in BOOKING_STATES:
        assert payload["states"][code]["count"] is None, code
        assert payload["states"][code]["basis"] is None, code
        assert payload["states"][code]["single_row"] is None, code
    assert payload["population"]["booked"] is None
    assert payload["samples"] is None
    assert payload["gate"]["count"] is None
    assert "0" not in json.dumps(payload["states"]), payload["states"]
    assert "измер" in payload["note"], payload["note"]
    assert "совпало" in payload["note"] and "не" in payload["note"]
    # The read is still an HTTP 200 with a legible state: an unreadable store is a
    # fact the panel can show, not a request that fails.
    response = client.get(ENDPOINT)
    assert response.status_code == 200, response.text


def test_a_failed_query_is_a_different_sentence_from_a_missing_column(
    client, owned, monkeypatch
):
    """«сервер ошибся» and «данных нет» are two states, and three if the network is out.

    The third belongs to the panel and is pinned in `test_w20c_ui_booking_fidelity.py`.
    """
    payload = _unread_payload(monkeypatch, "table posts is locked", "post_table_unread")
    assert payload["reason"] == "post_table_unread"
    assert payload["reason_detail"]
    assert "запрос вернул ошибку" in payload["note"], payload["note"]


def test_an_empty_plan_store_reports_no_measurement_and_not_a_clean_record(
    client, owned
):
    """`editorial_plans` = 0 on the live base: the read runs, and still knows nothing.

    With the columns present and no booking in the table the counts are measured
    zeros — that part is true — but the read must not let them read as a verdict
    about the plan's schedule, and it says so in the sentence it serves.
    """
    payload = _payload(client)
    assert payload["readable"] is True
    booked = payload["population"]["booked"]
    assert isinstance(booked, int)
    if booked == 0:
        assert "нельзя" in payload["note"] or "нет ни у одного" in payload["note"], payload["note"]
        assert "совпало" in payload["note"]
    else:
        assert "Прочитано" in payload["note"], payload["note"]
