"""Format and coverage denominators must say what they can prove, not only what they counted.

The pipeline learns which formats work per platform. For that lesson to mean
anything, a row counted as an outcome has to be an outcome that happened. The
publisher used to stamp `status = "published"` on dry runs that never left the
process — fixed as a write, but the historic rows stand, and the denominators were
never re-examined. Measured on the live database 2026-11-05: all 15 telegram
published rows carry `tg_message_id IS NULL` with the invented `tg_chat_id = -1`,
and 7 of them hold the only nonzero engagement in `post_stats`.

The honesty rule these tests pin is «report alongside, do not exclude silently»:

* the totals keep counting what they always counted (a pinned API contract, and a
  silent redefinition would change the panel with the planner switched off);
* the evidenced subset travels beside them, with `sample_size` naming what the
  evidenced rate actually divides by;
* a row is `unevidenced` only when the platform has a rule and the row fails it —
  a platform with no rule is `unrulled`, which is not proof;
* where nothing is evidenced there is no rate at all: `None`, never `0.0`.

Every exact count here sits inside a group this file invents (its own platform
name or its own `post_format`), because the suite shares one SQLite database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from tgfarm.db.models import Channel, CrosspostLog, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s8_analytics import coverage as coverage_module
from tgfarm.s8_analytics import formats as formats_module
from tgfarm.s8_analytics.coverage import STATUS_MEASURED, STATUS_PARTIAL, measurement_coverage
from tgfarm.s8_analytics.formats import (
    EVIDENCE_MIXED,
    EVIDENCE_NEVER,
    EVIDENCE_OK,
    EVIDENCE_RULES,
    EVIDENCE_STALE,
    EVIDENCE_UNRULLED,
    MIN_SAMPLE,
    format_performance,
    planner_history,
)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
WINDOW_DAYS = 30
STALE_AFTER_DAYS = 7
FRESH = 0.5
DEAD = 30.0

_seeded_channels: list[str] = []


def _unique(key: str) -> str:
    """A name nobody else's fixture writes to (the suite shares one database)."""
    return f"{key}_{uuid4().hex[:8]}".lower()[:32]


def _fmt() -> str:
    """An invented `post_format`: my own group inside a real platform."""
    return f"ev{uuid4().hex[:10]}"


@pytest.fixture(autouse=True)
def _clean_seeded_rows(monkeypatch):
    """Throwaway rows, and a staleness threshold no local `.env` can move."""
    monkeypatch.setattr(formats_module, "_configured_stale_after_days", lambda: STALE_AFTER_DAYS)
    yield
    with session_scope() as session:
        post_ids = list(
            session.scalars(select(Post.id).where(Post.channel_id.in_(_seeded_channels))).all()
        )
        public_ids = list(
            session.scalars(select(Post.public_id).where(Post.channel_id.in_(_seeded_channels))).all()
        )
        if post_ids:
            session.execute(delete(PostStat).where(PostStat.post_id.in_(post_ids)))
        if public_ids:
            session.execute(delete(CrosspostLog).where(CrosspostLog.post_public_id.in_(public_ids)))
            session.execute(delete(Post).where(Post.id.in_(post_ids)))
        session.execute(delete(Channel).where(Channel.id.in_(_seeded_channels)))
    _seeded_channels.clear()


def _channel(platform: str) -> str:
    channel_id = f"den_{uuid4().hex[:8]}"
    _seeded_channels.append(channel_id)
    with session_scope() as session:
        session.add(
            Channel(id=channel_id, title="Знаменатели", niche="business", platform=platform)
        )
    return channel_id


#: `crosspost_logs` shapes a test can ask for: the journal status it writes, and
#: whether it carries a remote id and URL. `posted_without_id` is the live dzen
#: shape (29 of its 37 publications) that `maintenance` reports and deliberately
#: leaves alone — a publication whose identifier the parser never found.
JOURNAL_KINDS: dict[str, tuple[str, bool]] = {
    "posted": ("posted", True),
    "posted_without_id": ("posted", False),
    "dry_run": ("dry_run", False),
    "skipped": ("skipped", False),
}


