"""A format lesson may only be called reliable on publications that can be proven.

The farm already refuses an unmeasured post as proof of a format working, and
`19d1b09` taught `s8_analytics/formats.py` to ask a third question of the same
rows — *did this publication happen at all*: `EVIDENCE_RULES` (:90), the buckets
(:134), the per-format ``publication`` block (:501) and the report-level one
(:522-545). `planner_history` (:556, consumed at `s4_generation:1396`/`:1479`)
gates on it and emits `publication_evidence` (:592/:613). The durable-lesson path
did not: `s12_learning/__init__.py:240 learn_format_lessons` read
`format_performance(...)` (:295), took `reliable = bool(item.get("reliable"))`
(:321) — which is `measured >= MIN_SAMPLE` over *published* rows — and
`format_lessons(..., reliable_only=…)` (:427/:475) was the only gate, so a lesson
could be declared reliable on a sample that includes publications nothing can
point at. Two standards for «may this steer work» existed at once. These tests pin
the lesson path to the one that was already correct, rather than to a third.

The policy, as chosen and as measured on the live base 2026-11-05 (vk 404
published / 404 provable, dzen 57 / 35, telegram 15 / 0 with 7 of its 8 `news`
posts measured):

* the reliability sample is the evidenced subset, so 50 phantom publications are
  worth less than 3 provable ones — and on telegram not one lesson can become
  reliable until the publisher journals something, which is the correct outcome,
  not a regression to soften;
* nothing is deleted or quietly re-measured: `measured`, `published`,
  `sample_size` and `avg_engagement` keep counting what they always counted, and
  «50 published, 0 provable» travels beside them in the row, in the note and in
  the state (`not_measured` / `unevidenced` / `below_sample` / `reliable`);
* the same veto binds the hand-off: what `durable_format_preference` gives the
  planner is the provable count and the provable average, exactly the two numbers
  `planner_history` would have given for the same rows;
* a lesson written before this marker existed declares nothing and keeps its old
  rights, because the next learning cycle rewrites it (the same courtesy P70
  shipped for the freshness marker).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from tgfarm.config import Persona
from tgfarm.core.schemas import Cluster, ItemScored
from tgfarm.db.models import Channel, CrosspostLog, LearningMemory, Post, PostStat
from tgfarm.db.session import session_scope
from tgfarm.s4_generation import durable_format_preference, format_plan_for
from tgfarm.s8_analytics import formats as formats_module
from tgfarm.s8_analytics.formats import (
    EVIDENCE_MIXED,
    EVIDENCE_NEVER,
    EVIDENCE_OK,
    EVIDENCE_RULES,
    EVIDENCE_UNRULLED,
    MIN_SAMPLE,
    RULE_JOURNAL,
    RULE_MESSAGE_ID,
    format_performance,
    planner_history,
)
from tgfarm.s10_format import plan_for_cluster
from tgfarm.s12_learning import (
    best_format_lesson,
    format_lessons,
    format_state,
    learn_format_lessons,
    lessons,
)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
WINDOW_DAYS = 60
STALE_AFTER_DAYS = 7
FRESH = 0.5

#: The report row the panel and `/api/metrics/formats` have always published.
LEGACY_ROW_KEYS = {"format", "published", "measured", "avg_engagement", "reliable", "n"}
#: The lesson fields existing readers name. `reliable` and `state` are here — what
#: changed is *how they are computed*, not what they are called.
LEGACY_LESSON_KEYS = {
    "format", "platform", "niche", "measured", "published", "avg_engagement",
    "baseline_engagement", "relative_quality", "quality", "min_sample", "reliable",
    "state", "window_days", "fresh_measured", "stale_measured", "fresh_avg_engagement",
    "evidence_status", "evidence_stale_after_days",
}

_seeded_channels: list[str] = []
_seeded_keys: list[str] = []


def _unique(key: str) -> str:
    """A platform/format name nobody else's fixture writes to (one shared DB)."""
    return f"{key}_{uuid4().hex[:8]}".lower()[:32]


