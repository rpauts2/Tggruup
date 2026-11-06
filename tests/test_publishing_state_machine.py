"""The publishing state machine: which transition the publish path has earned.

Live `posts.status` on 2026-11-05 is `rejected` 18705, `published` 471, `draft`
432, `failed` 365, `scheduled` 298, `publish_unknown` 51, `gated` 47. Every
evidence number downstream — `format_performance`, `measurement_coverage`, the S12
lessons, the queue card, the backlog card — divides by one of those words, so a
transition that writes a state the farm did not earn is a measurement lie, not a
cosmetic bug. `publish_unknown` is the worst one to lose: its whole meaning is "we
sent it and cannot tell whether the platform took it — a human has not looked yet".

The table below was enumerated from this module's own code at a8987af, not from
prose. Thirteen transitions write `posts.status`, and the guards were uneven:

* `claim`                  `_claim_post`      — conditional UPDATE `WHERE status='scheduled'`
* `stuck_watchdog`         `_requeue_stuck`   — conditional UPDATE `WHERE status='publishing'`
* `manual_unpublish`       `unpublish`        — conditional UPDATE `WHERE status IN ('scheduled','gated')`
* `first_slot`      `schedule_ready_posts`     — loaded `WHERE status='gated'`, Python side
* `unsubmitted_requeue`/`_expire`/`_park`
                  `requeue_unsubmitted`        — Python re-read refusing anything but
  `scheduled`/`failed`, and any row already carrying `published_at`
* `success`, `success_without_evidence`,
  `ambiguous_failure`, `terminal_failure`,
  `retryable_failure`, `cancelled_send`
                  `_publish_one`              — **no precondition at all.** Each did
  `session.get(post_id)` and assigned `posts.status` whatever the row happened to
  say. One guard existed there (the cancellation branch); five writes did not.

Those are what this file pins. Before them, a row the heartbeat watchdog had
parked could be silently rewritten to `published` by a worker still sitting in a
browser; an operator's `confirm_published` could be erased to `failed` by that same
worker's timeout; and a publisher returning `ok` with nothing to point at could
still stamp `published` — the shape `maintenance.publication_evidence_repair` was
written to clean up after the fact (15 Telegram rows on 2026-10-26, every one
`tg_message_id IS NULL`).

Verdicts come from the rule, never from what the publisher does today:

> A publish-path transition is legal only if this worker earned it — the row is in
> the state that transition's own claim put it in — and, for `published`, only if
> the destination holds something this database can point at.

Every cell runs a real call against a real row. Every *forbidden* cell is paired
with a control row in the same run that must move, so a green assertion can never
mean "the writer happened not to run". Offline by construction, like every
`test_publish_*` file: all three senders are recorders, the bot token is a
plainly-labelled sentinel, and no cell reads `.env`.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import get_args
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from tgfarm.core.errors import PublishError
from tgfarm.core.schemas import CrosspostResult, PostStatus
from tgfarm.db.models import Channel, CrosspostLog, OperatorLog, Post
from tgfarm.db.session import session_scope
from tgfarm.s7_publish import (
    CROSSPOST_ONLY_STATUSES,
    POST_STATUSES,
    SEND_AUTHORIZING_STATUSES,
    STATUS_DRAFT,
    STATUS_FAILED,
    STATUS_GATED,
    STATUS_PUBLISH_UNKNOWN,
    STATUS_PUBLISHED,
    STATUS_PUBLISHING,
    STATUS_REJECTED,
    STATUS_SCHEDULED,
)

#: The eight states the publish path can leave a row in, read out of the domain
#: contract so a state added there has to be walked here too.
STATES: tuple[str, ...] = tuple(sorted(POST_STATUSES))

CHANNEL_ARGS = {"title": "state machine", "niche": "general", "enabled": True}
REAL_TG_CHAT = -1002003004005
#: Names "a token is configured" without being one.
TOKEN_SET = "statemachine-bot-token-configured"
#: Far in the past, so the scheduling layer's own `ORDER BY created_at` picks this
#: file's rows instead of the suite's residue.
ANCIENT = datetime(2000, 1, 1, tzinfo=UTC)

PRE_SUBMIT_ERROR = "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://dzen.ru/x"
DRIVER_DIED_ERROR = "Connection closed while reading from the driver"
#: What `_requeue_stuck` leaves behind when it parks a stale `publishing` row, word
#: for word. A worker that loses that race may not annotate the park it did not make.
PARK_REASON = (
    "outcome unknown after 15 min without heartbeat; verify platform before retry"
)

#: The six `_publish_one` outcomes. All five of these went to the row unguarded.
SEND_TRANSITIONS = (
    "success",
    "success_without_evidence",
    "ambiguous_failure",
    "terminal_failure",
    "retryable_failure",
    "cancelled_send",
)

_seeded_channels: list[str] = []
_seeded_public_ids: list[str] = []


# ------------------------------------------------------------------ the columns
#: The thirteen transitions the enumeration found.
TRANSITIONS = (
    "claim",
    *SEND_TRANSITIONS,
    "stuck_watchdog",
    "unsubmitted_requeue",
    "unsubmitted_expire",
    "unsubmitted_park",
    "first_slot",
    "manual_unpublish",
)

#: from-state -> transition -> the state the row must hold afterwards. Anything
#: not listed must come back byte-identical.
LEGAL: dict[str, dict[str, str]] = {
    STATUS_SCHEDULED: {
        "claim": STATUS_PUBLISHING,
        "success": STATUS_PUBLISHED,
        "success_without_evidence": STATUS_PUBLISH_UNKNOWN,
        "ambiguous_failure": STATUS_PUBLISH_UNKNOWN,
        "terminal_failure": STATUS_FAILED,
        "retryable_failure": STATUS_SCHEDULED,
        "cancelled_send": STATUS_PUBLISH_UNKNOWN,
        "unsubmitted_requeue": STATUS_SCHEDULED,
        "unsubmitted_expire": STATUS_FAILED,
        "unsubmitted_park": STATUS_PUBLISH_UNKNOWN,
        "manual_unpublish": STATUS_SCHEDULED,
    },
    STATUS_GATED: {"first_slot": STATUS_SCHEDULED, "manual_unpublish": STATUS_SCHEDULED},
    STATUS_PUBLISHING: {"stuck_watchdog": STATUS_PUBLISH_UNKNOWN},
    # `failed` is re-claimable only by the operator-run requeue tool, into one of
    # the three verdicts that tool declares — never by a publisher.
    STATUS_FAILED: {
        "unsubmitted_requeue": STATUS_SCHEDULED,
        "unsubmitted_expire": STATUS_FAILED,
        "unsubmitted_park": STATUS_PUBLISH_UNKNOWN,
    },
    # These four already carry a verdict. Nothing in the publish path may move
    # them, and `publish_unknown` / `published` least of all.
    STATUS_PUBLISHED: {},
    STATUS_PUBLISH_UNKNOWN: {},
    STATUS_REJECTED: {},
    STATUS_DRAFT: {},
}

#: The state a control row starts in, chosen so the same call visibly moves it.
CONTROL_STATE = {
    "claim": STATUS_SCHEDULED,
    "success": STATUS_SCHEDULED,
    "success_without_evidence": STATUS_SCHEDULED,
    "ambiguous_failure": STATUS_SCHEDULED,
    "terminal_failure": STATUS_SCHEDULED,
    "retryable_failure": STATUS_SCHEDULED,
    "cancelled_send": STATUS_SCHEDULED,
    "stuck_watchdog": STATUS_PUBLISHING,
    "unsubmitted_requeue": STATUS_FAILED,
    "unsubmitted_expire": STATUS_FAILED,
    "unsubmitted_park": STATUS_FAILED,
    "first_slot": STATUS_GATED,
    "manual_unpublish": STATUS_GATED,
}


@dataclass(frozen=True)
class Cell:
    transition: str
    from_state: str
    to_state: str
    earned: bool

    @property
    def id(self) -> str:
        return f"{self.from_state}->{self.transition}:{self.to_state}"


def _cells() -> list[Cell]:
    out = []
    for transition in TRANSITIONS:
        for state in STATES:
            legal = LEGAL.get(state, {})
            # "earned" is the table's judgement, not a string comparison: some legal
            # transitions leave the status word alone (`retryable_failure` re-arms a
            # `scheduled` row, `unsubmitted_expire` fails it again) and still write.
            out.append(
                Cell(
                    transition,
                    state,
                    legal.get(transition, state),
                    transition in legal,
                )
            )
    return out


CELLS = _cells()


# ------------------------------------------------------------------ row helpers
def _slug() -> str:
    return uuid4().hex[:8]


def _seed_channel(platform: str = "telegram") -> str:
    channel_id = f"sm_{platform}_{_slug()}"
    with session_scope() as session:
        session.merge(Channel(id=channel_id, platform=platform, tg_chat_id=REAL_TG_CHAT, **CHANNEL_ARGS))
    _seeded_channels.append(channel_id)
    return channel_id


def _seed_post(
    state: str,
    *,
    platform: str = "telegram",
    error: str | None = None,
    attempts: int = 0,
    created_at: datetime = ANCIENT,
    journal_posted: bool = False,
) -> tuple[int, str]:
    channel_id = _seed_channel(platform)
    public_id = f"sm{_slug()}"
    published_at = ANCIENT if state == STATUS_PUBLISHED else None
    scheduled_at = ANCIENT if state in (STATUS_SCHEDULED, STATUS_PUBLISHING) else None
    with session_scope() as session:
        row = Post(
            public_id=public_id,
            channel_id=channel_id,
            hook="Хук машины состояний.",
            body="Тело поста машины состояний.",
            status=state,
            scheduled_at=scheduled_at,
            published_at=published_at,
            publish_error=error,
            attempts=attempts,
            created_at=created_at,
            # A `published` row is seeded with the evidence it is entitled to, so
            # every cell is about the transition rather than about a malformed row.
            tg_message_id=999 if state == STATUS_PUBLISHED else None,
            tg_chat_id=REAL_TG_CHAT if state == STATUS_PUBLISHED and platform == "telegram" else None,
        )
        session.add(row)
        session.flush()
        post_id = int(row.id)
    if journal_posted:
        # Dzen and VK leave their identifier in the journal, never on the post.
        with session_scope() as session:
            session.add(
                CrosspostLog(
                    post_public_id=public_id,
                    platform=platform,
                    status="posted",
                    remote_id=None,
                    url=None,
                )
            )
    _seeded_public_ids.append(public_id)
    return post_id, public_id


OUTCOME_FIELDS = (
    "status",
    "published_at",
    "tg_message_id",
    "tg_chat_id",
    "attempts",
    "publish_error",
    "publish_heartbeat_at",
    "scheduled_at",
)


def _read(post_id: int) -> dict:
    with session_scope() as session:
        row = session.get(Post, post_id)
        assert row is not None
        return {field: getattr(row, field) for field in OUTCOME_FIELDS}


def _status(post_id: int) -> str:
    return str(_read(post_id)["status"])


# ------------------------------------------------------------------ the senders
class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _HttpError(RuntimeError):
    """Shaped like the `httpx` errors the real publishers raise."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"client error {status_code}")
        self.response = _Response(status_code)