def _publish(
    channel_id: str,
    fmt: str,
    *,
    engagement: int | None = None,
    tg_message_id: int | None = None,
    journal: str | None = "auto",
    status: str = "published",
    days_ago: float = 1.0,
    collected_days_ago: float = FRESH,
) -> str:
    """One post inside the window, with the evidence the caller asks for.

    `journal` picks a shape from `JOURNAL_KINDS`; `auto` means «whatever this
    platform's rule needs», which is `posted` for dzen and vk and no journal row at
    all for telegram, whose proof is the message id.
    """
    public_id = f"den_{uuid4().hex[:10]}"
    platform = str(_platform_of(channel_id) or "").strip().lower()
    with session_scope() as session:
        post = Post(
            public_id=public_id,
            channel_id=channel_id,
            status=status,
            post_format=fmt,
            published_at=(NOW - timedelta(days=days_ago)) if status == "published" else None,
            tg_message_id=tg_message_id,
            hook="Заголовок",
            body="Текст",
        )
        session.add(post)
        session.flush()
        if engagement is not None:
            session.add(
                PostStat(
                    post_id=post.id,
                    reactions=engagement,
                    comments=0,
                    forwards=0,
                    collected_at=NOW - timedelta(days=collected_days_ago),
                )
            )
        if journal == "auto":
            kind = "posted" if platform in ("dzen", "vk") else None
        else:
            kind = journal
        if kind:
            row_status, has_ref = JOURNAL_KINDS[kind]
            session.add(
                CrosspostLog(
                    post_public_id=public_id,
                    platform=platform,
                    status=row_status,
                    remote_id=f"remote-{uuid4().hex[:6]}" if has_ref else None,
                    url=f"https://example.invalid/{uuid4().hex[:6]}" if has_ref else None,
                    created_at=NOW,
                )
            )
    return public_id


def _platform_of(channel_id: str) -> str:
    with session_scope() as session:
        return str(
            session.execute(select(Channel.platform).where(Channel.id == channel_id)).scalar_one()
        )


def _report(platform: str | None = None) -> dict:
    return format_performance(WINDOW_DAYS, platform=platform, now=NOW)


def _publication(report: dict, fmt: str) -> dict:
    return report["measurement"][fmt]["publication"]


# ---------------------------------------------------------------- live evidence
def test_phantom_publications_stay_in_the_total_and_leave_the_evidenced_sample():
    """The old denominator, the new split, and the identity between them.

    Three telegram posts with no message id and a snapshot each: this is the live
    database's shape — every telegram publication in it, and the only engagement
    it holds. They must keep inflating `published`/`measured` (that is what the
    panel and `MIN_SAMPLE` have always read) while the evidenced subset reports
    them as nothing.
    """
    platform = "telegram"
    channel = _channel(platform)
    fmt = _fmt()
    for engagement in (10, 20, 30):
        _publish(channel, fmt, engagement=engagement, tg_message_id=None)

    report = _report(platform)
    publication = _publication(report, fmt)

    assert report["formats"][fmt]["published"] == 3
    assert report["formats"][fmt]["measured"] == 3
    assert report["formats"][fmt]["n"] == 3
    # The legacy read is unchanged, so nothing here quietly prettier-ed a number.
    assert report["formats"][fmt]["avg_engagement"] == pytest.approx(20.0)
    assert report["formats"][fmt]["reliable"] is (3 >= MIN_SAMPLE)

    assert publication["published"] == 3
    assert publication["published_evidenced"] == 0
    assert publication["published_unevidenced"] == 3
    assert publication["published_unrulled"] == 0
    assert publication["measured_unevidenced"] == 3
    assert publication["sample_size"] == 0
    # The freshness dimension's live count, minus exactly what this dimension
    # disproves: without this key a reader could not get from `fresh: 3` to
    # `fresh_sample_size: 0` without guessing, and the planner veto below reads
    # this number directly.
    assert report["measurement"][fmt]["fresh"] == 3
    assert publication["fresh_unevidenced"] == 3
    assert publication["fresh_sample_size"] == 0
    assert publication["evidence_status"] == EVIDENCE_NEVER
    # reported alongside, never instead: the three parts are exactly the whole
    assert (
        publication["published_evidenced"]
        + publication["published_unevidenced"]
        + publication["published_unrulled"]
    ) == publication["published"]


def test_a_window_of_only_unevidenced_rows_yields_no_rate_rather_than_a_zero_rate():
    """`None` is the honest answer, and `0.0` is the one reading this must kill.

    A rate of zero says «this format earned nothing», which is a verdict a
    planner can rank. Three phantom publications that earned nothing measurable
    say «nothing here can be pointed at», which is not a rankable number.
    """
    channel = _channel("telegram")
    fmt = _fmt()
    for engagement in (5, 6, 7):
        _publish(channel, fmt, engagement=engagement, tg_message_id=None)

    report = _report("telegram")
    publication = _publication(report, fmt)

    assert publication["sample_size"] == 0
    assert publication["avg_engagement"] is None
    assert publication["fresh_avg_engagement"] is None
    assert publication["reliable"] is False
    assert publication["evidence_status"] == EVIDENCE_NEVER
    assert report["publication"]["avg_engagement"] is None
    assert report["publication"]["evidence_status"] == EVIDENCE_NEVER