@pytest.fixture(autouse=True)
def _clean_seeded_rows(monkeypatch):
    monkeypatch.setattr(formats_module, "_configured_stale_after_days", lambda: STALE_AFTER_DAYS)
    yield
    with session_scope() as session:
        session.execute(delete(LearningMemory).where(LearningMemory.key.in_(_seeded_keys)))
        session.execute(
            delete(LearningMemory).where(LearningMemory.channel_id.in_(_seeded_channels))
        )
        post_ids = list(
            session.scalars(select(Post.id).where(Post.channel_id.in_(_seeded_channels))).all()
        )
        public_ids = list(
            session.scalars(
                select(Post.public_id).where(Post.channel_id.in_(_seeded_channels))
            ).all()
        )
        if post_ids:
            session.execute(delete(PostStat).where(PostStat.post_id.in_(post_ids)))
        if public_ids:
            session.execute(delete(CrosspostLog).where(CrosspostLog.post_public_id.in_(public_ids)))
        session.execute(delete(Post).where(Post.id.in_(post_ids)))
        session.execute(delete(Channel).where(Channel.id.in_(_seeded_channels)))
    _seeded_channels.clear()
    _seeded_keys.clear()


def _channel(platform: str) -> str:
    channel_id = f"egl_{uuid4().hex[:8]}"
    _seeded_channels.append(channel_id)
    with session_scope() as session:
        session.add(Channel(id=channel_id, title="Доказанные уроки", niche="business", platform=platform))
    return channel_id


