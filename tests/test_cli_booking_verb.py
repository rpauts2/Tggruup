"""W22-D: the booking pass gets the operator's verb — `tgfarm plan-book`.

Wave 21 gave plan booking a live caller (`s4_generation.book_plan_tick`, the
third plan job) but no offline one: every minute the pass reserves could only
arrive from a scheduler tick, so a restart with `PLAN_EXECUTION_ENABLED` turned
on could not be rehearsed. These tests hold the verb to what the wave-18
`plan-dry-run` precedent promises, and each claim is read back from the store
rather than from the command's own stdout:

1. the default run judges and writes nothing — even with the switch on, the row
   keeps its NULL `plan_slot_at`, keeps its status, and no journal row appears;
2. `--apply` with the switch off writes nothing either, and says so in the
   pass's own words instead of inventing a booking;
3. `--apply` with the switch on writes exactly the one column the pass owns —
   no status, no `scheduled_at`, no publication;
4. with the switch off the verb still judges, because a rehearsal that refuses
   to look is not a rehearsal;
5. the zeroes read differently from each other: "the plan named no minute for
   this topic" (`no_schedule_entry`), "the plan named one it cannot read"
   (`schedule_entry_slot_unreadable`) and "there was no open order to walk"
   (`considered=0`) are three sentences, not three spellings of `0`;
6. nothing on either path reaches a transport, and the bound the operator may
   narrow is the pass's own, not a number invented here.

Shared-database discipline: `tests/conftest.py` gives the suite one database, so
nothing asserts an absolute count over the whole `posts` table. Every channel,
cluster, post and plan snapshot is `w22d`-prefixed and deleted on teardown.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select

from tgfarm import cli, s4_generation
from tgfarm.core.settings import reset_settings_cache
from tgfarm.db.models import Channel, Cluster, EditorialPlan, EditorialPlanEntry, PipelineRun, Post
from tgfarm.db.session import session_scope

#: a reserved minute that has already arrived, so nothing here waits on a clock;
#: `Europe/Moscow` keeps it inside the shipped cadence's live window, the same
#: convention the executor's own suite plans against.
DUE_SLOT = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)


def _slug() -> str:
    return uuid.uuid4().hex[:8]


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


def _switch(monkeypatch, value: bool) -> None:
    monkeypatch.setenv("PLAN_EXECUTION_ENABLED", "true" if value else "false")
    reset_settings_cache()


@pytest.fixture()
def flag_on(monkeypatch):
    _switch(monkeypatch, True)
    yield
    reset_settings_cache()


@pytest.fixture()
def flag_off(monkeypatch):
    _switch(monkeypatch, False)
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
            plan = session.get(EditorialPlan, plan_id)
            if plan is not None:
                plan.status = "active"


# ------------------------------------------------------------------ row builders
def _channel(owned: _Owned) -> str:
    channel_id = f"w22d-ch-{_slug()}"
    owned.channel_ids.append(channel_id)
    with session_scope() as session:
        session.add(
            Channel(
                id=channel_id,
                platform="telegram",
                title=channel_id,
                niche="",
                enabled=True,
                timezone="Europe/Moscow",
                posts_per_day=5,
            )
        )
    return channel_id


def _cluster(owned: _Owned) -> str:
    public_id = f"w22d-cl-{_slug()}"
    owned.cluster_ids.append(public_id)
    with session_scope() as session:
        session.add(
            Cluster(
                public_id=public_id,
                label=f"Тема w22d {_slug()}",
                niche="",
                status="ready",
                heat=0.5,
                last_seen_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
    return public_id


def _post(
    owned: _Owned,
    channel_id: str,
    cluster_id: str,
    *,
    entry_id: int | None = 1,
    status: str = "gated",
    booked: datetime | None = None,
    scheduled_at: datetime | None = None,
) -> str:
    """A draft the plan ordered and nobody has answered yet.

    `plan_entry_id` is what makes an order open at all: `editorial` treats a draft
    the plan never wrote as plain material, so a fixture that left it NULL would
    test a queue the booking pass cannot even see.
    """
    public_id = f"w22d-post-{_slug()}"
    owned.post_ids.append(public_id)
    with session_scope() as session:
        session.add(
            Post(
                public_id=public_id,
                channel_id=channel_id,
                cluster_public_id=cluster_id,
                hook="Заголовок поста для rehearsal бронирования.",
                body="Тело поста; план резервирует под него минуту.",
                status=status,
                generation_provider="offline-template",
                plan_entry_id=entry_id,
                plan_slot_at=booked,
                scheduled_at=scheduled_at,
                created_at=datetime.now(UTC),
            )
        )
    return public_id


def _plan(owned: _Owned, entries: list[dict]) -> str:
    public_id = f"w22d-plan-{_slug()}"
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
                    reasons=["тест W22-D"],
                    slot_at=entry.get("slot_at"),
                    payload={},
                )
            )
    return public_id


def _ordered_queue(
    owned: _Owned,
    *,
    action: str = "schedule",
    slot_at: datetime | None = DUE_SLOT,
    count: int = 1,
) -> tuple[str, list[str], list[str]]:
    """`count` topics, each with an open order and exactly one entry about it."""
    channel_id = _channel(owned)
    cluster_ids = [_cluster(owned) for _ in range(count)]
    _plan(
        owned,
        [
            {
                "cluster_public_id": cluster_id,
                "action": action,
                "channel_id": channel_id,
                "platform": "telegram",
                "slot_at": slot_at,
            }
            for cluster_id in cluster_ids
        ],
    )
    post_ids = [_post(owned, channel_id, cluster_id) for cluster_id in cluster_ids]
    return channel_id, cluster_ids, post_ids


# ---------------------------------------------------------------- the store, read
def _row(public_id: str) -> tuple[Any, ...]:
    """The four columns this verb could conceivably touch, in that order."""
    with session_scope() as session:
        row = session.execute(
            select(Post.status, Post.plan_slot_at, Post.scheduled_at, Post.published_at).where(
                Post.public_id == public_id
            )
        ).one()
    return (row[0], row[1], row[2], row[3])


def _booking_journal_rows() -> int:
    with session_scope() as session:
        return int(
            session.execute(
                select(func.count())
                .select_from(PipelineRun)
                .where(PipelineRun.stage == s4_generation.STAGE_BOOKING)
            ).scalar_one()
        )


def _stdout(capsys) -> str:
    return capsys.readouterr().out


def _report_of(out: str) -> dict:
    """The JSON block the command printed, separated from its human lines."""
    return json.loads(out[out.index("{") : out.rindex("}") + 1])


def _decision(report: dict, cluster_public_id: str) -> dict:
    for item in report["decisions"]:
        if item["cluster_public_id"] == cluster_public_id:
            return item
    raise AssertionError(f"проход не рассмотрел заказ {cluster_public_id}: {report['states']}")


# ------------------------------------------------------------------------- wiring
def test_the_parser_wires_the_verb_and_borrows_the_pass_limit():
    """`--limit` defaults to the pass's own bound: no second number to keep in sync."""
    parser = cli.build_parser()
    args = parser.parse_args(["plan-book"])

    assert args.func is cli.cmd_plan_book
    assert args.apply is False
    assert args.limit == s4_generation.MAX_BOOKINGS_PER_RUN == cli.booking_pass_limit()
    assert parser.parse_args(["plan-book", "--limit", "5"]).limit == 5
    assert parser.parse_args(["plan-book", "--apply"]).apply is True


