"""W19-C: the post-level read answers «из какого решения плана этот пост».

`tgfarm/db/models.py` gave the post row two new columns in wave 19-B —
`posts.plan_entry_id` (the soft reference to the `editorial_plan_entries` row that
ordered the post) and `posts.plan_slot_at` (the slot that entry booked, which
survives every later overwrite of `posts.scheduled_at`). Nothing on the live base
has run on them yet (2026-11-05, read-only connection: `editorial_plans` = 0 rows,
`editorial_plan_entries` = 0 rows, and no post carries either column), so the read
that hands them to the panel has to be able to say three different things and not
conflate them:

* the post came out of a plan and the plan is still readable — name the entry and
  the snapshot that ordered it;
* the post was produced while no plan was in play — nothing to say, and no
  placeholder allowed to say it for them;
* the post names an entry that is gone — the reference is a real measured fact and
  the answer is unread, which is neither of the two above.

This file pins the first half of that contract on the payload the panel already
consumes (`_post_view`, which serves both `GET /api/posts` and
`GET /api/posts/{public_id}`), because a second endpoint shape would be a second
contract to keep in sync and nothing would read it.

Shared-database discipline: `tests/conftest.py` gives every file in the suite the
same session DB, so each row this file writes carries a `w19cprov` prefix (the
status doubles as the list filter, which isolates the response), every claim is
about the fields of a row this file created, and the autouse fixture deletes only
the ids named by that test.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import SQLAlchemyError

from tgfarm.core.apiauth import ensure_token
from tgfarm.db.models import Channel, EditorialPlan, EditorialPlanEntry, Post
from tgfarm.db.session import get_engine, session_scope

#: The three keys the panel is allowed to dereference. A fourth would be a second
#: way to say one of these; a fifth spelling of "unknown" would be the bug.
PROVENANCE_KEYS = ("plan_entry_id", "plan_slot_at", "plan_public_id")

#: `posts.status` is a free String(16), so a per-test value here makes
#: `GET /api/posts?status=…` return only the rows this file wrote.
STATUS_LEN = 16


def _status() -> str:
    raw = f"w19c{uuid4().hex[:8]}"
    assert len(raw) <= STATUS_LEN, raw
    return raw


def _slot(*, days: int = 1, hours: int = 0) -> datetime:
    """A booking the plan could have written: whole seconds, in UTC.

    Microseconds are dropped deliberately — `_iso_stamp` serves stamps at second
    precision, and a fixture with a sub-second part would compare unequal to the
    value the endpoint is allowed to send.
    """
    return (datetime.now(UTC) + timedelta(days=days, hours=hours)).replace(microsecond=0)


@pytest.fixture(scope="module")
def client():
    from tgfarm.api import create_app

    return TestClient(
        create_app(with_scheduler=False),
        base_url="http://127.0.0.1",
        headers={"X-TGFarm-Token": ensure_token()},
    )


class _Owned:
    """The ids this test wrote, named so that leaving means deleting exactly them."""

    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.channel_ids: list[str] = []
        self.post_ids: list[str] = []
        self.plan_ids: list[int] = []
        self.entry_ids: list[int] = []

    def forget(self) -> None:
        with session_scope() as session:
            if self.post_ids:
                session.execute(delete(Post).where(Post.public_id.in_(self.post_ids)))
            if self.channel_ids:
                session.execute(delete(Channel).where(Channel.id.in_(self.channel_ids)))
            if self.entry_ids:
                session.execute(
                    delete(EditorialPlanEntry).where(EditorialPlanEntry.id.in_(self.entry_ids))
                )
            if self.plan_ids:
                session.execute(delete(EditorialPlan).where(EditorialPlan.id.in_(self.plan_ids)))


@pytest.fixture()
def owned() -> _Owned:
    rows = _Owned()
    yield rows
    rows.forget()


# --------------------------------------------------------------------- seeding
def _channel(owned: _Owned) -> str:
    channel_id = f"w19cprov{uuid4().hex[:8]}"
    owned.channel_ids.append(channel_id)
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="W19-C канал", niche="test", platform="telegram"))
    return channel_id


def _plan_entry(owned: _Owned, *, slot_at: datetime | None = None) -> tuple[str, int]:
    """One snapshot plus the entry that booked `slot_at`; returns (public_id, id)."""
    public_id = f"w19cprovplan{uuid4().hex[:8]}"
    with session_scope() as session:
        plan = EditorialPlan(public_id=public_id, status="superseded", horizon_days=7)
        session.add(plan)
        session.flush()
        entry = EditorialPlanEntry(
            plan_id=plan.id,
            cluster_public_id=f"w19cprovtopic{uuid4().hex[:8]}",
            action="produce",
            platform="telegram",
            slot_at=slot_at,
        )
        session.add(entry)
        session.flush()
        owned.plan_ids.append(int(plan.id))
        owned.entry_ids.append(int(entry.id))
        return public_id, int(entry.id)


def _post(
    owned: _Owned,
    channel_id: str,
    *,
    status: str | None = None,
    plan_entry_id: int | None = None,
    plan_slot_at: datetime | None = None,
    scheduled_at: datetime | None = None,
) -> tuple[str, str]:
    """A post as the pipeline writes it: provenance set once, at insert."""
    status = status or _status()
    public_id = f"w19cprovpost{uuid4().hex[:8]}"
    owned.statuses.append(status)
    owned.post_ids.append(public_id)
    with session_scope() as session:
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                hook="Заголовок из плана",
                body="Тело поста.",
                status=status,
                scheduled_at=scheduled_at,
                plan_entry_id=plan_entry_id,
                plan_slot_at=plan_slot_at,
            )
        )
    return public_id, status


def _list_rows(client: TestClient, status: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/posts?status={status}&limit=5")
    assert response.status_code == 200, response.text
    return response.json()["posts"]


def _one_row(client: TestClient, status: str) -> dict[str, Any]:
    rows = _list_rows(client, status)
    assert len(rows) == 1, rows
    return rows[0]


# ----------------------------------------------------------------- contract shape
def test_every_row_the_panel_reads_carries_the_three_provenance_keys(client, owned):
    """A key the server omits is a key the panel may not distinguish from a null."""
    channel_id = _channel(owned)
    planned_id, status_planned = _post(owned, channel_id)
    _, status_plain = _post(owned, channel_id)

    for status in (status_planned, status_plain):
        row = _one_row(client, status)
        for key in PROVENANCE_KEYS:
            assert key in row, f"{key} is missing from the served row: {sorted(row)}"


def test_a_post_that_came_from_a_readable_plan_names_the_entry_and_the_snapshot(
    client, owned
):
    """The one state where all three fields have an answer."""
    slot = _slot(days=2)
    channel_id = _channel(owned)
    plan_public_id, entry_id = _plan_entry(owned, slot_at=slot)
    public_id, status = _post(owned, channel_id, plan_entry_id=entry_id, plan_slot_at=slot)

    row = _one_row(client, status)
    assert row["id"] == public_id
    assert row["plan_entry_id"] == entry_id
    assert row["plan_public_id"] == plan_public_id, (
        "the entry is readable and the snapshot that owns it is not named: the "
        "operator is told which decision ordered the post but not which plan made it"
    )
    assert row["plan_slot_at"] is not None
    assert datetime.fromisoformat(row["plan_slot_at"]) == slot


def test_a_post_produced_without_a_plan_says_nothing_in_all_three_fields(client, owned):
    """The state the live farm is in for every row it has today.

    `plan_slot_at: 0`, `""` and the epoch are all answers this endpoint may not
    give: each one is a slot the panel would happily print.
    """
    channel_id = _channel(owned)
    public_id, status = _post(owned, channel_id)

    row = _one_row(client, status)
    assert row["plan_entry_id"] is None
    assert row["plan_slot_at"] is None
    assert row["plan_public_id"] is None
    for key in PROVENANCE_KEYS:
        value = row[key]
        assert not isinstance(value, bool), f"{key} was served as a boolean: {value!r}"
        assert value is None or isinstance(value, (int, str)), f"{key}: {value!r}"
        if isinstance(value, str):
            assert value.strip(), f"{key} is an empty string dressed up as an answer"
        if isinstance(value, int):
            assert value > 0, f"{key} is the placeholder zero, not a reference: {value!r}"
        if key == "plan_slot_at" and isinstance(value, str):
            assert "1970" not in value, f"{key} is the epoch printed as a booking: {value!r}"
    assert public_id == row["id"]


def test_a_zero_entry_reference_is_nothing_to_say(client, owned):
    """`0` is not a row id, and passing it through would be a fake provenance.

    `s4_generation.persist_draft` writes the id it was handed and never a zero, but
    the column is a plain nullable integer, so the read has to decide what a zero
    means. `None` is the only answer that does not invent a plan entry.
    """
    channel_id = _channel(owned)
    _post(owned, channel_id, plan_entry_id=0, plan_slot_at=None)

    row = _one_row(client, owned.statuses[-1])
    assert row["plan_entry_id"] is None, row["plan_entry_id"]
    assert row["plan_public_id"] is None


def test_a_dangling_entry_reference_stays_a_reference_and_loses_only_the_answer(
    client, owned
):
    """The third silence: the post names an entry, and that entry is gone.

    This is not "no plan" — deleting the reference would tell the operator the plan
    never spoke, when the truth is that the plan's words can no longer be read.
    """
    channel_id = _channel(owned)
    gone_entry_id = _next_free_entry_id()
    slot = _slot(days=1)
    _post(owned, channel_id, plan_entry_id=gone_entry_id, plan_slot_at=slot)

    row = _one_row(client, owned.statuses[-1])
    assert row["plan_entry_id"] == gone_entry_id
    assert row["plan_public_id"] is None, "an unreadable entry may not gain a plan id"
    assert row["plan_slot_at"] is not None, (
        "the booked slot is a column on the post row: losing the entry does not "
        "unbook it, and hiding that is the defect this column exists to fix"
    )


def test_the_booked_slot_survives_a_later_move_of_scheduled_at(client, owned):
    """Why `plan_slot_at` is its own column, asserted through the read.

    `posts.scheduled_at` is overwritten by every later queue move; the booking is
    not. A read that derived the slot from `scheduled_at` would answer a different
    question than the one asked.
    """
    booked = _slot(days=3)
    channel_id = _channel(owned)
    _plan, entry_id = _plan_entry(owned, slot_at=booked)
    public_id, status = _post(
        owned, channel_id, plan_entry_id=entry_id, plan_slot_at=booked,
        scheduled_at=booked + timedelta(hours=9),
    )

    row = _one_row(client, status)
    assert datetime.fromisoformat(row["plan_slot_at"]) == booked
    assert datetime.fromisoformat(row["scheduled_at"]) != datetime.fromisoformat(
        row["plan_slot_at"]
    ), "the fixture did not move the queue slot, so nothing was proven"
    with session_scope() as session:
        stored = session.execute(select(Post.plan_slot_at).where(Post.public_id == public_id))
        assert stored.scalar_one() == booked.replace(tzinfo=None)


def test_the_detail_payload_carries_the_same_three_keys_as_the_list(client, owned):
    """One projection, one contract: the drawer and the queue may not disagree."""
    slot = _slot(days=5)
    channel_id = _channel(owned)
    plan_public_id, entry_id = _plan_entry(owned, slot_at=slot)
    public_id, status = _post(owned, channel_id, plan_entry_id=entry_id, plan_slot_at=slot)

    listed = _one_row(client, status)
    detail = client.get(f"/api/posts/{public_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    for key in PROVENANCE_KEYS:
        assert key in body, f"{key} is in the list row but not in the detail row"
        assert body[key] == listed[key], (key, body[key], listed[key])


def test_a_dangling_reference_and_a_missing_one_are_not_the_same_answer(client, owned):
    """The two "nothing to say" states must stay distinguishable in one response."""
    channel_id = _channel(owned)
    gone = _next_free_entry_id()
    status = _status()
    dangling_id, _ = _post(owned, channel_id, status=status, plan_entry_id=gone)
    unplanned_id, _ = _post(owned, channel_id, status=status)

    rows = {row["id"]: row for row in _list_rows(client, status)}
    assert set(rows) == {dangling_id, unplanned_id}, rows
    assert rows[dangling_id]["plan_entry_id"] == gone
    assert rows[dangling_id]["plan_public_id"] is None
    assert rows[unplanned_id]["plan_entry_id"] is None
    assert rows[unplanned_id]["plan_public_id"] is None
    assert rows[dangling_id] != rows[unplanned_id], (
        "the two silences collapsed into one answer"
    )


def test_a_naive_booked_slot_is_served_in_one_clock(client, owned):
    """SQLite hands `DateTime(timezone=True)` back naive; the panel would read it
    as wall-clock local time, which is the drift `_iso_stamp` exists to repair."""
    naive_booked = datetime(2096, 3, 4, 5, 6, 7)
    channel_id = _channel(owned)
    _post(owned, channel_id, plan_slot_at=naive_booked)

    row = _one_row(client, owned.statuses[-1])
    served = row["plan_slot_at"]
    assert isinstance(served, str), served
    assert re.search(r"[+-]\d{2}:\d{2}$", served), f"offset-less stamp: {served!r}"
    parsed = datetime.fromisoformat(served)
    assert parsed.tzinfo is not None
    assert parsed.astimezone(timezone.utc) == naive_booked.replace(tzinfo=UTC)
    assert parsed > datetime.now(UTC), "a booking in the past is not a booking"


# ------------------------------------------------------------------ the one read
def _count_entry_reads(fn) -> list[str]:
    """Every statement `fn()` runs that touches `editorial_plan_entries`."""
    seen: list[str] = []

    def before(connection, cursor, statement, *args):
        if "editorial_plan_entries" in statement:
            seen.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", before)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return seen


def test_the_list_resolves_entries_in_one_read_not_one_per_row(client, owned):
    """Provenance is a column read, not a query per card.

    `GET /api/posts` serves up to 500 rows; a per-row lookup would turn a panel
    refresh into 500 statements against the shared DB, which is the kind of change
    that survives review and then bites on the live base.
    """
    channel_id = _channel(owned)
    _plan, entry_id = _plan_entry(owned, slot_at=_slot())
    for _ in range(4):
        _post(owned, channel_id, plan_entry_id=entry_id)
    status = owned.statuses[-1]
    assert len({row["plan_entry_id"] for row in _list_rows(client, status)}) == 1

    def read():
        response = client.get("/api/posts?limit=200")
        assert response.status_code == 200, response.text

    statements = _count_entry_reads(read)
    assert len(statements) <= 1, f"{len(statements)} reads of the entry table for one list"


def test_the_provenance_read_touches_the_entry_table_only_when_something_is_named(
    client, owned,
):
    """No post carries provenance today, so the panel must cost nothing today.

    The live base is exactly this state, so this is the measurement of "the change
    is invisible until the plan starts producing".
    """
    channel_id = _channel(owned)
    _post(owned, channel_id)
    _post(owned, channel_id)

    def read():
        response = client.get("/api/posts?limit=200")
        assert response.status_code == 200, response.text

    assert _count_entry_reads(read) == []


def _next_free_entry_id() -> int:
    """An id no `editorial_plan_entries` row has — a reference whose target is gone.

    Measured, not assumed: the shared session DB may already hold entries written
    by another file, so the probe takes the current maximum.
    """
    try:
        with session_scope() as session:
            highest = session.execute(select(func.max(EditorialPlanEntry.id))).scalar()
    except SQLAlchemyError as exc:  # pragma: no cover - the table always exists
        raise AssertionError(f"the entry table is unreadable: {exc}") from exc
    return int(highest or 0) + 1000