class _Ambiguous(RuntimeError):
    """A read timeout after the request had already been sent."""

    def __init__(self) -> None:
        super().__init__("ReadTimeout: timed out while reading the response")


def _flip(post_id: int, state: str) -> None:
    """Simulate another path writing this row *during* the send.

    This is the window the five unguarded outcome writes fell through: the stuck
    detector parks the row, an operator confirms it, a second worker resolves it —
    and the first worker's own write lands afterwards, blind to all of it. Each
    hijack carries the stamp the real writer would have left with it, so a guard
    that merely preserves the word while erasing its evidence still fails here.
    """
    with session_scope() as session:
        row = session.get(Post, post_id)
        row.status = state
        if state == STATUS_PUBLISHED:
            row.published_at = datetime.now(UTC)
            row.tg_message_id = 13579
            row.publish_error = None
        elif state == STATUS_PUBLISH_UNKNOWN:
            row.publish_heartbeat_at = None
            row.publish_error = PARK_REASON


async def _noop_heartbeat(*_args, **_kwargs) -> None:
    return None


class _CadencePinned:
    """The real schedule with one cadence mode named out loud.

    Which branch `schedule_ready_posts` takes is decided by
    `config.schedule.cadence["mode"]`, and the mode reaching the tick comes from a
    process-wide `lru_cache` that `tests/conftest.py:257` reloads rather than
    clears. A test that must run the interval branch therefore has to say so
    itself; inheriting it from the neighbour that cached last is how the same
    file passes alone and fails in a full run. Everything else — hours, day
    parts — is delegated to the real object, so this pins one key and not a
    fixture-wide fiction.
    """

    def __init__(self, inner: object, cadence: dict) -> None:
        self._inner = inner
        self.cadence = cadence

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def _world(monkeypatch) -> None:
    """Pin the two facts `_publish_one` asks about; stub the persona lookup."""
    from tgfarm import s7_publish

    settings = SimpleNamespace(
        live=True,
        telegram_bot_token=TOKEN_SET,
        approval_required=False,
        dzen_requires_llm=True,
        primary_publish_max_attempts=5,
        dzen_min_gap_s=0,
        vk_prime_hours="",
        vk_prime_share=0.0,
    )
    monkeypatch.setattr(s7_publish, "get_settings", lambda: settings)
    monkeypatch.setattr("tgfarm.platforms.is_platform_enabled", lambda _platform: True)
    real_config = s7_publish.get_config()
    monkeypatch.setattr(
        s7_publish,
        "get_config",
        lambda: SimpleNamespace(
            persona=lambda _channel_id: SimpleNamespace(enabled=True, posts_per_day=6),
            schedule=real_config.schedule,
            personas=real_config.personas,
        ),
    )


