"""A legal window narrower than the slot step is still bookable.

`_first_free_and_legal_slot` walks a fixed 90-minute grid. From 08:30 the cells
are 08:30, 10:00 and 11:30, so a channel whose only legal hour is 09:xx was told
`no_legal_hours` while 09:00 was free - and every live schedulexecution of that
plan was refused. The walk now probes the top of the hours each step crosses,
without changing the 24/7 answer (there every cell is legal, so the extra probe
is never reached).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from tgfarm.db.models import Channel
from tgfarm.editorial_plan import _next_free_slot


def _channel() -> Channel:
    return Channel(
        id="narrow-window-utc",
        platform="telegram",
        title="narrow window",
        niche="",
        enabled=True,
        timezone="UTC",
    )


def _config(window: list[int]):
    return SimpleNamespace(
        schedule=SimpleNamespace(cadence={"mode": "interval", "active_hours": window})
    )


def test_a_supplemental_candidate_keeps_the_established_half_hour_phase():
    """The hour a skipped grid cell crossed is offered at :30, not at :00.

    The builder starts 30 minutes after the clock: 08:00 -> 08:30, and the
    90-minute grid then jumps to 10:00, skipping the 09:xx window entirely.
    The supplemental candidate fills that hour at the same 30-minute phase the
    channel already books with, so a correction anchored to a refused minute
    cannot jump backwards to a new hour boundary.
    """
    now = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)

    slot = _next_free_slot(_channel(), {}, now, 1, config=_config([9, 10]))

    assert slot == datetime(2026, 11, 4, 9, 30, tzinfo=UTC)


def test_an_occupied_legal_hour_moves_to_the_next_legal_hour():
    now = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)
    channel = _channel()
    taken = {channel.id: {datetime(2026, 11, 4, 9, 0, tzinfo=UTC)}}

    slot = _next_free_slot(channel, taken, now, 1, config=_config([9, 12]))

    assert slot == datetime(2026, 11, 4, 10, 0, tzinfo=UTC)


def test_a_legal_hour_on_the_next_day_is_still_found():
    now = datetime(2026, 11, 4, 8, 30, tzinfo=UTC)

    slot = _next_free_slot(_channel(), {}, now, 1, config=_config([3, 4]))

    assert slot == datetime(2026, 11, 5, 3, 0, tzinfo=UTC)


def test_a_247_channel_keeps_the_plain_grid_answer():
    now = datetime(2026, 11, 4, 21, 0, tzinfo=UTC)

    slot = _next_free_slot(_channel(), {}, now, 1, config=_config(None))

    assert slot == datetime(2026, 11, 4, 21, 30, tzinfo=UTC)