def test_evidenced_average_divides_by_the_evidenced_sample_only():
    """Proof changes the mean without moving the total.

    Two publications the journal can point at, three that nothing can: the
    evidenced average is over the two, the reported total still counts five, and
    `sample_size` states plainly which of them the rate divided by.
    """
    channel = _channel("dzen")
    fmt = _fmt()
    for engagement in (40, 60):
        _publish(channel, fmt, engagement=engagement, journal="posted")
    for engagement in (100, 100, 100):
        _publish(channel, fmt, engagement=engagement, journal="dry_run")

    report = _report("dzen")
    publication = _publication(report, fmt)

    assert publication["published"] == 5
    assert publication["published_evidenced"] == 2
    assert publication["published_unevidenced"] == 3
    assert publication["measured"] == 5
    assert publication["measured_evidenced"] == 2
    assert publication["measured_unevidenced"] == 3
    assert publication["sample_size"] == 2
    assert publication["avg_engagement"] == pytest.approx(50.0)
    # ... while the untouched whole-window average over all five stands at 80.0,
    # because the three phantoms earned a hundred each and nothing can take them
    # out of the number the panel has always shown.
    assert report["formats"][fmt]["avg_engagement"] == pytest.approx(80.0)
    assert publication["evidence_status"] == EVIDENCE_MIXED


def test_posted_journal_without_an_identifier_is_still_evidence():
    """A missing id is not a missing publication — 29 of the live 37 dzen rows.

    `maintenance` reports these as `posted_without_id` and deliberately leaves
    them alone; treating them as unevidenced would un-publish real work and make
    the two readings of the same rows disagree.
    """
    channel = _channel("dzen")
    fmt = _fmt()
    _publish(channel, fmt, engagement=11, journal="posted_without_id")

    publication = _publication(_report("dzen"), fmt)

    assert publication["published_evidenced"] == 1
    assert publication["published_unevidenced"] == 0
    assert publication["sample_size"] == 1
    assert publication["avg_engagement"] == pytest.approx(11.0)
    assert publication["evidence_status"] == EVIDENCE_OK


def test_unrulled_platform_keeps_the_legacy_numbers_and_names_the_missing_rule():
    """No rule for this platform is not the same thing as proof.

    A row nobody can disprove stays in the sample — demoting it would be an
    invention in the other direction — but the report says the count rests on the
    absence of a rule rather than on evidence.
    """
    platform = _unique("pikabu")
    channel = _channel(platform)
    fmt = _fmt()
    for engagement in (7, 8, 9):
        _publish(channel, fmt, engagement=engagement)

    report = _report(platform)
    publication = _publication(report, fmt)

    assert report["formats"][fmt]["measured"] == 3
    assert report["formats"][fmt]["avg_engagement"] == pytest.approx(8.0)
    assert publication["published_unrulled"] == 3
    assert publication["published_unevidenced"] == 0
    assert publication["measured_unrulled"] == 3
    assert publication["sample_size"] == 3
    assert publication["avg_engagement"] == pytest.approx(8.0)
    assert publication["evidence_status"] == EVIDENCE_UNRULLED
    assert platform not in EVIDENCE_RULES


def test_publish_unknown_rows_are_named_and_enter_no_denominator():
    """The 51 live rows in this state may be neither counted nor invisible.

    Their send may or may not have happened, so they are in no denominator — and
    the report names them, because a reader who is not told cannot tell «nothing
    uncertain» from «51 rows nobody dares count».
    """
    channel = _channel("dzen")
    fmt = _fmt()
    _publish(channel, fmt, engagement=5, status="publish_unknown")
    _publish(channel, fmt, engagement=5, status="publish_unknown")
    _publish(channel, fmt, engagement=5, journal="posted")

    report = _report("dzen")

    assert report["formats"][fmt]["published"] == 1
    assert report["formats"][fmt]["measured"] == 1
    assert report["publication"]["publish_unknown"] >= 2
    assert report["publication"]["rules"] == EVIDENCE_RULES