def _drive_publish(
    monkeypatch,
    post_id: int,
    transition: str,
    *,
    platform: str = "telegram",
    flip_to: str | None = None,
    media: str | None = None,
) -> dict:
    """Run one `_publish_one`, with the sender standing in for the platform."""
    from tgfarm import s7_publish

    calls: list[str] = []

    def observed() -> None:
        if flip_to is not None:
            _flip(post_id, flip_to)

    async def telegram_send(_chat_id, _text, _media=None):
        calls.append("telegram")
        observed()
        if transition in ("success", "hijacked_success"):
            return 777
        if transition == "success_without_evidence":
            # The shape the 15 fabricated rows came back from: `ok`, and nothing
            # on the far end to point at.
            return None
        if transition == "ambiguous_failure":
            raise _Ambiguous()
        if transition == "terminal_failure":
            raise _HttpError(404)
        if transition == "retryable_failure":
            raise _HttpError(429)
        if transition == "cancelled_send":
            raise asyncio.CancelledError
        raise AssertionError(f"no sender outcome for {transition}")

    async def dzen_publish(post_id_arg, _text, _cover=None, **_kw):
        calls.append("dzen")
        observed()
        return {"ok": True, "message_id": None, "remote_id": f"dzen-{post_id_arg}"}

    async def vk_publish(post_id_arg, _text):
        calls.append("vk")
        observed()
        return {"ok": True, "message_id": None, "remote_id": f"vk-{post_id_arg}"}

    if platform == "telegram":
        monkeypatch.setattr(s7_publish, "PUBLISHER", SimpleNamespace(send=telegram_send))
    elif platform == "dzen":
        monkeypatch.setattr(s7_publish, "_publish_dzen", dzen_publish)
    else:
        monkeypatch.setattr(s7_publish, "_publish_vk", vk_publish)
    monkeypatch.setattr(s7_publish, "_publish_heartbeat_loop", _noop_heartbeat)

    target = REAL_TG_CHAT if platform == "telegram" else platform
    try:
        result = asyncio.run(
            s7_publish._publish_one(post_id, target, "Текст машины состояний.", media, platform)
        )
    except asyncio.CancelledError:
        # The cancellation branch parks the row and re-raises on purpose.
        result = {"cancelled": True}
    result["calls"] = calls
    return result


# ------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _leave_no_residue():
    """The suite shares one session database; this file's rows must not outlive it.

    A leftover `publish_unknown` here lands in some other file's denominator,
    which is the same class of error this file is written about.
    """
    yield
    with session_scope() as session:
        if _seeded_public_ids:
            session.execute(
                delete(CrosspostLog).where(CrosspostLog.post_public_id.in_(_seeded_public_ids))
            )
            session.execute(
                delete(OperatorLog).where(OperatorLog.target.in_(_seeded_public_ids))
            )
            ids = list(
                session.scalars(select(Post.id).where(Post.public_id.in_(_seeded_public_ids))).all()
            )
            if ids:
                session.execute(delete(Post).where(Post.id.in_(ids)))
        if _seeded_channels:
            session.execute(delete(Channel).where(Channel.id.in_(_seeded_channels)))
    _seeded_channels.clear()
    _seeded_public_ids.clear()