def _publish(
    channel_id: str,
    fmt: str,
    *,
    engagement: int | None = 10,
    tg_message_id: int | None = None,
    journal: str | None = None,
    collected_days_ago: float = FRESH,
) -> None:
    """One published, measured post and the proof (or the lack of it) behind it.

    `tg_message_id` is telegram's proof, `journal` the dzen/vk one
    (`posted` carries a remote id, `posted_without_id` is the live dzen shape —
    27 of its 35 journalled rows — and `dry_run` proves nothing).
    """
    public_id = f"egl_{uuid4().hex[:10]}"
    platform = _platform_of(channel_id)
    with session_scope() as session:
        post = Post(
            public_id=public_id,
            channel_id=channel_id,
            status="published",
            post_format=fmt,
            published_at=NOW - timedelta(days=1),
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
        if journal:
            status, has_ref = {"posted": ("posted", True), "posted_without_id": ("posted", False),
                               "dry_run": ("dry_run", False)}[journal]
            session.add(
                CrosspostLog(
                    post_public_id=public_id,
                    platform=platform,
                    status=status,
                    remote_id=f"remote-{uuid4().hex[:6]}" if has_ref else None,
                    url=f"https://example.invalid/{uuid4().hex[:6]}" if has_ref else None,
                    created_at=NOW,
                )
            )


def _platform_of(channel_id: str) -> str:
    with session_scope() as session:
        return str(
            session.execute(select(Channel.platform).where(Channel.id == channel_id)).scalar_one()
        )


def _track_key(platform: str, fmt: str) -> str:
    key = f"{platform}/{fmt}"
    _seeded_keys.append(key)
    return key


def _lesson_row(channel_id: str, platform: str, fmt: str) -> dict:
    key = _track_key(platform, fmt)
    row = next((item for item in lessons(channel_id, kind="format") if item["key"] == key), None)
    assert row is not None, f"no lesson stored for {key}"
    return row


def _seed(platform: str, fmt: str, *, evidenced: int, unevidenced: int, engagement: int) -> str:
    """`evidenced` provable and `unevidenced` phantom measured publications, one format."""
    channel = _channel(platform)
    for _ in range(evidenced):
        _publish(
            channel, fmt, engagement=engagement,
            tg_message_id=1000 + engagement if platform == "telegram" else None,
            journal="posted" if platform in ("dzen", "vk") else None,
        )
    for _ in range(unevidenced):
        _publish(
            channel, fmt, engagement=engagement,
            tg_message_id=None if platform == "telegram" else None,
            journal="dry_run" if platform in ("dzen", "vk") else None,
        )
    return channel


# ------------------------------------------------------------------ the write gate
def test_phantoms_cannot_top_up_the_sample_that_makes_a_lesson_reliable():
    """Three provable posts decide; the four phantoms beside them are reported, not used.

    The legacy `reliable` was `measured >= MIN_SAMPLE`, i.e. 7 here — and 7 would
    have been reached by phantom rows alone. The evidenced subset is 3, so the
    verdict is `reliable` for the *right* reason, with the disproved half named.
    """
    fmt = _unique("news")
    channel = _seed("telegram", fmt, evidenced=3, unevidenced=4, engagement=10)

    report = learn_format_lessons(channel, days=WINDOW_DAYS, now=NOW)
    value = dict(_lesson_row(channel, "telegram", fmt)["value"] or {})

    assert report["formats"][fmt]["state"] == "reliable"
    assert report["formats"][fmt]["reliable"] is True
    assert report["unevidenced"] == []
    # the legacy counts are what they always were: seven measured of seven published
    assert value["measured"] == 7 and value["published"] == 7
    assert _lesson_row(channel, "telegram", fmt)["sample_size"] == 7
    # and the provable subset travels beside them, never instead of them
    assert value["measured_evidenced"] == 3
    assert value["measured_unevidenced"] == 4
    assert value["published_evidenced"] == 3 and value["published_unevidenced"] == 4
    assert value["publication_evidence"] == EVIDENCE_MIXED
    assert value["publication_rule"] == RULE_MESSAGE_ID
    assert value["evidenced_sample_size"] == 3
    assert value["reliable"] is True and value["state"] == "reliable"


def test_an_evidenced_sample_below_the_threshold_is_an_observation_not_a_conclusion():
    """2 provable + 5 phantom: the sample is 2, however many posts the window holds."""
    fmt = _unique("stat")
    channel = _seed("telegram", fmt, evidenced=2, unevidenced=5, engagement=20)

    learn_format_lessons(channel, days=WINDOW_DAYS, now=NOW)
    value = dict(_lesson_row(channel, "telegram", fmt)["value"] or {})

    assert value["measured"] == 7 and value["published"] == 7
    assert value["measured_evidenced"] == 2
    assert value["state"] == "below_sample" and value["reliable"] is False
    assert value["min_sample"] == MIN_SAMPLE
    # the note names the threshold *and* the phantoms, so the row reads honestly
    note = _lesson_row(channel, "telegram", fmt)["note"] or ""
    assert "порог" in note and "без доказательства" in note
    # and nothing downstream may promote it
    assert format_state(channel, fmt, platform="telegram")["reliable"] is False
    assert best_format_lesson(channel, platform="telegram") is None


def test_fifty_publications_with_no_evidence_are_still_reported_as_fifty():
    """The telegram shape of the live base: 15 published, 0 provable, 7 of them measured.

    Nothing may be learned from it and nothing may be hidden about it: the lesson
    says 7 measured of 7 published with 0 provable, and the sample that would have
    flipped it to `reliable` is stated rather than deleted.
    """
    fmt = _unique("analysis")
    channel = _seed("telegram", fmt, evidenced=0, unevidenced=50, engagement=900)

    report = learn_format_lessons(channel, days=WINDOW_DAYS, now=NOW)
    row = _lesson_row(channel, "telegram", fmt)
    value = dict(row["value"] or {})

    # the old denominator really was over the whole 50, and it really did say reliable
    legacy = format_performance(WINDOW_DAYS, platform="telegram", now=NOW)["formats"][fmt]
    assert legacy["measured"] == 50 and legacy["reliable"] is True

    assert value["measured"] == 50 and value["published"] == 50
    assert row["sample_size"] == 50
    assert value["measured_evidenced"] == 0
    assert value["measured_unevidenced"] == 50
    assert value["published_unevidenced"] == 50
    assert value["publication_evidence"] == EVIDENCE_NEVER
    assert value["evidenced_sample_size"] == 0
    assert value["state"] == "unevidenced" and value["reliable"] is False
    assert "доказательством отправки" in (row["note"] or "")
    # named as a refusal of its own, not folded into «never measured»
    assert report["unevidenced"] == [fmt]
    assert report["not_measured"] == []
    assert report["formats"][fmt]["state"] == "unevidenced"
    assert report["published_evidenced_total"] == 0
    assert report["published_unevidenced_total"] == 50
    assert report["published_total"] == 50
    # and the row is a record, not a steering input
    assert best_format_lesson(channel, platform="telegram") is None
    assert format_lessons(channel, platform="telegram", reliable_only=True) == []


def test_generation_stops_steering_on_formats_where_nothing_is_provable():
    """The consequence, stated plainly rather than softened.

    With every telegram publication unevidenced there is no trustworthy format
    lesson for telegram, `durable_format_preference` says so under its own state,
    and the plan falls back to the rules — not to a preference invented from 50
    rows the farm cannot show were ever sent.
    """
    persona = Persona(id="ru", title="Канал", niche="business", min_chars=150, max_chars=700)
    cluster = Cluster(
        id=f"cl-{uuid4().hex[:6]}",
        label="ЦБ сохранил ставку",
        items=[
            ItemScored(
                source_id=f"s{i}",
                external_id=f"x{i}-{uuid4().hex[:6]}",
                url=f"https://example.invalid/s{i}",
                title=f"Заголовок {i}",
                text="текст источника " * 40,
                media_kind=None,
            )
            for i in range(3)
        ],
        heat=0.2,
    )
    fmt = _unique("news")
    channel = _seed("telegram", fmt, evidenced=0, unevidenced=12, engagement=40)
    learn_format_lessons(channel, days=WINDOW_DAYS, now=NOW)

    preference = durable_format_preference(channel, "telegram")
    plan = SimpleNamespace(
        persona=persona, channel_id=channel, cluster_public_id=cluster.id, platform="telegram"
    )
    chosen = format_plan_for(
        plan, cluster_view=cluster, topic=cluster.label, media_kinds=set(),
        has_video=False, history={}, use_performance=True,
    )

    assert preference["state"] == "unevidenced"
    assert preference["history"] == {}
    assert preference["format"] == ""
    assert preference["sample_size"] == 12
    assert preference["unevidenced_measured"] == 12
    assert "без доказательства отправки" in preference["detail"]
    assert "только правила" in chosen.reason
    assert "история" not in chosen.reason
    assert chosen.fmt == plan_for_cluster(cluster, persona, has_video=False).fmt


# -------------------------------------------------------------------- the read gate
def test_the_durable_lesson_hands_the_planner_only_its_provable_half():
    """The hand-off is gated too: the planner row is the evidenced count and average.

    Seven measured telegram publications, three of them provable, the four
    phantoms each earning 900 — a lesson that steered on all seven would say
    «518.57 on 7», and `planner_history` over the same rows says «10.0 on 3».
    Two answers to one question is the defect; this pins the single one.
    """
    fmt = "stat"  # a real format name: this one reaches the planner, not a refusal
    channel = _seed("telegram", fmt, evidenced=3, unevidenced=0, engagement=10)
    for _ in range(4):
        _publish(channel, fmt, engagement=900, tg_message_id=None)
    learn_format_lessons(channel, days=WINDOW_DAYS, now=NOW)

    preference = durable_format_preference(channel, "telegram")

    assert preference["state"] == "trusted"
    assert preference["format"] == fmt
    assert preference["sample_size"] == 3
    row = preference["history"][fmt]
    assert row["measured"] == 3
    assert row["avg_engagement"] == pytest.approx(10.0)
    assert row["publication_evidence"] == EVIDENCE_MIXED
    assert row["unevidenced_measured"] == 4
    assert row["unevidenced_published"] == 4
    assert row["sample_size"] == 3
    # the blended average the phantoms drag up is nowhere in the reason
    chosen = format_plan_for(
        SimpleNamespace(
            persona=Persona(id="ru", title="Канал", niche="business", min_chars=150, max_chars=700),
            channel_id=channel, cluster_public_id="cl-x", platform="telegram",
        ),
        cluster_view=None, topic="ЦБ сохранил ставку", media_kinds=set(), has_video=False,
        history={}, use_performance=True, preference=preference,
    )
    assert "518" not in chosen.reason
    assert "урок S12" in chosen.reason


def test_a_partly_provable_lesson_is_refused_when_the_provable_part_is_empty():
    """3 live phantoms + 3 dead proofs: the freshness veto is not the one that saves it.

    The row itself is written as `reliable` — three provable measurements is a
    conclusion about the window, and a lesson is a record of that window, not a
    planner decision. What the planner may act on is the intersection: live *and*
    provable, which here is empty. Without the second veto the three fresh
    phantoms would have carried the plan at 999 apiece; with it the lesson is
    refused under its own name.
    """
    fmt = "flash"
    channel = _channel("telegram")
    for _ in range(3):
        _publish(channel, fmt, engagement=999, tg_message_id=None, collected_days_ago=FRESH)
    for index in range(3):
        _publish(channel, fmt, engagement=40, tg_message_id=7100 + index, collected_days_ago=30)
    report = learn_format_lessons(channel, days=WINDOW_DAYS, now=NOW)

    # the record still says the window was concluded, on the provable three
    assert report["formats"][fmt]["state"] == "reliable"
    assert format_state(channel, fmt, platform="telegram")["reliable"] is True

    preference = durable_format_preference(channel, "telegram")

    assert preference["state"] == "unevidenced"
    assert preference["live_sample_size"] == 0
    assert preference["sample_size"] == 6
    assert preference["unevidenced_measured"] == 3
    assert preference["publication_evidence"] == EVIDENCE_MIXED
    assert preference["history"] == {}
    # the refusal names the missing proof, not a stale collector
    assert "доказательством отправки" in preference["detail"]
    assert "старше порога" not in preference["detail"]


# ------------------------------------------------------------- one standard, checked
@pytest.mark.parametrize(
    ("platform", "evidenced", "unevidenced", "expected"),
    [
        # every publication provable: both feeds say yes, on the same sample
        ("telegram", 3, 0, True),
        ("vk", 4, 0, True),
        # nothing provable: both say no, whatever the raw `measured` count says
        ("telegram", 0, 3, False),
        ("dzen", 0, 9, False),
        # provable half below the threshold, phantom half above it: both say no
        ("telegram", 2, 6, False),
        ("dzen", 3, 2, True),
        # a `posted` journal row without a remote id is still proof (live dzen shape)
        ("dzen", 3, 0, True),
        # no rule for this platform: nothing is disproved, so nothing is vetoed
        ("max", 3, 0, True),
        ("max", 2, 0, False),
    ],
    ids=[
        "tg-3-ok", "vk-4-ok", "tg-0-phantom", "dzen-0-phantom",
        "tg-2-of-8", "dzen-3-of-5", "dzen-posted-without-id", "unrulled-3", "unrulled-2",
    ],
)
def test_planner_history_and_the_lesson_path_answer_the_same_yes_or_no(
    platform, evidenced, unevidenced, expected
):
    """The deliverable: one fixture, two feeds, one verdict — and one sample.

    `planner_history` is the live feed the planner reads; `learn_format_lessons` +
    `durable_format_preference` is the durable feed it reads instead. They are
    allowed to differ in *wording* and in nothing else. Every row here is inside
    the live window, so the freshness veto is silent and publication evidence is
    the only thing deciding.
    """
    if platform == "dzen" and evidenced and unevidenced == 0:
        # the no-remote-id journal shape needs its own seeding pass
        channel = _channel(platform)
        for _ in range(evidenced):
            _publish(channel, "stat", engagement=15, journal="posted_without_id")
    else:
        channel = _seed(platform, "stat", evidenced=evidenced, unevidenced=unevidenced,
                        engagement=15)

    history = planner_history(platform, days=WINDOW_DAYS, now=NOW)["stat"]
    learn_format_lessons(channel, days=WINDOW_DAYS, now=NOW)
    lesson = format_state(channel, "stat", platform=platform)
    preference = durable_format_preference(channel, platform)

    # 1. the same yes/no on «may this steer work»
    assert history["reliable"] is expected
    assert lesson["reliable"] is expected
    assert (preference["state"] == "trusted") is expected
    # 2. the same sample underneath it: the planner's count is the provable one,
    #    and the durable row carries the very same number under its own name
    assert history["measured"] == history["sample_size"] == evidenced
    assert lesson["measured_evidenced"] == evidenced
    assert lesson["measured"] == evidenced + unevidenced
    if expected:
        # a trusted preference hands on exactly that gated sample
        assert preference["sample_size"] == evidenced == history["measured"]
    else:
        # a refusal keeps reporting the raw count it refused, in its own state:
        # «measured 8, proved 2» is the fact, «0» would be a second fabrication
        assert preference["state"] in {"unevidenced", "below_sample"}
        assert preference["sample_size"] == evidenced + unevidenced == lesson["measured"]
    # 3. the evidence word is the same word in both feeds
    assert lesson["publication_evidence"] == history["publication_evidence"]
    assert platform in EVIDENCE_RULES or lesson["publication_evidence"] == EVIDENCE_UNRULLED


# ------------------------------------------------------------------- nothing redefined
def test_legacy_keys_keep_their_meaning_and_the_new_ones_are_added():
    """How P70 shipped, again: additive, with the old numbers still the old numbers.

    The panel reads `formats[*]` and `/api/metrics/formats` reads the report keys;
    the S12 row is read field by field by `s4_generation`. Redefining any of them
    here would change the panel and the plans with the gate switched on, which is
    why the evidenced subset travels beside the counts instead of replacing them.
    """
    fmt = _unique("digest")
    channel = _seed("telegram", fmt, evidenced=4, unevidenced=2, engagement=30)

    report = format_performance(WINDOW_DAYS, platform="telegram", now=NOW)
    assert set(report["formats"][fmt]) == LEGACY_ROW_KEYS
    assert report["formats"][fmt]["measured"] == 6
    assert report["formats"][fmt]["published"] == 6
    assert report["formats"][fmt]["avg_engagement"] == pytest.approx(30.0)
    assert report["formats"][fmt]["reliable"] is True

    learn_format_lessons(channel, days=WINDOW_DAYS, now=NOW)
    value = dict(_lesson_row(channel, "telegram", fmt)["value"] or {})
    assert LEGACY_LESSON_KEYS <= set(value)
    assert value["avg_engagement"] == pytest.approx(30.0)
    assert value["fresh_avg_engagement"] == pytest.approx(30.0)
    # the new averages are the evidenced subset, spelled `None` when it is empty
    assert value["evidenced_avg_engagement"] == pytest.approx(30.0)
    assert value["fresh_evidenced_avg_engagement"] == pytest.approx(30.0)
    assert value["measured_evidenced"] == 4 and value["measured_unevidenced"] == 2
    assert value["unevidenced_fresh"] == 2
    # and the evidenced subset of the report is what those numbers came from
    publication = report["measurement"][fmt]["publication"]
    assert publication["sample_size"] == value["measured_evidenced"]
    assert publication["published_evidenced"] == value["published_evidenced"]
    assert publication["published_unevidenced"] == value["published_unevidenced"]


def test_a_lesson_that_declares_nothing_keeps_its_old_rights():
    """Rows written before this gate are not re-judged behind the operator's back.

    The next learning cycle rewrites them with the marker, so the veto closes on
    its own; inventing one now would silently change stored memory the farm never
    measured this way, which is the same call P70 made for `evidence_status`.
    """
    platform = _unique("tamworth")  # no rule for this platform at all
    fmt = "news"  # a real format name, so a trusted lesson really can steer
    channel = _channel(platform)
    for _ in range(3):
        _publish(channel, fmt, engagement=50, journal=None)
    learn_format_lessons(channel, days=WINDOW_DAYS, now=NOW)
    key = _track_key(platform, fmt)
    with session_scope() as session:
        row = session.execute(
            select(LearningMemory).where(
                LearningMemory.channel_id == channel, LearningMemory.kind == "format"
            )
        ).scalar_one()
        value = dict(row.value or {})
        for marker in (
            "publication_evidence", "measured_evidenced", "measured_unevidenced",
            "published_evidenced", "published_unevidenced", "evidenced_sample_size",
            "unevidenced_fresh", "evidenced_avg_engagement",
            "fresh_evidenced_avg_engagement", "publication_rule",
        ):
            value.pop(marker, None)
        row.value = value

    assert lessons(channel, kind="format")
    assert format_state(channel, fmt, platform=platform)["reliable"] is True
    preference = durable_format_preference(channel, platform)
    assert preference["state"] == "trusted"
    assert preference["sample_size"] == 3
    assert "publication_evidence" not in preference["history"][fmt]
    assert key == f"{platform}/{fmt}"


def test_the_rule_used_is_the_platform_rule_and_not_an_invented_one():
    """A platform S8 has no rule for is `unrulled`, which is not the same as false.

    Demoting unrulled rows would be a fabrication in the other direction: 15 live
    telegram rows fail `posts.tg_message_id`, and a Reddit post has no rule to fail.
    """
    assert EVIDENCE_RULES == {"telegram": RULE_MESSAGE_ID, "dzen": RULE_JOURNAL, "vk": RULE_JOURNAL}

    fmt = _unique("news")
    channel = _seed("pikabu", fmt, evidenced=3, unevidenced=0, engagement=12)
    learn_format_lessons(channel, days=WINDOW_DAYS, now=NOW)
    value = dict(_lesson_row(channel, "pikabu", fmt)["value"] or {})
    assert value["publication_rule"] is None
    assert value["publication_evidence"] == EVIDENCE_UNRULLED
    assert value["published_unrulled"] == 3
    assert value["measured_evidenced"] == 3
    assert value["state"] == "reliable"

    telegram_channel = _seed("telegram", fmt, evidenced=3, unevidenced=0, engagement=12)
    learn_format_lessons(telegram_channel, days=WINDOW_DAYS, now=NOW)
    tg_value = dict(_lesson_row(telegram_channel, "telegram", fmt)["value"] or {})
    assert tg_value["publication_evidence"] == EVIDENCE_OK
    assert tg_value["published_unevidenced"] == 0
    assert tg_value["state"] == "reliable"