def test_the_new_verb_is_documented_where_the_cli_documents_itself():
    assert "plan-book" in cli.__doc__


def test_the_glossary_only_names_states_the_pass_can_return():
    """A verb that invents an outcome is a verb an operator cannot trust.

    `would_book` is the preview's own word for a minute it did not write; every
    other key has to be one `book_plan_slots` really reports — and every state the
    pass can report has to have a sentence, or the operator reads a code.
    """
    assert cli.BOOKING_WOULD_BOOK not in s4_generation.PLAN_BOOKING_STATES
    invented = (
        set(cli.BOOKING_STATE_NOTES) - set(s4_generation.PLAN_BOOKING_STATES)
        - {cli.BOOKING_WOULD_BOOK}
    )
    assert not invented, f"глоссарий брони выдумывает состояния: {invented}"
    for state in s4_generation.PLAN_BOOKING_STATES:
        assert state in cli.BOOKING_STATE_NOTES, f"состояние {state} осталось без объяснения"


# ------------------------------------------------------------- the default run
def test_the_rehearsal_with_the_switch_on_writes_no_row_and_journals_nothing(
    rows, flag_on, capsys
):
    """The headline claim: judging a booking is not doing one, on a live switch."""
    _, cluster_ids, post_ids = _ordered_queue(rows)

    before = _row(post_ids[0])
    journal_before = _booking_journal_rows()

    assert cli.main(["plan-book"]) == 0

    out = _stdout(capsys)
    assert _row(post_ids[0]) == before, "предпросмотр изменил строку"
    assert _booking_journal_rows() == journal_before, "предпросмотр записал строку в журнал"
    assert before[1] is None, "минута должна была остаться незабронированной"
    assert '"dry_run": true' in out
    assert '"would_book": 1' in out
    assert "забронировано было бы 1" in out
    assert "ни одна строка не изменена" in out
    assert cluster_ids[0] in out