# ---------------------------------------------------------------- the drivers
def _apply(cell: Cell, monkeypatch) -> tuple[int, int, dict, dict]:
    """Run one cell against a subject row and a control row."""
    from tgfarm import s7_publish

    error: str | None = None
    attempts = 0
    created_at = ANCIENT
    if cell.transition in ("unsubmitted_requeue", "unsubmitted_expire", "unsubmitted_park"):
        error = DRIVER_DIED_ERROR if cell.transition == "unsubmitted_park" else PRE_SUBMIT_ERROR
        # Sorts first inside the requeue tool's own `ORDER BY attempts DESC`, so
        # the cell is about this row and not about the suite's residue.
        attempts = 10**6
        if cell.transition == "unsubmitted_expire":
            # Past `max_age_hours`, which is the only thing that makes the tool
            # choose `failed` over `scheduled`.
            created_at = datetime.now(UTC) - timedelta(hours=150)
        else:
            created_at = datetime.now(UTC) - timedelta(hours=1)

    subject_id, subject_public = _seed_post(
        cell.from_state, error=error, attempts=attempts, created_at=created_at
    )
    control_id, control_public = _seed_post(
        CONTROL_STATE[cell.transition],
        error=error,
        attempts=max(0, attempts - 1),
        created_at=created_at,
    )

    if cell.transition == "stuck_watchdog":
        # The watchdog measures from the heartbeat, so age the rows past the
        # window rather than faking the clock the code reads.
        stale = datetime.now(UTC) - timedelta(minutes=999)
        with session_scope() as session:
            for row_id in (subject_id, control_id):
                row = session.get(Post, row_id)
                row.scheduled_at = stale
                row.publish_heartbeat_at = stale

    subject_before = _read(subject_id)
    control_before = _read(control_id)

    if cell.transition in SEND_TRANSITIONS:
        _world(monkeypatch)
        _drive_publish(monkeypatch, subject_id, cell.transition)
        _drive_publish(monkeypatch, control_id, cell.transition)
    elif cell.transition == "claim":
        s7_publish._claim_post(subject_id)
        s7_publish._claim_post(control_id)
    elif cell.transition == "stuck_watchdog":
        s7_publish._requeue_stuck(minutes=15)
    elif cell.transition in ("unsubmitted_requeue", "unsubmitted_expire", "unsubmitted_park"):
        s7_publish.requeue_unsubmitted(
            platform="telegram", limit=200, apply=True, max_age_hours=48.0
        )
    elif cell.transition == "first_slot":
        _world(monkeypatch)
        s7_publish.schedule_ready_posts(limit=5, blocked=s7_publish.empty_gate_report())
    elif cell.transition == "manual_unpublish":
        s7_publish.unpublish([subject_public, control_public])
    else:  # pragma: no cover — ids come from TRANSITIONS
        raise AssertionError(f"no driver for {cell.transition}")

    return subject_id, control_id, subject_before, control_before


@pytest.mark.parametrize("cell", CELLS, ids=[c.id for c in CELLS])
def test_transition_matrix(cell, monkeypatch):
    """One row per state, one column per transition, one verdict per cell."""
    subject_id, control_id, subject_before, control_before = _apply(cell, monkeypatch)

    after = _status(subject_id)
    assert after == cell.to_state, f"{cell.id} left the row at {after!r}"
    if cell.earned:
        return

    # Forbidden. First prove the writer actually ran this call, on the control
    # row: a green "nothing happened" on a writer that never fired is not evidence.
    control_after = _read(control_id)
    expected_control = LEGAL[CONTROL_STATE[cell.transition]][cell.transition]
    assert control_after["status"] == expected_control, (
        f"{cell.id}: the control row did not move either, so the writer was not run: "
        f"{control_after}"
    )
    assert control_after != control_before, f"{cell.id}: control untouched: {control_after}"

    # And then that the subject came back byte-identical. Not merely the same
    # status: an outcome write that leaves the status alone but stamps a fresh
    # `publish_error` still records a verdict on an attempt it did not make.
    assert _read(subject_id) == subject_before, (
        f"{cell.id} wrote to a row it never claimed: {_read(subject_id)}"
    )


def test_the_matrix_survives_a_neighbours_vk_row_in_the_shared_store(monkeypatch):
    """The settings stub must answer the whole tick, not only the rows this file seeds.

    Measured, not guessed. On 2026-11-06 a whole-suite run lost eight
    `first_slot` cells to `AttributeError: 'types.SimpleNamespace' object has no
    attribute 'vk_prime_hours'` at `tgfarm/s7_publish/__init__.py:251`, and the
    same file run alone passed. Neither the cadence nor the matrix changed:
    `schedule_ready_posts` selects `status='gated' AND scheduled_at IS NULL` with
    no channel filter (`tgfarm/s7_publish/__init__.py:395`–`:400`), so in the
    shared session store (`tests/conftest.py:242`) the tick also picks up a
    neighbour's gated row — and on a VK channel the interval branch calls
    `_maybe_prime_slot`, which asks for `vk_prime_hours` and `vk_prime_share`.
    `_world` pinned the six fields the send path reads, so it was the fake, not
    the product, that crashed. Two sibling files already carry both fields for
    exactly this reason (`tests/test_w19d_booked_slot.py:187`,
    `tests/test_w22c_booked_slot_rebinding.py:191`); this pins the third, and
    pins it through the call that actually failed rather than by hoping the
    neighbour stays quiet.
    """
    from tgfarm import s7_publish

    _seed_post(STATUS_GATED, platform="vk")
    subject_id, _public = _seed_post(STATUS_GATED)
    before = _status(subject_id)
    _world(monkeypatch)
    world = s7_publish.get_config()
    monkeypatch.setattr(
        s7_publish,
        "get_config",
        lambda: SimpleNamespace(
            persona=world.persona,
            personas=world.personas,
            schedule=_CadencePinned(
                world.schedule, {"mode": "interval", "min_gap_s": 300, "max_gap_s": 620}
            ),
        ),
    )

    gates = s7_publish.schedule_ready_posts(limit=5, blocked=s7_publish.empty_gate_report())

    assert before == STATUS_GATED, before
    assert _status(subject_id) == STATUS_SCHEDULED, (
        f"a row this file seeded was left gated by a tick that also saw a neighbour: {gates}"
    )