def test_dead_snapshots_do_not_reenter_the_sample_through_the_back_door():
    """The two vetoes are independent and both must hold at once.

    A phantom publication with a week-old snapshot fails the freshness gate
    already; it must not arrive as live evidence either.
    """
    channel = _channel("telegram")
    fmt = _fmt()
    for engagement in (50, 60, 70):
        _publish(channel, fmt, engagement=engagement, tg_message_id=None, collected_days_ago=DEAD)

    report = _report("telegram")
    publication = _publication(report, fmt)

    assert report["measurement"][fmt]["fresh"] == 0
    assert publication["fresh_sample_size"] == 0
    assert publication["fresh_avg_engagement"] is None
    # Two independent verdicts over the same three rows: the freshness dimension
    # still says «stale» exactly as it did before this split existed, and the
    # publication dimension says «not one row proven» on top of it. Neither word
    # is allowed to cancel the other, which is why neither count is shared.
    assert report["evidence_status"] == EVIDENCE_STALE
    assert publication["evidence_status"] == EVIDENCE_NEVER
    assert publication["published_unevidenced"] == 3


# ------------------------------------------------------------- the planner feed
def test_planner_history_drops_disproved_publications_and_names_the_drop():
    """The gated feed must not hand the planner a phantom sample.

    Same shape as the staleness veto it sits beside: the live count is what the
    planner acts on, and what was dropped is named instead of vanishing.
    """
    channel = _channel("telegram")
    fmt = _fmt()
    for engagement in (90, 90, 90):
        _publish(channel, fmt, engagement=engagement, tg_message_id=None)

    row = planner_history("telegram", days=WINDOW_DAYS, now=NOW)[fmt]

    assert row["fresh_measured"] == 3  # the old live count, still reported
    assert row["measured"] == 0  # ... and none of it is evidence
    assert row["unevidenced_measured"] == 3
    assert row["unevidenced_published"] == 3
    assert row["sample_size"] == 0
    assert row["publication_evidence"] == EVIDENCE_NEVER
    assert row["avg_engagement"] is None
    assert row["reliable"] is False


def test_planner_history_is_unchanged_when_every_publication_is_evidenced():
    """A veto, not a second strategy.

    With proof behind every row the gated feed is what it has always been, down to
    the average and the reliability flag; only the new keys describe the evidence.
    """
    channel = _channel("vk")
    fmt = _fmt()
    for engagement in (12, 24, 36):
        _publish(channel, fmt, engagement=engagement, journal="posted")

    report = _report("vk")
    row = planner_history("vk", days=WINDOW_DAYS, now=NOW)[fmt]
    evidence = report["measurement"][fmt]

    assert row["measured"] == evidence["fresh"] == 3
    assert row["avg_engagement"] == pytest.approx(evidence["fresh_avg_engagement"])
    assert row["avg_engagement"] == pytest.approx(24.0)
    assert row["reliable"] is True
    assert row["unevidenced_measured"] == 0
    assert row["unrulled_published"] == 0
    assert row["publication_evidence"] == EVIDENCE_OK


def test_planner_history_keeps_an_unrulled_platform_exactly_as_it_was():
    """Regression guard for the platforms this repo has no rule for yet."""
    platform = _unique("max")
    channel = _channel(platform)
    fmt = _fmt()
    for engagement in (15, 15, 15):
        _publish(channel, fmt, engagement=engagement)

    row = planner_history(platform, days=WINDOW_DAYS, now=NOW)[fmt]

    assert row["measured"] == 3
    assert row["unevidenced_measured"] == 0
    assert row["unrulled_published"] == 3
    assert row["avg_engagement"] == pytest.approx(15.0)
    assert row["publication_evidence"] == EVIDENCE_UNRULLED


# ------------------------------------------------------------------- coverage
def _coverage_row(platform: str) -> dict:
    row = measurement_coverage(now=NOW)["platforms"].get(platform)
    return row or {}