def test_the_rehearsal_names_the_minute_it_would_write(rows, flag_on, capsys):
    """A rehearsal that cannot say *which* instant it reserves proves nothing."""
    _, cluster_ids, _ = _ordered_queue(rows)

    assert cli.main(["plan-book"]) == 0

    report = _report_of(_stdout(capsys))
    decision = _decision(report, cluster_ids[0])
    assert decision["state"] == cli.BOOKING_WOULD_BOOK
    assert decision["slot_at"] == DUE_SLOT.isoformat()
    assert decision["reasons"] == ["тест W22-D"]
    assert decision["post_public_id"] == rows.post_ids[0]
    assert report["would_book"] == 1
    assert report["considered"] >= 1


def test_the_preview_never_claims_the_writing_passs_own_outcome(rows, flag_on):
    """`booked` and `claim_lost` are facts about a write; a preview has neither.

    The first would read as a reservation that never happened and the second as a
    race the rehearsal could not have observed — and an absent key is honest where
    a zero would be invented.
    """
    _ordered_queue(rows)

    report = cli.preview_plan_booking()

    assert "booked" not in report, "у предпросмотра нет права на счётчик записанного"
    assert s4_generation.PLAN_BOOKING_CLAIM_LOST not in report["states"]


def test_the_limit_bounds_the_rehearsal_like_the_pass_bounds_the_run(rows, flag_on):
    _ordered_queue(rows, count=3)

    narrow = cli.preview_plan_booking(limit=2)

    assert narrow["considered"] == 2
    assert len(narrow["decisions"]) == 2
    assert narrow["limit"] == 2


# ------------------------------------------------------- the flag-off rehearsal
def test_the_rehearsal_judges_with_the_switch_off_and_writes_nothing(rows, flag_off, capsys):
    """Wave 18's rule, applied to booking: the switch gates writes, not looking."""
    _, cluster_ids, post_ids = _ordered_queue(rows)
    before = _row(post_ids[0])

    assert cli.main(["plan-book"]) == 0

    out = _stdout(capsys)
    report = _report_of(out)
    assert report["flag_enabled"] is False
    assert _decision(report, cluster_ids[0])["state"] == cli.BOOKING_WOULD_BOOK
    assert report["would_book"] == 1
    assert _row(post_ids[0]) == before, "предпросмотр при выключенном флаге изменил строку"
    assert "PLAN_EXECUTION_ENABLED выключен" in out


def test_apply_with_the_switch_off_writes_no_row(rows, flag_off, capsys):
    """The second half of "writes nothing": consent without a switch is not a write."""
    _, _cluster_ids, post_ids = _ordered_queue(rows)
    before = _row(post_ids[0])
    journal_before = _booking_journal_rows()

    assert cli.main(["plan-book", "--apply"]) == 0

    out = _stdout(capsys)
    report = _report_of(out)
    assert report["booked"] == 0
    assert report["states"] == {s4_generation.PLAN_BOOKING_DISABLED: 1}
    assert _row(post_ids[0]) == before
    assert _booking_journal_rows() == journal_before
    assert s4_generation.PLAN_BOOKING_DISABLED in out
    assert "ни одна строка не изменена" in out


# ---------------------------------------------------------------- the --apply run
def test_apply_with_the_switch_on_writes_the_one_column_the_pass_owns(
    rows, flag_on, capsys
):
    """`--apply` is the pass, and the pass writes a minute and nothing else."""
    _, cluster_ids, post_ids = _ordered_queue(rows)

    assert cli.main(["plan-book", "--apply"]) == 0

    report = _report_of(_stdout(capsys))
    assert report["booked"] >= 1
    assert report["states"][s4_generation.PLAN_BOOKING_BOOKED] >= 1
    status, booked, scheduled, published = _row(post_ids[0])
    assert booked is not None
    assert booked.replace(tzinfo=UTC) == DUE_SLOT
    assert status == "gated", "бронирование не двигает статус"
    assert scheduled is None, "бронирование не ставит пост в очередь"
    assert published is None, "бронирование не публикует"
    _decision(report, cluster_ids[0])


def test_apply_says_what_it_wrote_and_which_minute(rows, flag_on, capsys):
    _, cluster_ids, post_ids = _ordered_queue(rows)

    assert cli.main(["plan-book", "--apply"]) == 0

    out = _stdout(capsys)
    assert "забронировано 1" in out
    assert DUE_SLOT.isoformat() in out
    assert cluster_ids[0] in out
    assert post_ids[0] in out