# ---------------------------------------------------- the two headline questions
#: A send whose row this worker no longer owns. The other path's verdict stands —
#: unless that verdict is "still queued", which would put the same material on the
#: platform a second time.
def _takeover_expectation(transition: str, hijack: str) -> str:
    if hijack == STATUS_PUBLISHING:
        # No takeover at all: the guard must not fire, or it would refuse every
        # real publication.
        return {
            "success": STATUS_PUBLISHED,
            "success_without_evidence": STATUS_PUBLISH_UNKNOWN,
            "ambiguous_failure": STATUS_PUBLISH_UNKNOWN,
            "terminal_failure": STATUS_FAILED,
            "retryable_failure": STATUS_SCHEDULED,
            "cancelled_send": STATUS_PUBLISH_UNKNOWN,
        }[transition]
    if transition == "success" and hijack in SEND_AUTHORIZING_STATUSES:
        return STATUS_PUBLISH_UNKNOWN
    return hijack


@pytest.mark.parametrize("hijack", STATES)
@pytest.mark.parametrize("transition", ["success", "ambiguous_failure", "cancelled_send"])
def test_a_row_taken_over_between_claim_and_outcome_keeps_its_verdict(
    monkeypatch, transition, hijack
):
    """The exact window the five unguarded writes fell through.

    The row is legitimately `scheduled`, this worker legitimately claims it, the
    platform call legitimately returns — and something else wrote the row while
    this one was waiting. That other verdict is the record; this worker's is a
    second opinion nobody asked for, and the two disagree.
    """
    subject_id, _public = _seed_post(STATUS_SCHEDULED)
    _world(monkeypatch)
    result = _drive_publish(monkeypatch, subject_id, transition, flip_to=hijack)

    assert result["calls"], "the cell never reached the sender, so it proves nothing"
    after = _status(subject_id)
    assert after == _takeover_expectation(transition, hijack), (
        f"{transition} rewrote a {hijack!r} row to {after!r}"
    )
    assert after != STATUS_PUBLISHED or hijack == STATUS_PUBLISHED or (
        hijack == STATUS_PUBLISHING and transition == "success"
    )
    if transition == "success" and hijack != STATUS_PUBLISHING:
        assert result.get("ok") is False, result
        assert result.get("outcome_not_recorded") is True, result
    if transition == "cancelled_send":
        assert result.get("cancelled") is True, result


def test_publish_unknown_is_not_silently_promoted_by_the_stuck_detector_race(
    monkeypatch,
):
    """End-to-end form of the race the 51 live rows depend on.

    `_requeue_stuck` is the heartbeat watchdog: it parks a `publishing` row whose
    heartbeat went stale. A browser publish that then finishes successfully used to
    stamp `published` over that park, which is precisely the row the operator is
    looking for and would now never find.
    """
    subject_id, _public = _seed_post(STATUS_SCHEDULED)
    _world(monkeypatch)
    result = _drive_publish(
        monkeypatch, subject_id, "success", flip_to=STATUS_PUBLISH_UNKNOWN
    )

    assert result["ok"] is False, result
    assert result["outcome_not_recorded"] is True, (
        "a refused outcome that still reported success is a phantom publication"
    )
    assert "publication not recorded" in str(result["error"]), result
    assert _status(subject_id) == STATUS_PUBLISH_UNKNOWN
    after = _read(subject_id)
    assert after["published_at"] is None, "a promotion to published leaves a stamp behind"
    assert after["tg_message_id"] is None, after
    # The refusal travels in the return value and the log, not in the row: a worker
    # that lost the race has no right to annotate the verdict it did not reach, and
    # `_requeue_stuck` already wrote the reason this row is waiting for a human.
    assert after["publish_error"] == PARK_REASON, after


def test_a_failed_row_cannot_be_written_over_a_published_one(monkeypatch):
    """The reverse direction, and the operator-confirmation race it comes from.

    `api._reconcile_publish_outcome` moves `publish_unknown -> published` on a
    human's word. The same post can still be in flight in a worker that then
    times out; that worker's `failed` used to erase the confirmation.
    """
    subject_id, _public = _seed_post(STATUS_SCHEDULED)
    _world(monkeypatch)
    result = _drive_publish(
        monkeypatch, subject_id, "ambiguous_failure", flip_to=STATUS_PUBLISHED
    )

    assert _status(subject_id) == STATUS_PUBLISHED, result
    after = _read(subject_id)
    assert after["tg_message_id"] == 13579, "the confirmed evidence was overwritten"
    assert after["published_at"] is not None

    # The same erasure through the terminal branch, which used to write `failed`.
    subject_id, _public = _seed_post(STATUS_SCHEDULED)
    result = _drive_publish(monkeypatch, subject_id, "terminal_failure", flip_to=STATUS_PUBLISHED)
    assert _status(subject_id) == STATUS_PUBLISHED, result
    assert "terminal_failure" not in str(_read(subject_id)["publish_error"])
    assert result["ok"] is False and result["outcome_not_recorded"] is True, result


