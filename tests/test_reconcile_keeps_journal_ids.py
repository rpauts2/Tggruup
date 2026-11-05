"""Confirming a publication must not blank an identifier the journal already has.

`5718327` taught the Dzen matcher to read article ids from *any* non-dry-run
journal row, precisely because an operator confirmation writes a second `posted`
row and the row it answers from is the empty `publishing` one. The write side of
the same operation was left alone: `api._reconcile_crosspost_outcome` copied
`latest.remote_id` and `latest.url` straight out of that empty row, so the
confirmation produced a `posted` row carrying nothing — and since the post
detail (`api._post_payload`) and every other reader take the *newest* `posted`
row, a known id went back to displaying as `external_url: null`.

Both directions are pinned here: a confirmation that would have blanked an id
keeps it, and a confirmation that learns a new id still records that one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tgfarm.api import _journal_identity, _reconcile_crosspost_outcome
from tgfarm.db.models import Channel, CrosspostLog
from tgfarm.db.models import Post as PostRow
from tgfarm.db.session import session_scope

NOW = datetime.now(UTC)
# `publishing` rows younger than this are still in flight and cannot be confirmed.
STALE = NOW - timedelta(minutes=45)


def _seed_journal(rows: list[dict], *, platform: str = "vk") -> str:
    """One post plus the journal rows given, oldest first, on a throwaway channel."""
    suffix = uuid.uuid4().hex[:8]
    channel_id = f"confirm_{suffix}"
    public_id = f"confirm_{suffix}"
    with session_scope() as session:
        session.add(
            Channel(id=channel_id, title="Confirm ids", niche="tech", platform="telegram", enabled=True)
        )
        session.add(
            PostRow(
                public_id=public_id,
                channel_id=channel_id,
                hook="Хук подтверждения.",
                body="Тело поста, опубликованного на площадке.",
                status="publish_unknown",
            )
        )
        for index, row in enumerate(rows):
            session.add(
                CrosspostLog(
                    post_public_id=public_id,
                    platform=platform,
                    status=row["status"],
                    remote_id=row.get("remote_id"),
                    url=row.get("url"),
                    attempt=row.get("attempt", 1),
                    created_at=row.get("created_at", STALE + timedelta(minutes=index)),
                )
            )
    return public_id


def _posted_rows(public_id: str, *, platform: str = "vk") -> list[CrosspostLog]:
    with session_scope() as session:
        return list(
            session.execute(
                select(CrosspostLog)
                .where(CrosspostLog.post_public_id == public_id, CrosspostLog.platform == platform)
                .order_by(CrosspostLog.id)
            ).scalars()
        )


def _newest(post_public_id: str, *, platform: str = "vk") -> CrosspostLog:
    return _posted_rows(post_public_id, platform=platform)[-1]


def test_confirming_from_an_empty_row_keeps_the_id_the_journal_has():
    """The defect: the row answered from carried nothing, an older one carried all."""
    public_id = _seed_journal(
        [
            {"status": "posted", "remote_id": "8712", "url": "https://vk.com/wall-1_8712"},
            {"status": "publishing", "created_at": STALE},
        ]
    )

    assert _reconcile_crosspost_outcome(public_id, "vk", "confirm_posted")["ok"] is True

    confirmed = _newest(public_id)
    assert confirmed.status == "posted"
    assert confirmed.remote_id == "8712"
    assert confirmed.url == "https://vk.com/wall-1_8712"


def test_the_identifier_survives_when_only_one_column_was_ever_learned():
    """Coalesce per column: an id without a url still beats a row with neither."""
    public_id = _seed_journal(
        [
            {"status": "unknown", "remote_id": "9001"},
            {"status": "publishing"},
        ]
    )

    _reconcile_crosspost_outcome(public_id, "vk", "confirm_posted")

    confirmed = _newest(public_id)
    assert confirmed.remote_id == "9001"
    assert confirmed.url is None, "nothing was ever learned about the url; invent none"


def test_the_newest_known_value_wins_for_each_column_separately():
    """A newer url and an older id are both the best we know — take each one."""
    public_id = _seed_journal(
        [
            {"status": "posted", "remote_id": "111", "url": "https://vk.com/old"},
            {"status": "unknown", "url": "https://vk.com/wall-1_111"},
            {"status": "publishing"},
        ]
    )

    _reconcile_crosspost_outcome(public_id, "vk", "confirm_posted")

    confirmed = _newest(public_id)
    assert confirmed.remote_id == "111"
    assert confirmed.url == "https://vk.com/wall-1_111"


def test_a_genuinely_new_id_still_updates():
    """The other direction: a publisher that learned an id late must not be
    overruled by some older row for the same pair."""
    public_id = _seed_journal(
        [
            {"status": "posted", "remote_id": "stale-1", "url": "https://vk.com/stale-1"},
            {"status": "unknown", "remote_id": "fresh-2", "url": "https://vk.com/fresh-2"},
        ]
    )

    _reconcile_crosspost_outcome(public_id, "vk", "confirm_posted")

    confirmed = _newest(public_id)
    assert confirmed.remote_id == "fresh-2"
    assert confirmed.url == "https://vk.com/fresh-2"


def test_the_history_that_held_the_id_is_left_exactly_as_it_was():
    """The journal stays append-only: the fix completes the new row, not the old."""
    public_id = _seed_journal(
        [
            {"status": "posted", "remote_id": "8712", "url": "https://vk.com/wall-1_8712"},
            {"status": "publishing"},
        ]
    )

    _reconcile_crosspost_outcome(public_id, "vk", "confirm_posted")

    before = _posted_rows(public_id)[:2]
    assert [row.status for row in before] == ["posted", "publishing"]
    assert before[0].remote_id == "8712"
    assert before[1].remote_id is None


def test_a_confirmation_with_nothing_to_carry_still_confirms():
    """No id anywhere: the row is written with NULLs, never with a fabricated key."""
    public_id = _seed_journal([{"status": "unknown"}])

    assert _reconcile_crosspost_outcome(public_id, "vk", "confirm_posted")["ok"] is True

    confirmed = _newest(public_id)
    assert confirmed.status == "posted"
    assert confirmed.remote_id is None
    assert confirmed.url is None


def test_a_dry_run_placeholder_is_not_an_identifier():
    """`dry-<post id>` was never sent anywhere; it must not become the id of a
    publication the operator just confirmed. Same exclusion the Dzen matcher
    reads by."""
    public_id = _seed_journal(
        [
            {"status": "dry_run", "remote_id": "dry-vk-42", "url": "https://example.invalid/dry"},
            {"status": "publishing"},
        ]
    )

    _reconcile_crosspost_outcome(public_id, "vk", "confirm_posted")

    confirmed = _newest(public_id)
    assert confirmed.status == "posted"
    assert confirmed.remote_id is None
    assert confirmed.url is None


@pytest.mark.parametrize(
    ("blank", "expected"),
    [(None, None), ("", None), ("   ", None)],
)
def test_blank_placeholders_are_not_treated_as_identifiers(blank, expected):
    """An empty string is as unknown as NULL, so neither is copied forward."""
    public_id = _seed_journal([{"status": "unknown", "remote_id": blank, "url": blank}])

    with session_scope() as session:
        assert _journal_identity(session, public_id, "vk") == (expected, expected)
