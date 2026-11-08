"""W23-A: the reader names a corrected booking instead of calling it `shifted`.

W22-C made the tick correct a booking that was illegal (`slot_booking_rebounded`,
stage `plan_slot_rebind`). A read that reports such a booking as `shifted` — the
same word it uses for a slot that merely moved — cannot be acted on by an operator:
a repaired schedule looks like a stall. This file pins that the reader's *state*
differs, under six testable conditions:

* the journal matches post and both slots — the state is `rebound`, not `shifted`,
  and the rebound count grows;
* no journal matches — the state stays `shifted` and the rebound count is a
  measured `0`, not a null;
* `PLAN_EXECUTION_ENABLED` is off — the payload is byte-for-byte the old one:
  six states, and the corrected booking still reads `shifted` (its sample still
  carries the journal attribution, as it does today);
* the journal cannot be read, or has been truncated past its cap — the rebound
  count stays `null`, because the number of surviving rebinds is not measured;
* a booking the tick left where it was stays `honoured` even if a journal entry
  names its slot;
* the shift span still covers rebound rows, and the read writes nothing.

Shared-database discipline matches `tests/test_w20c_api_booking_fidelity.py`:
delta reads, a `w23abook` prefix, and a fixture that removes exactly what it wrote.
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

from tgfarm.core.apiauth import ensure_token
from tgfarm.core.settings import reset_settings_cache
from tgfarm.db.models import Channel, PipelineRun, Post
from tgfarm.db.session import session_scope

BASE_STATES = (
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
    raw = f"w23a{_slug()}"
    assert len(raw) <= STATUS_LEN, raw
    return raw


def _moment(*, days: int = 0, hours: int = 0, minutes: int = 0) -> datetime:
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
        self.journal_ids: list[int] = []
        self.run_id_prefix: str | None = None

    def forget(self) -> None:
        with session_scope() as session:
            if self.post_ids:
                session.execute(delete(Post).where(Post.public_id.in_(self.post_ids)))
            if self.channel_ids:
                session.execute(delete(Channel).where(Channel.id.in_(self.channel_ids)))
            if self.journal_ids:
                session.execute(
                    delete(PipelineRun).where(PipelineRun.id.in_(self.journal_ids))
                )
            if self.run_id_prefix:
                session.execute(
                    delete(PipelineRun).where(
                        PipelineRun.run_id.like(f"{self.run_id_prefix}%")
                    )
                )


@pytest.fixture()
def owned() -> _Owned:
    rows = _Owned()
    yield rows
    rows.forget()


@pytest.fixture()
def flag(monkeypatch):
    """`PLAN_EXECUTION_ENABLED` set one way, and put back the other."""
    def _set(enabled: bool) -> None:
        monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "true" if enabled else "false")
        reset_settings_cache()

    _set(True)
    yield _set
    reset_settings_cache()


# ----------------------------------------------------------------------- seeding
def _channel(owned: _Owned, *, platform: str = "telegram") -> str:
    channel_id = f"w23abookchan{_slug()}"
    owned.channel_ids.append(channel_id)
    with session_scope() as session:
        session.add(
            Channel(
                id=channel_id,
                title="W23-A канал",
                niche="test",
                platform=platform,
                timezone="UTC",
                enabled=True,
                posts_per_day=6,
            )
        )
    return channel_id


def _post(
    owned: _Owned,
    channel_id: str,
    *,
    booked: datetime | None,
    scheduled: datetime | None,
) -> str:
    public_id = f"w23abookpost{_slug()}"
    with session_scope() as session:
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                hook="Заголовок поста с перепривязкой",
                body="Тело поста.",
                status="scheduled",
                plan_slot_at=booked,
                scheduled_at=scheduled,
                created_at=datetime.now(UTC),
            )
        )
    owned.post_ids.append(public_id)
    return public_id


def _rebind_journal(owned: _Owned, public_id: str, *, refused: datetime, corrected: datetime) -> int:
    """The tick's own record that this booking was corrected, in its own shape."""
    with session_scope() as session:
        journal = PipelineRun(
            run_id=f"w23arebind{_slug()}",
            stage="plan_slot_rebind",
            status="ok",
            meta={
                "outcome": "slot_booking_rebounded",
                "source": "plan_apply",
                "rebinds": [
                    {
                        "post_public_id": public_id,
                        "refused_slot_at": refused.isoformat(),
                        "corrected_slot_at": corrected.isoformat(),
                    }
                ],
            },
        )
        session.add(journal)
        session.flush()
        owned.journal_ids.append(journal.id)
        return journal.id


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