# --------------------------------------------------------------- evidence rules
def test_published_requires_something_the_destination_can_point_at(monkeypatch):
    """`published` is a claim about a remote system, so it needs a witness.

    Four cells, because there are three publisher shapes and the guard must not
    become "require a Telegram id": Dzen and VK hand their identifier to the
    crosspost journal, never to the post row, and `maintenance` already counts a
    journalled `posted` row as evidence for 29 of the 37 live Dzen publications.
    """
    from tgfarm import s7_publish

    # 1. A Telegram `ok` with no message id is not a publication.
    post_id, _public = _seed_post(STATUS_SCHEDULED)
    _world(monkeypatch)
    result = _drive_publish(monkeypatch, post_id, "success_without_evidence")
    assert result["ok"] is False, result
    after = _read(post_id)
    assert after["status"] == STATUS_PUBLISH_UNKNOWN, after
    assert after["published_at"] is None, after
    assert after["tg_message_id"] is None, after
    assert "no remote identifier" in str(after["publish_error"]), after

    # 2. The `dry-*` placeholder the off-mode adapters invent is not evidence.
    post_id, _public = _seed_post(STATUS_SCHEDULED, platform="vk")

    async def _dry_placeholder(post_arg, _text):
        return {"ok": True, "message_id": None, "remote_id": f"dry-vk-{post_arg}"}

    monkeypatch.setattr(s7_publish, "_publish_vk", _dry_placeholder)
    monkeypatch.setattr(s7_publish, "_publish_heartbeat_loop", _noop_heartbeat)
    _world(monkeypatch)
    result = asyncio.run(s7_publish._publish_one(post_id, "vk", "Текст.", None, "vk"))
    assert result["ok"] is False, result
    assert _status(post_id) == STATUS_PUBLISH_UNKNOWN, _read(post_id)

    # 3. A journalled `posted` row with no id at all still publishes. Dzen hands
    # its identifier to `crosspost_logs` and never to the post row, and 29 of the
    # 37 live Dzen publications have exactly that single witness.
    post_id, _public = _seed_post(
        STATUS_SCHEDULED, platform="dzen", journal_posted=True
    )
    result = _drive_publish(monkeypatch, post_id, "success", platform="dzen", media="cover.jpg")
    assert result["ok"] is True, result
    after = _read(post_id)
    assert after["status"] == STATUS_PUBLISHED, after
    assert after["tg_message_id"] is None, "Dzen never had a Telegram id to write"

    # 4. A real Telegram id still publishes, with the id the sender reported.
    post_id, _public = _seed_post(STATUS_SCHEDULED)
    result = _drive_publish(monkeypatch, post_id, "success")
    assert result["ok"] is True and result["calls"] == ["telegram"], result
    after = _read(post_id)
    assert after["status"] == STATUS_PUBLISHED, after
    assert after["tg_message_id"] == 777, "the id the sender reported is the evidence"


def test_the_tick_does_not_count_a_refused_outcome_as_a_publication(monkeypatch):
    """`published: 1` has to mean one row that actually is `published`.

    A refusal that still returned `ok` would let `_publish_due_once` increment
    `sent`, and the number the panel reads would stop being the number the column
    holds.
    """
    from tgfarm import s7_publish

    hijacked, _h_public = _seed_post(STATUS_SCHEDULED)
    queued, _q_public = _seed_post(STATUS_SCHEDULED)
    _world(monkeypatch)
    monkeypatch.setattr(
        s7_publish,
        "due_posts",
        lambda *a, **k: [
            (hijacked, "Текст.", REAL_TG_CHAT, "telegram"),
            (queued, "Текст.", REAL_TG_CHAT, "telegram"),
        ],
    )
    monkeypatch.setattr(s7_publish, "_media_for", lambda _ids: {})
    monkeypatch.setattr(s7_publish, "_inline_media_for", lambda _ids: {})

    async def _send_that_loses_its_row(_chat_id, _text, _media=None):
        _flip(hijacked, STATUS_PUBLISH_UNKNOWN)
        return 888

    monkeypatch.setattr(s7_publish, "PUBLISHER", SimpleNamespace(send=_send_that_loses_its_row))
    report = asyncio.run(s7_publish._publish_due_once(limit=10))

    assert report["published"] == 1, report
    assert report["failed"] == 1, report
    assert any("not recorded" in str(err) for err in report["errors"]), report["errors"]
    assert _status(hijacked) == STATUS_PUBLISH_UNKNOWN
    assert _status(queued) == STATUS_PUBLISHED