def test_a_second_apply_reports_the_order_as_answered_and_leaves_the_minute(
    rows, flag_on, capsys
):
    """Written once, never again — the column's own contract, read through the verb."""
    _, _cluster_ids, post_ids = _ordered_queue(rows)

    assert cli.main(["plan-book", "--apply"]) == 0
    first = _row(post_ids[0])[1]
    _stdout(capsys)

    assert cli.main(["plan-book", "--apply"]) == 0
    out = _stdout(capsys)

    assert _row(post_ids[0])[1] == first, "второй проход перезаписал забронированную минуту"
    assert "открытых заказов у плана нет" in out


# -------------------------------------------------------------------- the zeroes
def test_a_produce_plan_names_the_reason_instead_of_a_bare_zero(rows, flag_on, capsys):
    """"Nothing to book here" is a verdict about the plan, and it has to read so."""
    _, cluster_ids, post_ids = _ordered_queue(rows, action="produce", slot_at=None)
    before = _row(post_ids[0])

    assert cli.main(["plan-book"]) == 0

    out = _stdout(capsys)
    report = _report_of(out)
    decision = _decision(report, cluster_ids[0])
    assert decision["state"] == s4_generation.PLAN_BOOKING_NO_ENTRY
    assert decision["action"] == "produce"
    assert report["would_book"] == 0
    assert _row(post_ids[0]) == before
    assert s4_generation.PLAN_BOOKING_NO_ENTRY in out
    assert "produce-запись минуту не резервирует" in out


def test_an_unreadable_minute_is_not_reported_as_a_missing_entry(rows, flag_on):
    """A `schedule` whose minute cannot be read is the plan's fault, not no plan."""
    cluster_id = _cluster(rows)
    channel_id = _channel(rows)
    _plan(
        rows,
        [
            {
                "cluster_public_id": cluster_id,
                "action": "schedule",
                "channel_id": channel_id,
                "platform": "telegram",
                "slot_at": None,
            }
        ],
    )
    _post(rows, channel_id, cluster_id)

    report = cli.preview_plan_booking()

    decision = _decision(report, cluster_id)
    assert decision["state"] == s4_generation.PLAN_BOOKING_UNREADABLE_SLOT
    assert decision["action"] == "schedule"
    assert decision["slot_at"] is None
    assert report["would_book"] == 0


def test_a_store_without_a_plan_says_the_plan_is_missing(rows, flag_on):
    """No active plan is a different fact from a plan with nothing in it."""
    cluster_id = _cluster(rows)
    _post(rows, _channel(rows), cluster_id)

    report = cli.preview_plan_booking()

    decision = _decision(report, cluster_id)
    assert decision["state"] == s4_generation.PLAN_BOOKING_NO_PLAN
    assert decision["plan_public_id"] is None
    assert report["would_book"] == 0


def test_no_open_order_is_a_measured_zero_and_not_a_silence():
    """`considered=0` has its own sentence, and it is not "the pass broke"."""
    line = cli._booking_states_line({}, booked_key=cli.BOOKING_WOULD_BOOK)

    assert "измеренный ноль" in line
    assert "0" not in line
    assert "не удалось" not in line


# ---------------------------------------------------------------------- the traps
@pytest.fixture()
def transports_locked(monkeypatch):
    """Neither path may reach a transport, a scheduler or a publisher."""
    from tgfarm import s7_publish

    def trap(name):
        def _explode(*args, **kwargs):
            raise AssertionError(f"plan-book дошёл до {name}")

        return _explode

    for name in (
        "_publish_one",
        "publish_due",
        "run",
        "unpublish",
        "schedule_ready_posts",
        "_publish_vk",
        "_publish_dzen",
    ):
        monkeypatch.setattr(s7_publish, name, trap(name))


def test_plan_book_never_reaches_a_transport(rows, flag_on, transports_locked, capsys):
    _ordered_queue(rows)

    assert cli.main(["plan-book"]) == 0
    assert cli.main(["plan-book", "--apply"]) == 0
    _stdout(capsys)


def test_plan_book_never_reaches_the_network(rows, flag_on, monkeypatch, capsys):
    """Offline means no client object is even constructed on this path."""
    import httpx

    def _no_http(*args, **kwargs):
        raise AssertionError("plan-book полез в сеть")

    monkeypatch.setattr(httpx, "Client", _no_http)
    monkeypatch.setattr(httpx, "AsyncClient", _no_http)
    _ordered_queue(rows)

    assert cli.main(["plan-book"]) == 0
    assert cli.main(["plan-book", "--apply"]) == 0
    _stdout(capsys)