def _delta(before: dict, after: dict) -> dict[str, int]:
    """The integer fields this test moved, so prose and residue cannot fake it.

    The suite shares one database, so a platform's absolute count is somebody
    else's business too; what this file seeds is exactly what the difference is.
    """
    return {
        key: value - before.get(key, 0)
        for key, value in after.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def test_coverage_splits_the_claimed_publications_from_the_provable_ones():
    """`published` is a claim; the audit now reports what backs it.

    Measured on deltas inside rows this test owns, so a shared database cannot
    change the answer: two telegram posts with a message id and snapshots, three
    without anything to point at.
    """
    before = _coverage_row("telegram")
    channel = _channel("telegram")
    fmt = _fmt()
    for engagement in (3, 5):
        _publish(channel, fmt, engagement=engagement, tg_message_id=1000 + engagement)
    for engagement in (7, 9, 11):
        _publish(channel, fmt, engagement=engagement, tg_message_id=None)

    after = _coverage_row("telegram")
    delta = _delta(before, after)

    assert delta["published"] == 5
    assert delta["published_evidenced"] == 2
    assert delta["published_unevidenced"] == 3
    assert delta["measured_posts"] == 5
    assert delta["measured_evidenced"] == 2
    assert delta["published_unrulled"] == 0
    # the identity holds for the row itself, not only for the increment
    assert (
        after["published_evidenced"]
        + after["published_unevidenced"]
        + after["published_unrulled"]
    ) == after["published"]


def test_coverage_does_not_call_a_platform_measured_on_the_strength_of_drafts():
    """The numerator and the denominator have to be about the same posts.

    A snapshot can hang off a post nobody published; `measured_posts` counted it,
    so a platform could be declared fully measured while not one publication had
    been read.
    """
    before = _coverage_row("dzen")
    channel = _channel("dzen")
    fmt = _fmt()
    _publish(channel, fmt, engagement=None, journal="posted")
    _publish(channel, fmt, engagement=None, journal="posted", status="draft")
    _publish(channel, f"{fmt}d", engagement=8, journal="posted", status="failed")

    after = _coverage_row("dzen")
    delta = _delta(before, after)

    assert delta["measured_posts"] == 1
    assert delta["measured_published"] == 0
    assert delta["published"] == 1
    assert after["status"] != STATUS_MEASURED


def test_coverage_ratio_is_none_when_there_is_nothing_to_divide_by(monkeypatch):
    """`0.0` answered «no publication exists» with «nothing was measured».

    A farm that has published nothing and a farm nobody reads are two different
    facts, and the number must not collapse them.
    """
    platform = _unique("empty")
    _channel(platform)
    monkeypatch.setattr(coverage_module, "_platform_keys", lambda: {platform})

    report = measurement_coverage(now=NOW)

    assert report["totals"]["published"] == 0
    assert report["coverage"] is None
    assert report["coverage_evidenced"] is None
    assert report["platforms"][platform]["published_evidenced"] == 0
    # ... and the per-platform fields still tell the two states apart.
    assert report["platforms"][platform]["status"] != STATUS_MEASURED


def test_coverage_unevidenced_publications_are_said_in_the_reason():
    """The prose and the numbers agree: a partial platform names its phantoms.

    Two unproven publications with a snapshot, one proven with a snapshot and two
    publications with no snapshot at all — the last pair is what keeps the platform
    `partial` rather than `measured`, because a fully measured platform has no gap
    to explain and so no reason string to carry the claim.
    """
    before = _coverage_row("telegram")
    channel = _channel("telegram")
    fmt = _fmt()
    for engagement in (2, 3):
        _publish(channel, fmt, engagement=engagement, tg_message_id=None)
    _publish(channel, fmt, engagement=4, tg_message_id=77)
    _publish(channel, fmt, engagement=None, tg_message_id=78)
    _publish(channel, fmt, engagement=None, tg_message_id=None)

    row = _coverage_row("telegram")

    assert row["published_unevidenced"] - before.get("published_unevidenced", 0) == 3
    assert row["published_evidenced"] - before.get("published_evidenced", 0) == 2
    assert row["status"] == STATUS_PARTIAL
    assert "без доказательства отправки" in (row["reason"] or "")
    assert "posts.tg_message_id" in (row["reason"] or "")


def test_coverage_table_reports_attribution_as_posts_not_journal_rows():
    """399 journal rows describe 399 publications nobody ever measured.

    `attributable` keeps its old meaning for the panel that reads it; the post-level
    count next to it is the claim a reader can put beside `measured_posts`.
    """
    before = {
        row["platform"]: row for row in coverage_module.coverage_table(now=NOW)
    }.get("vk", {})
    channel = _channel("vk")
    fmt = _fmt()
    _publish(channel, fmt, engagement=None, journal="posted")
    _publish(channel, fmt, engagement=None, journal="dry_run")

    vk = next(row for row in coverage_module.coverage_table(now=NOW) if row["platform"] == "vk")

    assert vk["published_evidenced"] - before.get("published_evidenced", 0) == 1
    assert vk["published_unevidenced"] - before.get("published_unevidenced", 0) == 1
    # attribution moved from journal rows to posts: the dry run is not attributable
    assert vk["attributed_posts"] - before.get("attributed_posts", 0) == 1
    assert vk["measured_evidenced"] - before.get("measured_evidenced", 0) == 0
    assert {"published_evidenced", "measured_evidenced", "attributed_posts"} <= set(vk)