# --------------------------------------------------------------------- the state
def test_a_rebound_differs_from_a_plain_shift_when_the_journal_matches(
    client, owned, flag
):
    """Matched evidence gives the row its own state, and the count to go with it.

    Fails if the reader keeps reporting a journal-corrected booking as `shifted`:
    the operator then cannot tell a repaired schedule from a stalled one.
    """
    flag(True)
    booked = _moment(days=3)
    corrected = booked + timedelta(hours=1)
    channel_id = _channel(owned)

    def seed() -> str:
        public_id = _post(owned, channel_id, booked=booked, scheduled=corrected)
        journal_id = _rebind_journal(owned, public_id, refused=booked, corrected=corrected)
        seed.journal_id = journal_id  # type: ignore[attr-defined]
        return public_id

    before, after, public_id = _delta(client, seed)
    assert _count(after, "rebound") - _count(before, "rebound") == 1
    assert _count(after, "shifted") == _count(before, "shifted"), (
        "the corrected booking was still counted as a plain shift"
    )
    row = _row(after, public_id)
    assert row["state"] == "rebound", row
    assert row["rebind"]["status"] == "matched"
    assert row["rebind"]["code"] == "slot_booking_rebounded"
    assert row["rebind"]["journal_id"] == seed.journal_id  # type: ignore[attr-defined]
    assert row["rebind"]["source"] == "plan_apply"


def test_a_shift_without_journal_answer_stays_shifted_and_rebound_is_zero(
    client, owned, flag
):
    """No evidence is a measured zero for rebound, not a null and not a rebound.

    The journal was read and found nothing for this row; the honest answer for
    "how many rebinds" is `0`, because the measurement was made and it came back
    empty. Fails if an implementation reports `null` whenever there are no
    rebinds (or claims rebound for any shift).
    """
    flag(True)
    booked = _moment(days=3)
    channel_id = _channel(owned)

    def seed() -> str:
        return _post(owned, channel_id, booked=booked,
                     scheduled=booked + timedelta(hours=2))

    before, after, public_id = _delta(client, seed)
    assert _count(after, "shifted") - _count(before, "shifted") == 1
    assert _count(after, "rebound") == _count(before, "rebound"), (
        "a shift with no journal answer was claimed as a rebind"
    )
    row = _row(after, public_id)
    assert row["state"] == "shifted", row
    assert row["rebind"]["status"] == "not_found"


def test_flag_off_payload_is_byte_for_byte_the_old_one(client, owned, flag):
    """With the flag off the corrected booking reads `shifted`, as today.

    The journal attribution on the sample is shipped behaviour and stays; the
    *state* must not gain a seventh answer the operator did not ask the consumer
    to have. Fails if the reader applies the new classification under the old
    flag, or if `rebound` leaks into the states dict.
    """
    flag(False)
    booked = _moment(days=3)
    corrected = booked + timedelta(hours=1)
    channel_id = _channel(owned)

    def seed() -> str:
        public_id = _post(owned, channel_id, booked=booked, scheduled=corrected)
        _rebind_journal(owned, public_id, refused=booked, corrected=corrected)
        return public_id

    before, after, public_id = _delta(client, seed)
    assert tuple(after["states"]) == BASE_STATES, list(after["states"])
    assert "rebound" not in after["states"]
    assert _count(after, "shifted") - _count(before, "shifted") == 1
    row = _row(after, public_id)
    assert row["state"] == "shifted", row
    assert row["rebind"]["status"] == "matched", (
        "the attribution shipping today must survive the flag"
    )


def test_an_unread_journal_leaves_the_rebound_count_at_null(client, owned, flag, monkeypatch):
    """Evidence that could not be read is `null`, not `0` (P34).

    A count of `0` here would claim "no rebinds happened" about rows the journal
    was never asked about. Fails if the implementation reports a measured zero
    (or reclassifies rows) when `explain_rebinds` cannot open the stage at all.
    """
    from tgfarm import booking_evidence

    flag(True)

    def explode():
        raise RuntimeError("journal table unavailable")

    monkeypatch.setattr(booking_evidence, "session_scope", explode)
    booked = _moment(days=3)
    channel_id = _channel(owned)

    def seed() -> str:
        return _post(owned, channel_id, booked=booked,
                     scheduled=booked + timedelta(hours=1))

    before, after, public_id = _delta(client, seed)
    assert _count(after, "shifted") - _count(before, "shifted") == 1
    assert after["states"]["rebound"]["count"] is None, after["states"]["rebound"]
    assert after["states"]["rebound"]["basis"] is None
    assert after["states"]["rebound"]["single_row"] is None
    row = _row(after, public_id)
    assert row["state"] == "shifted", row
    assert row["rebind"]["status"] == "unread"