# ------------------------------------------------------------------ vocabulary
def test_the_persisted_spelling_of_unknown_is_publish_unknown():
    """Which word wins, established in code rather than in prose.

    `publish_unknown` is the `posts.status` value: what `PostStatus` declares, what
    the reconciliation board reads, what `maintenance.publication_evidence_repair`
    writes, and what 51 live rows already hold. `unknown` is
    `crosspost_logs.status`, a different table. The overlap between the two
    vocabularies is stated here as data, because two files currently read them as
    if they were one.
    """
    from tgfarm import s7_publish

    assert s7_publish.POST_STATUSES == frozenset(STATES) == frozenset(get_args(PostStatus))
    assert s7_publish.STATUS_PUBLISH_UNKNOWN == "publish_unknown"
    assert s7_publish.STATUS_PUBLISHED == "published"

    journal = frozenset(get_args(CrosspostResult.model_fields["status"].annotation))
    # Two vocabularies, two tables. They agree on exactly these two words, and on
    # nothing else — which is why reading one as the other is a silent bug:
    # `distribution.py:741` counts the journal's, `api.py:1446` counts the column's.
    assert journal & s7_publish.POST_STATUSES == {STATUS_SCHEDULED, STATUS_FAILED}
    assert CROSSPOST_ONLY_STATUSES == journal - {STATUS_SCHEDULED, STATUS_FAILED}
    assert CROSSPOST_ONLY_STATUSES.isdisjoint(s7_publish.POST_STATUSES)
    assert "unknown" in journal and "unknown" not in s7_publish.POST_STATUSES
    assert "published" in s7_publish.POST_STATUSES and "published" not in journal

    # The API's waiting set is a subset of the declared column vocabulary: the
    # backlog card cannot count a state the publish path never writes.
    from tgfarm.api import BACKLOG_WAITING

    assert set(BACKLOG_WAITING) <= s7_publish.POST_STATUSES, set(BACKLOG_WAITING)
    assert s7_publish.STATUS_PUBLISH_UNKNOWN in BACKLOG_WAITING, (
        "the 51 uncertain rows are only countable while the waiting set spells them"
    )


@pytest.mark.parametrize(
    "spelling",
    ["unknown", "posted", "dry_run", "permanent", "skipped", "publish_Unknown", "published ", ""],
)
def test_a_non_canonical_spelling_is_rejected_by_the_one_helper(spelling):
    """Every status write in this module goes through `canonical_post_status`."""
    from tgfarm import s7_publish

    assert spelling not in s7_publish.POST_STATUSES
    with pytest.raises(PublishError, match="is not a canonical posts.status"):
        s7_publish.canonical_post_status(spelling)


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (STATUS_PUBLISHING, STATUS_PUBLISHED, True),
        (STATUS_PUBLISHING, STATUS_PUBLISH_UNKNOWN, True),
        (STATUS_PUBLISHING, STATUS_FAILED, True),
        (STATUS_PUBLISHING, STATUS_SCHEDULED, True),
        (STATUS_PUBLISHING, "unknown", False),
        (STATUS_PUBLISHING, "posted", False),
        (STATUS_PUBLISHING, STATUS_DRAFT, False),
        (STATUS_PUBLISH_UNKNOWN, STATUS_PUBLISHED, False),
        (STATUS_PUBLISH_UNKNOWN, STATUS_PUBLISH_UNKNOWN, False),
        (STATUS_PUBLISHED, STATUS_PUBLISHED, False),
        (STATUS_PUBLISHED, STATUS_FAILED, False),
        (STATUS_SCHEDULED, STATUS_PUBLISHED, False),
        (None, STATUS_PUBLISHED, False),
        (STATUS_REJECTED, STATUS_SCHEDULED, False),
        (STATUS_GATED, STATUS_PUBLISHED, False),
        (STATUS_FAILED, STATUS_PUBLISHED, False),
    ],
)
def test_the_outcome_predicate_is_a_closed_table(current, target, allowed):
    from tgfarm import s7_publish

    assert s7_publish.publish_outcome_allowed(current, target) is allowed
    assert s7_publish.publisher_owns(current) is (current == STATUS_PUBLISHING)


@pytest.mark.parametrize(
    ("transition", "constant", "forged"),
    [
        ("success", "STATUS_PUBLISHED", "posted"),
        ("ambiguous_failure", "STATUS_PUBLISH_UNKNOWN", "unknown"),
    ],
)
def test_forging_a_vocabulary_into_the_constant_never_persists_it(
    monkeypatch, transition, constant, forged
):
    """The reverse run, kept as a test rather than as a claim.

    Every write names a module constant, so the constants are the attack surface.
    Put the *other* table's word in one and the write must fail loudly with the row
    untouched — not quietly persist `unknown`, which neither the backlog card nor
    the reconciliation board would ever find again.
    """
    from tgfarm import s7_publish

    post_id, _public = _seed_post(STATUS_SCHEDULED)
    _world(monkeypatch)
    monkeypatch.setattr(s7_publish, constant, forged)

    raised = None
    try:
        _drive_publish(monkeypatch, post_id, transition)
    except PublishError as exc:
        raised = exc

    assert _status(post_id) == STATUS_PUBLISHING, "the forged value reached the row"
    if raised is None:
        # `publish_outcome_allowed` consults `PUBLISH_OUTCOME_TARGETS`, and
        # `canonical_post_status` consults the frozen `POST_STATUSES`, so a forged
        # target is refused twice over — by the predicate and by the helper. Both
        # doors hold; neither may persist the word.
        assert _status(post_id) not in (forged, STATUS_PUBLISHED)
        with pytest.raises(PublishError):
            s7_publish.canonical_post_status(forged)


def test_requeue_parks_with_the_column_spelling_not_its_own_action_token():
    """`requeue_unsubmitted` classifies a candidate as `"unknown"` internally.

    That token is the journal's word. What lands in `posts.status` is the column's,
    and a row already parked stays parked: those 51 wait on a human, not on another
    pass of the tool.
    """
    from tgfarm import s7_publish

    parked_id, _parked_public = _seed_post(
        STATUS_PUBLISH_UNKNOWN, error=DRIVER_DIED_ERROR, attempts=10**6,
        created_at=datetime.now(UTC) - timedelta(hours=3),
    )
    fresh_id, fresh_public = _seed_post(
        STATUS_FAILED, error=DRIVER_DIED_ERROR, attempts=10**6 - 1,
        created_at=datetime.now(UTC) - timedelta(hours=3),
    )
    parked_before = _read(parked_id)

    summary = s7_publish.requeue_unsubmitted(
        platform="telegram", limit=200, apply=True, max_age_hours=48.0
    )
    assert fresh_public in summary["park_as_unknown"], summary
    assert _status(fresh_id) == STATUS_PUBLISH_UNKNOWN
    assert _read(parked_id) == parked_before, "a parked row was re-parked"

    with session_scope() as session:
        values = list(
            session.scalars(
                select(Post.status).where(Post.public_id.in_([fresh_public]))
            ).all()
        )
    assert values == [STATUS_PUBLISH_UNKNOWN], values
    assert "unknown" not in values, "the requeue tool's action token reached the column"


