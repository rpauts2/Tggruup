"""W24-M: attribution of collected stats to publications must be honest.

The suite is offline (conftest blocks the network); everything here runs
against the throwaway session database. The three rules under test:

1. a publication without the external id the collector needs is *unknowable*,
   never silently folded into a measured bucket or reported as zero,
2. a stats row that matches no publication is reported as an orphan and does
   not inflate anyone's coverage,
3. unmeasured is None, not 0 — an empty denominator is a different claim from
   "nothing was measured here".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select

from tgfarm.db.models import Channel, CrosspostLog, Post, PostStat
from tgfarm.s8_analytics.attribution import (
    Measurement,
    PublicationRef,
    attribution_report,
    collect_refs_and_measurements,
    summarize_attribution,
)


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------- pure layer
def test_publication_without_external_id_is_unknowable_not_zero():
    refs = [
        PublicationRef(platform="telegram", key="tg:1", has_external_id=False),
        PublicationRef(platform="telegram", key="tg:2", has_external_id=True),
    ]
    report = summarize_attribution(refs, [])
    tg = report["platforms"]["telegram"]
    assert tg["publications"] == 2
    assert tg["unknowable"] == 1
    assert tg["with_external_id"] == 1
    # N > 0 with nothing matched: coverage is a true 0.0, while the timestamp
    # of the last measurement stays None — never a fabricated epoch or zero.
    assert tg["with_stats"] == 0
    assert tg["coverage"] == 0.0
    assert tg["share_unknowable"] == 0.5
    assert tg["last_measurement_at"] is None


def test_empty_denominator_is_none_not_zero():
    refs = [PublicationRef(platform="dzen", key="cp:1", has_external_id=True)]
    report = summarize_attribution(refs, [Measurement("cp:1", _now())])
    assert report["platforms"]["dzen"]["coverage"] == 1.0

    # a platform the caller asked about that has no publications: unknown,
    # which is a different claim from "0 % of them were measured"
    report = summarize_attribution(refs, [Measurement("cp:1", _now())], platforms=("telegram",))
    empty = report["platforms"]["telegram"]
    assert empty["publications"] == 0
    assert empty["coverage"] is None
    assert empty["share_unknowable"] is None
    assert empty["last_measurement_at"] is None

    nothing = summarize_attribution([], [])
    assert nothing["platforms"] == {}
    assert nothing["orphan_measurements"] == 0


def test_stats_row_matching_nothing_is_an_orphan_and_does_not_inflate():
    refs = [PublicationRef(platform="telegram", key="tg:1", has_external_id=True)]
    measurements = [
        Measurement("tg:1", _now()),
        Measurement("tg:404", _now()),  # post deleted / never published
        Measurement("cp:77", _now()),
    ]
    report = summarize_attribution(refs, measurements)
    assert report["orphan_measurements"] == 2
    tg = report["platforms"]["telegram"]
    assert tg["publications"] == 1
    assert tg["with_stats"] == 1  # orphan rows did not touch this bucket
    assert tg["coverage"] == 1.0


def test_partial_measurement_keeps_absent_posts_absent_not_zero():
    refs = [
        PublicationRef(platform="telegram", key=f"tg:{i}", has_external_id=True)
        for i in (1, 2, 3, 4)
    ]
    measurements = [Measurement("tg:1", _now()), Measurement("tg:2", _now())]
    tg = summarize_attribution(refs, measurements)["platforms"]["telegram"]
    assert tg["coverage"] == 0.5
    assert tg["unknowable"] == 0  # attributable but unmeasured ≠ unknowable


# ----------------------------------------------------------- DB integration
def _seed_tg_post(db, channel_id: str, *, tg_message_id, with_stat: bool, status="published"):
    db.add(Channel(id=channel_id, platform="telegram", title=f"probe {channel_id}"))
    post = Post(
        public_id=f"p-{channel_id}",
        channel_id=channel_id,
        status=status,
        published_at=_now() if status == "published" else None,
        tg_message_id=tg_message_id,
    )
    db.add(post)
    db.flush()
    if with_stat:
        db.add(PostStat(post_id=post.id))
    db.commit()
    return post


def _purge(db, channel_id: str) -> None:
    ids = select(Post.id).where(Post.channel_id.like(f"{channel_id}%"))
    db.execute(delete(PostStat).where(PostStat.post_id.in_(ids)))
    db.execute(delete(Post).where(Post.channel_id.like(f"{channel_id}%")))
    db.execute(delete(Channel).where(Channel.id.like(f"{channel_id}%")))
    db.commit()


def test_attribution_report_sees_null_tg_message_id_as_unknowable(db):
    cid = f"w24m-tg-{_uid()}"
    _seed_tg_post(db, f"{cid}-a", tg_message_id=None, with_stat=False)
    _seed_tg_post(db, f"{cid}-b", tg_message_id=None, with_stat=True)
    _seed_tg_post(db, f"{cid}-c", tg_message_id=4242, with_stat=True)
    try:
        report = attribution_report(
            days=1, channel_ids=[f"{cid}-a", f"{cid}-b", f"{cid}-c"]
        )
        tg = report["platforms"]["telegram"]
        assert tg["publications"] == 3
        assert tg["with_external_id"] == 1
        # the measured-but-idless post is still unattributable going forward
        assert tg["unknowable"] == 2
        assert tg["with_stats"] == 2
        assert tg["coverage"] == round(2 / 3, 4)
        assert tg["last_measurement_at"] is not None
    finally:
        _purge(db, cid)


def test_stat_row_matching_no_publication_surfaces_as_orphan(db):
    cid = f"w24m-orphan-{_uid()}"
    good = _seed_tg_post(db, f"{cid}-good", tg_message_id=7, with_stat=True)
    ghost = _seed_tg_post(db, f"{cid}-ghost", tg_message_id=None, with_stat=True, status="draft")
    try:
        refs, measurements = collect_refs_and_measurements(days=1)
        tg_keys = {f"tg:{good.id}", f"tg:{ghost.id}"}
        scoped_refs = [r for r in refs if r.key in tg_keys]
        all_ref_keys = {r.key for r in refs}
        scoped_meas = [m for m in measurements if m.key in tg_keys or m.key not in all_ref_keys]
        report = summarize_attribution(scoped_refs, scoped_meas)
        assert report["orphan_measurements"] >= 1  # the draft ghost's stat row
        tg = report["platforms"]["telegram"]
        assert tg["publications"] == 1  # a draft is not a publication
        assert tg["with_stats"] == 1  # the ghost's stat did not attach here
        assert tg["coverage"] == 1.0
    finally:
        _purge(db, cid)


def test_crosspost_log_without_remote_id_is_unknowable(db):
    platform = f"w24mx{_uid()}"[:32]
    db.add(
        CrosspostLog(post_public_id=f"x-{_uid()}", platform=platform, status="posted", remote_id=None)
    )
    db.add(
        CrosspostLog(post_public_id=f"x-{_uid()}", platform=platform, status="posted", remote_id="rid-1")
    )
    db.commit()
    try:
        bucket = attribution_report(days=1)["platforms"][platform]
        assert bucket["publications"] == 2
        assert bucket["with_external_id"] == 1
        assert bucket["unknowable"] == 1
        # this probe platform has no collector at all: with_stats is a true
        # zero and coverage a true 0.0 — not None, the denominator is not empty
        assert bucket["with_stats"] == 0
        assert bucket["coverage"] == 0.0
        assert bucket["last_measurement_at"] is None
    finally:
        db.execute(delete(CrosspostLog).where(CrosspostLog.platform == platform))
        db.commit()