def test_a_truncated_journal_leaves_the_rebound_count_at_null(client, owned, flag):
    """Evidence past the reader's cap is unmeasured, and says so with `null`.

    `explain_rebinds` reads the newest 1000 `plan_slot_rebind` journals; with
    more behind them a row may be a rebind this read cannot prove. Reporting
    `0` (or only the proven ones) would dress a windowed hunt as a total.
    """
    flag(True)
    booked = _moment(days=3)
    corrected = booked + timedelta(hours=1)
    channel_id = _channel(owned)

    def seed() -> str:
        public_id = _post(owned, channel_id, booked=booked, scheduled=corrected)
        _rebind_journal(owned, public_id, refused=booked, corrected=corrected)
        prefix = f"w23ainc{_slug()}"
        owned.run_id_prefix = prefix
        # Newer than the target journal, so the target falls past the newest
        # 1000 rows the reader opens — the record this read cannot know about.
        with session_scope() as session:
            session.add_all(
                PipelineRun(
                    run_id=f"{prefix}-{index:04d}",
                    stage="plan_slot_rebind",
                    status="ok",
                    meta={"outcome": "other"},
                )
                for index in range(1005)
            )
        return public_id

    before, after, public_id = _delta(client, seed)
    assert _count(after, "shifted") - _count(before, "shifted") == 1
    assert after["states"]["rebound"]["count"] is None, after["states"]["rebound"]
    assert after["states"]["rebound"]["single_row"] is None
    assert _row(after, public_id)["state"] == "shifted"
    assert _row(after, public_id)["rebind"]["status"] == "incomplete"


def test_an_honoured_booking_stays_honoured_even_with_a_journal_entry(
    client, owned, flag
):
    """A rebind that put the slot back where the plan booked it is a match.

    `explain_rebinds` matches only rows whose two instants differ, and a row the
    queue used exactly at its booking is `honoured` whatever the journal says;
    claiming it as a rebind would invent a correction the columns deny.
    """
    flag(True)
    slot = _moment(days=3)
    channel_id = _channel(owned)

    def seed() -> str:
        public_id = _post(owned, channel_id, booked=slot, scheduled=slot)
        _rebind_journal(owned, public_id, refused=slot, corrected=slot)
        return public_id

    before, after, public_id = _delta(client, seed)
    assert _row(after, public_id)["state"] == "honoured"
    assert _count(after, "rebound") == _count(before, "rebound")


def test_the_shift_span_still_covers_a_rebound_movement(client, owned, flag):
    """`largest_abs_minutes` is about how far the used slot moved, not by whom.

    With every movement corrected by the tick, a span that only read `shifted`
    rows would announce that nothing moved. Fails if rebound rows drop out of
    the later/earlier/largest aggregation.
    """
    flag(True)
    channel_id = _channel(owned)

    def seed() -> str:
        later_booked = _moment(days=3)
        later = _post(owned, channel_id, booked=later_booked,
                      scheduled=later_booked + timedelta(hours=2))
        _rebind_journal(owned, later, refused=later_booked,
                        corrected=later_booked + timedelta(hours=2))
        earlier_booked = _moment(days=5)
        _post(owned, channel_id, booked=earlier_booked,
              scheduled=earlier_booked - timedelta(minutes=45))
        return later

    before, after, public_id = _delta(client, seed)
    assert after["shift"]["later"] - before["shift"]["later"] == 1
    assert after["shift"]["earlier"] - before["shift"]["earlier"] == 1
    assert after["shift"]["largest_abs_minutes"] == 120.0
    assert _row(after, public_id)["state"] == "rebound"


def test_the_read_writes_nothing(client, owned, flag):
    """A reader must not become a second scheduler, flag on included.

    Two reads over an unchanged table return identical bytes, and neither adds a
    journal row nor a post row. Fails if attribution ever writes.
    """
    flag(True)
    booked = _moment(days=3)
    corrected = booked + timedelta(hours=1)
    channel_id = _channel(owned)
    public_id = _post(owned, channel_id, booked=booked, scheduled=corrected)
    journal_id = _rebind_journal(
        owned, public_id, refused=booked, corrected=corrected
    )

    def counts() -> tuple[int, int]:
        with session_scope() as session:
            posts = session.execute(
                select(func.count()).select_from(Post).where(
                    Post.public_id == public_id
                )
            ).scalar_one()
            journals = session.execute(
                select(func.count()).select_from(PipelineRun).where(
                    PipelineRun.id == journal_id
                )
            ).scalar_one()
        return posts, journals

    before = counts()
    first = _payload(client)
    second = _payload(client)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert counts() == before == (1, 1)