# ------------------------------------------------------- the enumeration itself
def test_no_transition_writes_posts_status_without_the_vocabulary():
    """A raw status literal in a write position is how the next fabrication lands.

    Scanning the module's own source is the only way to pin "every writer goes
    through the helper" for a writer that does not exist yet.
    """
    from tgfarm import s7_publish

    src = inspect.getsource(s7_publish)
    orm_assignments = re.findall(r'\b(?:post|row)\.status\s*=\s*"', src)
    assert orm_assignments == [], f"raw status assignment: {orm_assignments}"
    bulk_values = re.findall(r'\.values\(\s*status\s*=\s*"', src)
    assert bulk_values == [], f"raw status in a bulk UPDATE: {bulk_values}"

    published_writes = re.findall(
        r'(?:post|row)\.status\s*=\s*canonical_post_status\(STATUS_PUBLISHED\)', src
    )
    assert len(published_writes) == 1, (
        f"`published` must have exactly one writer in the publish path, found "
        f"{len(published_writes)}"
    )

    for name in (
        "STATUS_DRAFT", "STATUS_GATED", "STATUS_SCHEDULED", "STATUS_PUBLISHING",
        "STATUS_PUBLISH_UNKNOWN", "STATUS_PUBLISHED", "STATUS_REJECTED", "STATUS_FAILED",
    ):
        value = getattr(s7_publish, name)
        assert value in s7_publish.POST_STATUSES, f"{name} = {value!r} is not persistable"
        assert name.removeprefix("STATUS_").lower().replace("_", "") in value.replace("_", "")


@pytest.mark.parametrize(
    ("state", "claimable"),
    [
        (STATUS_SCHEDULED, True),
        (STATUS_PUBLISHING, False),
        (STATUS_PUBLISHED, False),
        (STATUS_PUBLISH_UNKNOWN, False),
        (STATUS_GATED, False),
        (STATUS_DRAFT, False),
        (STATUS_REJECTED, False),
        (STATUS_FAILED, False),
    ],
)
def test_only_a_queued_row_can_be_claimed(state, claimable):
    """`_claim_post` is the transition every other guard in this file is phrased
    against — "you do not hold the claim" is only a fact if claiming is exclusive.
    """
    from tgfarm import s7_publish

    post_id, _public = _seed_post(state)
    before = _read(post_id)
    assert s7_publish._claim_post(post_id) is claimable
    if claimable:
        after = _read(post_id)
        assert after["status"] == STATUS_PUBLISHING
        assert after["attempts"] == before["attempts"] + 1, "the claim is what counts the try"
    else:
        # Not merely the same word: a second `publishing` row must not collect a
        # third attempt, a fresh heartbeat or a re-stamped `scheduled_at`.
        assert _read(post_id) == before, "a row this call may not take was still written"


def test_the_matrix_walks_the_cross_product_it_claims():
    """A guard is only as wide as the product it walks, so the width is asserted."""
    assert STATES == (
        "draft",
        "failed",
        "gated",
        "publish_unknown",
        "published",
        "publishing",
        "rejected",
        "scheduled",
    )
    assert len(TRANSITIONS) == 13
    assert len(CELLS) == 8 * 13 == 104
    assert len({c.id for c in CELLS}) == len(CELLS), "a cell may not be declared twice"

    earned = [c for c in CELLS if c.earned]
    assert len(earned) == sum(len(v) for v in LEGAL.values()) == 17, (
        f"the table and the walk disagree about how many cells are legal: {earned}"
    )
    assert {c.transition for c in earned} == set(TRANSITIONS), "every column needs a positive"
    assert {c.from_state for c in earned} == {
        STATUS_SCHEDULED, STATUS_GATED, STATUS_PUBLISHING, STATUS_FAILED
    }

    # The four states that already carry a verdict may be attempted, never moved.
    for state in (STATUS_PUBLISHED, STATUS_PUBLISH_UNKNOWN, STATUS_REJECTED, STATUS_DRAFT):
        blocked = [c for c in CELLS if c.from_state == state]
        assert len(blocked) == 13
        assert all(not c.earned for c in blocked), (
            f"{state} must be terminal for the publish path: "
            f"{[c.id for c in blocked if c.earned]}"
        )

    # The two questions this file exists for, as named cells.
    for cell_id in (
        f"{STATUS_PUBLISH_UNKNOWN}->success:{STATUS_PUBLISH_UNKNOWN}",
        f"{STATUS_PUBLISH_UNKNOWN}->success_without_evidence:{STATUS_PUBLISH_UNKNOWN}",
        f"{STATUS_PUBLISHED}->terminal_failure:{STATUS_PUBLISHED}",
        f"{STATUS_PUBLISHED}->ambiguous_failure:{STATUS_PUBLISHED}",
        f"{STATUS_PUBLISHED}->stuck_watchdog:{STATUS_PUBLISHED}",
    ):
        cell = next((c for c in CELLS if c.id == cell_id), None)
        assert cell is not None, cell_id
        assert cell.earned is False, f"{cell_id} must be forbidden"
