"""S12 — self-learning memory for the farm.

Lessons live in `LearningMemory` under `kind` — today `strategy`, `slot`, `tactic`,
`mistake` and `format` — and every one of them has a named reader:
`tests/test_lesson_readers.py` derives the kinds written below from this module's own
source and fails when a kind appears here with no read anywhere in `tgfarm/`. A format
lesson is keyed `platform/format` per channel and is written only from posts S8
actually measured — an unmeasured publication is activity, not evidence, and a
below-threshold sample is stored as an observation, never as a conclusion that may
steer generation. Measured is a second question from «did it happen at all», and the
lesson asks both: a `published` row the platform's own evidence rule disproves does
not count toward the sample that makes a lesson `reliable`, so a format can be stored
as `unevidenced` — measured often, provable never (`_format_state`).

There used to be one more kind, `angle`: one row per eligible angle per channel per
cycle, holding the same `er`/`ctr`/`quality`/`n` the strategy row is computed from.
Nothing in the package ever read it — the per-angle conclusions are consumed as
`strategy/angle_weights` (S3), `tactic/best_angle` and `mistake/worst_angle` (the S4
prompt) — so the rows were a cache with no reader, and they are gone.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import select

from tgfarm.core.logging import get_logger
from tgfarm.core.run import to_thread
from tgfarm.db.models import Channel as ChannelRow
from tgfarm.db.models import LearningMemory
from tgfarm.db.session import session_scope
from tgfarm.s8_analytics import (
    angle_performance,
    hook_style_performance,
    recent_metrics,
)
from tgfarm.s8_analytics.formats import EVIDENCE_RULES, evidence_status_for, format_performance
from tgfarm.s8_analytics.formats import MIN_SAMPLE as FORMAT_MIN_SAMPLE

log = get_logger(__name__)

MIN_SAMPLES = 3
#: Window used when the caller does not name one. S8's own planner window is read
#: from settings at call time; this is only the fallback when settings are
#: unreadable, and it matches the window the planner measures over.
FORMAT_WINDOW_DAYS = 60
# Treat the equivalent of eight posts as prior evidence. Small samples move
# strategy weights only a little; confidence rises as real observations accrue.
PRIOR_STRENGTH = 8.0


def _clamp_quality(value: float, default: float = 0.5) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(score):
        return default
    return min(1.0, max(0.0, score))


def _shrink_quality(
    score: float,
    baseline: float,
    sample_size: int,
    prior_strength: float = PRIOR_STRENGTH,
) -> float:
    """Shrink a noisy observed score toward its channel baseline.

    The sample size is the weight of the observed data; the prior strength is
    the weight of the baseline. A lucky three-post streak therefore cannot
    outweigh a pattern measured across dozens of posts.
    """
    prior = max(0.0, float(prior_strength))
    n = max(0, int(sample_size))
    base = _clamp_quality(baseline)
    observed = _clamp_quality(score)
    if n == 0:
        return round(base, 4)
    if prior == 0:
        return round(observed, 4)
    return round((observed * n + base * prior) / (n + prior), 4)


def _sample_confidence(sample_size: int) -> float:
    """Confidence as the share of observed evidence after prior smoothing.

    Keep the mathematical value unrounded; presentation and persisted summaries
    may round it, but strategy weighting and tests should use the actual ratio.
    """
    n = max(0, int(sample_size))
    return n / (n + PRIOR_STRENGTH) if n else 0.0


def _confidence_weighted_qualities(performance: dict) -> tuple[float, dict[str, dict]]:
    """Return a sample-weighted baseline and shrinkage estimates for eligible groups."""
    observed: dict[str, tuple[float, int]] = {}
    for key, data in performance.items():
        try:
            n = int(data.get("n", 0))
            score = float(data.get("quality", 0.5))
        except (TypeError, ValueError):
            continue
        if n < MIN_SAMPLES or not math.isfinite(score):
            continue
        observed[str(key)] = (_clamp_quality(score), n)

    total_n = sum(n for _, n in observed.values())
    if total_n == 0:
        return 0.5, {}

    baseline = sum(score * n for score, n in observed.values()) / total_n
    evidence = {
        key: {
            "observed_quality": score,
            "quality": _shrink_quality(score, baseline, n),
            "n": n,
        }
        for key, (score, n) in observed.items()
    }
    return round(baseline, 4), evidence


def _rounded_or_none(value: object) -> float | None:
    """An average that may honestly be absent: `None` stays `None`, never `0`.

    The house idiom, and the reason a reader of a lesson can tell «not one
    evidenced measurement» from «an evidenced measurement that earned nothing»:
    `s8_analytics.formats._rate` returns `None` for an empty sample, and S4 reads
    that `None` as a refusal instead of a rankable zero.
    """
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 4)


def _upsert(channel_id, kind, key, value, score, sample_size, confidence, note=None):
    with session_scope() as session:
        row = session.execute(
            select(LearningMemory).where(
                LearningMemory.channel_id == channel_id,
                LearningMemory.kind == kind,
                LearningMemory.key == key,
            )
        ).scalar_one_or_none()
        if row is None:
            row = LearningMemory(
                channel_id=channel_id, kind=kind, key=key, value=value,
                score=score, sample_size=sample_size, confidence=confidence, note=note,
            )
            session.add(row)
        else:
            row.value = value
            row.score = score
            row.sample_size = sample_size
            row.confidence = confidence
            row.note = note


def lessons(channel_id="", kind=None):
    with session_scope() as session:
        stmt = select(LearningMemory)
        if channel_id:
            stmt = stmt.where(LearningMemory.channel_id == channel_id)
        if kind:
            stmt = stmt.where(LearningMemory.kind == kind)
        rows = session.execute(stmt).scalars().all()
        return [
            {
                "channel_id": r.channel_id,
                "kind": r.kind,
                "key": r.key,
                "value": r.value or {},
                "score": r.score,
                "sample_size": r.sample_size,
                "confidence": r.confidence,
                "note": r.note,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]


def best_angle_weights(channel_id):
    rows = lessons(channel_id, kind="strategy")
    for r in rows:
        if r["key"] == "angle_weights" and isinstance(r["value"], dict):
            return {str(k): float(v) for k, v in r["value"].items()}
    return None


def best_hook_style_weights(channel_id):
    rows = lessons(channel_id, kind="strategy")
    for r in rows:
        if r["key"] == "hook_style_weights" and isinstance(r["value"], dict):
            return {str(k): float(v) for k, v in r["value"].items()}
    return None


def _relative_quality(average: float, baseline: float) -> float:
    """Measured engagement as a share of itself plus the platform average.

    A ratio, not a percentile: with a dozen measured posts a percentile would be
    a story told about noise, while ``avg / (avg + baseline)`` only says «лучше
    или хуже среднего по этой площадке», stays monotone in the real number and
    lands in the 0..1 range every other S12 score uses.
    """
    avg = max(0.0, float(average))
    base = max(0.0, float(baseline))
    total = avg + base
    if total <= 0:
        return 0.5
    return round(avg / total, 4)


def _channel_row(channel_id: str) -> tuple[str, str]:
    """(platform, niche) of a channel as the farm's own catalogue states them."""
    with session_scope() as session:
        row = session.execute(
            select(ChannelRow.platform, ChannelRow.niche).where(ChannelRow.id == channel_id)
        ).one_or_none()
    if row is None:
        return "", ""
    return str(row[0] or "").strip().lower(), str(row[1] or "").strip().lower()


def _format_window_days() -> int:
    """The window S8 measures formats over, so learning and planning agree."""
    try:
        from tgfarm.core.settings import get_settings

        configured = getattr(get_settings(), "format_performance_window_days", FORMAT_WINDOW_DAYS)
        return max(1, int(configured))
    except Exception as exc:  # noqa: BLE001 - an unreadable setting is not a verdict
        log.debug("s12: format window setting unreadable: %s", exc)
        return FORMAT_WINDOW_DAYS


def _format_state(
    sample_size: int,
    measured: int,
    min_sample: int,
    evidenced: int | None = None,
) -> str:
    """One of `not_measured` / `unevidenced` / `below_sample` / `reliable`.

    The four are not interchangeable: a format nobody ever measured is a blind
    spot, a format whose rows nothing can point at is a *different* blind spot, a
    format measured twice is a hint, and only `reliable` may steer work.

    `evidenced` is the second question `s8_analytics.formats` already asks of the
    same rows — of the posts counted above, how many are backed by proof the
    publication happened (a telegram message id, a journalled `posted` row) — and
    it is what decides here, exactly as it decides in `planner_history`
    (`s8_analytics/formats.py:595`, `:602`). A `published` row the platform's own
    rule disproves is activity, not an outcome, so it may not top up the sample
    that flips a lesson to `reliable`; 50 phantoms are worth less than 3
    provable posts, and a lesson built on them would teach the farm that a send
    it cannot show ever happened is a send that worked.

    `None` means the caller declares nothing about publication — a row written
    before this gate existed, or a report built without the ``publication`` block
    — and then the ladder keeps the meaning it has always had, the same courtesy
    `s4_generation.durable_format_preference` extends to a lesson with no
    freshness marker.
    """
    if int(measured or 0) <= 0 or int(sample_size or 0) <= 0:
        return "not_measured"
    size = int(sample_size)
    if evidenced is None:
        return "reliable" if size >= max(1, int(min_sample)) else "below_sample"
    provable = max(0, int(evidenced))
    if provable <= 0 and provable < size:
        # Not «too few measurements»: every measurement this format has is a
        # publication nobody can point at. Collecting more of the same posts
        # would not move it, which is why it is not called `below_sample`.
        return "unevidenced"
    if provable < max(1, int(min_sample)):
        return "below_sample"
    return "reliable"


def learn_format_lessons(
    channel_id: str,
    *,
    platform: str | None = None,
    days: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Store *measured* format outcomes as lessons; never store an unmeasured one.

    S8 already answers «how did this format do on this platform» and already
    carries the honest sample size, the `min_sample` and the `reliable` flag, so
    this reuses those numbers instead of inventing a second threshold:

    * the report is platform-scoped — a Reddit result has not earned the right to
      decide a Дзен post, and an unknown platform stops learning rather than
      pooling destinations into one number;
    * a published post with no stats is not evidence: it raises `published`, never
      an average, and a format whose measured count is zero writes no lesson;
    * a measured post whose publication nothing can point at is not an outcome
      either: the reliability sample is the *evidenced* subset S8 already
      computes (`measurement[fmt]["publication"]["sample_size"]`), so phantoms
      cannot top it up — while `measured`/`published`/`sample_size` on the row
      keep counting what they always counted, and the disproved count travels in
      `measured_unevidenced` and in the note. This is the same veto
      `planner_history` applies to the live feed, so the two bodies of evidence
      answer «may this steer work» with one word, not two;
    * a format measured below `min_sample` is stored as an observation with its
      sample size and state `below_sample`, never phrased as a conclusion;
    * the score is shrunk toward «as good as the platform average» by the same
      `_shrink_quality` prior the angles and slots use, so a lucky two-post
      streak cannot outweigh a format measured across dozens.

    A measurement that is no longer live is *stored and marked*, not dropped: the
    row says `fresh_measured`/`stale_measured`/`evidence_status` and its note
    states plainly that the plan does not build on it. Deleting it would make the
    blind spot disappear from the very memory whose job is to record blind spots,
    while `s4_generation.durable_format_preference` refuses every status in
    `s8_analytics.formats.EVIDENCE_UNTRUSTED` — so a stale lesson can inform a
    reader but cannot steer a generation, and the choice falls back to the live
    window or the rules instead of a fabricated priority. A publication that cannot
    be proven is treated the same way and for the same reason: the counts stay in
    the row (`published_unevidenced`/`measured_unevidenced`), but they do not enter
    the sample the verdict rests on, and `s4_generation` subtracts them from both
    the count and the average it hands the planner — one standard, the one
    `planner_history` already applies, not a second one.

    Returns the run summary (rows written, per-format state, the not-measured and
    the unevidenced lists) so a caller can report what it refused to learn.
    """
    channel = str(channel_id or "").strip()
    if not channel:
        return {
            "channel_id": "", "platform": "", "written": 0,
            "window_days": int(days) if days else _format_window_days(),
            "min_sample": FORMAT_MIN_SAMPLE, "formats": {}, "not_measured": [],
            "unevidenced": [],
            "reason": "урок формата требует канала",
        }

    window = int(days) if days else _format_window_days()
    known_platform, niche = _channel_row(channel)
    platform_key = str(platform).strip().lower() if platform else known_platform
    if not platform_key:
        return {
            "channel_id": channel, "platform": "", "written": 0, "window_days": window,
            "min_sample": FORMAT_MIN_SAMPLE, "formats": {}, "not_measured": [],
            "unevidenced": [],
            "reason": "площадка канала неизвестна: смешивать платформы в одном уроке нельзя",
        }

    report = format_performance(window, platform=platform_key, now=now)
    min_sample = max(1, int(report.get("min_sample") or FORMAT_MIN_SAMPLE))
    formats = report.get("formats") or {}
    # Same posts, second question: `formats` answers «was it measured», this
    # answers «is that measurement still live». Both are stored, because a lesson
    # is read long after the window it came from is gone, and a reader then has no
    # way to tell 3 live snapshots from 3 snapshots nobody refreshed in a month.
    measurement = report.get("measurement") or {}
    # Report-level publication block: the counts the whole window was measured on.
    # Read here instead of re-summed, so learning and the report cannot drift.
    report_publication = report.get("publication") or {}
    stale_after_days = int(report.get("stale_after_days") or 0)
    measured_total = sum(int(item.get("measured") or 0) for item in formats.values())
    engagement_total = sum(
        float(item.get("avg_engagement") or 0.0) * int(item.get("measured") or 0)
        for item in formats.values()
    )
    baseline = engagement_total / measured_total if measured_total else 0.0

    written = 0
    states: dict[str, dict[str, Any]] = {}
    not_measured: list[str] = []
    unevidenced: list[str] = []
    for raw_format, item in formats.items():
        fmt = str(raw_format or "").strip().lower()
        if not fmt:
            continue
        measured = int(item.get("measured") or 0)
        published = int(item.get("published") or 0)
        average = float(item.get("avg_engagement") or 0.0)
        evidence = measurement.get(fmt) or {}
        fresh = int(evidence.get("fresh") or 0)
        stale = int(evidence.get("stale") or 0)
        evidence_status = str(evidence.get("evidence_status") or "") or evidence_status_for(
            fresh, stale
        )
        fresh_average = float(evidence.get("fresh_avg_engagement") or 0.0)
        # Third question of the same rows, answered by S8 and reused here verbatim:
        # which of these publications can anything point at. An absent block is a
        # report that declares nothing, and a declaring nothing is the old ladder —
        # the same courtesy the freshness marker gets in S4.
        publication = evidence.get("publication") or {}
        declares = bool(publication)
        published_evidenced = int(publication.get("published_evidenced") or 0)
        published_unevidenced = int(publication.get("published_unevidenced") or 0)
        published_unrulled = int(publication.get("published_unrulled") or 0)
        measured_evidenced = int(publication.get("sample_size") or 0)
        measured_unevidenced = int(publication.get("measured_unevidenced") or 0)
        fresh_evidenced = int(publication.get("fresh_sample_size") or 0)
        unevidenced_fresh = int(publication.get("fresh_unevidenced") or 0)
        publication_status = str(publication.get("evidence_status") or "")
        evidenced_average = publication.get("avg_engagement")
        fresh_evidenced_average = publication.get("fresh_avg_engagement")
        if measured <= 0:
            # Activity, not evidence: named as a blind spot in this run's summary,
            # never stored as a row a later reader could mistake for a verdict.
            states[fmt] = {
                "state": "not_measured", "measured": 0, "published": published,
                "sample_size": 0, "confidence": 0.0, "reliable": False,
                "fresh_measured": 0, "stale_measured": 0, "evidence_status": "never",
                # How much of that unpublished-looking activity is itself a
                # phantom: «50 published, 35 provable» is the operator's read, not
                # a number this function may quietly round down to 35.
                "published_evidenced": published_evidenced,
                "published_unevidenced": published_unevidenced,
                "publication_evidence": publication_status,
            }
            not_measured.append(fmt)
            continue

        observed = _relative_quality(average, baseline)
        quality = _shrink_quality(observed, 0.5, measured)
        state = _format_state(
            measured, measured, min_sample,
            evidenced=measured_evidenced if declares else None,
        )
        reliable = state == "reliable"
        if state == "unevidenced":
            # A named refusal, in the same shape as `not_measured`: a caller that
            # only counts written rows cannot tell «learned nothing» from «measured
            # 50 posts and could prove none of them».
            unevidenced.append(fmt)
        confidence = _sample_confidence(measured)
        value = {
            "format": fmt,
            "platform": platform_key,
            "niche": niche,
            "measured": measured,
            "published": published,
            "avg_engagement": round(average, 4),
            "baseline_engagement": round(baseline, 4),
            "relative_quality": observed,
            "quality": quality,
            "min_sample": min_sample,
            "reliable": reliable,
            "state": state,
            "window_days": window,
            # How much of `measured` was live when this lesson was written. The
            # counts and `state` above keep meaning exactly what they meant (a
            # lesson is a record of the window, not a planner decision), so this
            # is where `s4_generation.durable_format_preference` reads whether the
            # lesson is allowed to steer.
            "fresh_measured": fresh,
            "stale_measured": stale,
            "fresh_avg_engagement": round(fresh_average, 4),
            "evidence_status": evidence_status,
            "evidence_stale_after_days": stale_after_days,
            # Publication evidence, the veto that decides `state` above. Nothing in
            # the four legacy counts was redefined: `measured`, `published`,
            # `sample_size` (the stored column) and `avg_engagement` still mean what
            # they meant when this lesson was written, and the provable subset
            # travels beside them so a reader can see «50 published, 35 provable»
            # instead of a silently smaller sample.
            "publication_evidence": publication_status,
            "publication_rule": EVIDENCE_RULES.get(platform_key),
            "published_evidenced": published_evidenced,
            "published_unevidenced": published_unevidenced,
            "published_unrulled": published_unrulled,
            "measured_evidenced": measured_evidenced if declares else measured,
            "measured_unevidenced": measured_unevidenced,
            # The sample a *decision* could rest on right now: live snapshots that
            # are also evidenced, the same number `planner_history` calls
            # `sample_size` and the same one S4 hands the planner.
            "evidenced_sample_size": fresh_evidenced if declares else min(fresh, measured),
            "unevidenced_fresh": unevidenced_fresh,
            "evidenced_avg_engagement": _rounded_or_none(evidenced_average),
            "fresh_evidenced_avg_engagement": _rounded_or_none(fresh_evidenced_average),
        }
        evidence_tail = (
            ""
            if evidence_status == "ok"
            else f"; живых измерений {fresh} из {measured}, состояние {evidence_status}"
        )
        # Named in every note, not only in the payload: a reader of
        # `learning_memory` must be able to see that the sample was gated, not
        # that it shrank on its own.
        publication_tail = (
            ""
            if not declares or measured_unevidenced <= 0
            else f"; {measured_unevidenced} из {measured} измерений — публикации без "
                 "доказательства отправки, в выборку не вошли"
        )
        if state == "reliable":
            reason = (
                f"формат {fmt} на {platform_key}: измерено {measured} из {published}, "
                f"доказанных {measured_evidenced if declares else measured}, "
                f"вовлечённость {average:.2f} против {baseline:.2f} по площадке; надёжно"
                f"{evidence_tail}{publication_tail}"
            )
        elif state == "unevidenced":
            reason = (
                f"формат {fmt} на {platform_key}: измерено {measured} из {published}, "
                f"но ни одну публикацию нельзя подтвердить доказательством отправки "
                f"({measured_unevidenced} без доказательства) — наблюдение, не вывод"
            )
        else:
            reason = (
                f"формат {fmt} на {platform_key}: измерено {measured} из {published} "
                f"(порог {min_sample}) — наблюдение, не вывод{evidence_tail}"
                f"{publication_tail}"
            )
        if evidence_status == "stale":
            reason = (
                f"формат {fmt} на {platform_key}: {measured} измерений, но все снимки "
                f"старше {stale_after_days} дн(а) — не живое доказательство, "
                "план на этом уроке не строится"
                f"{publication_tail}"
            )
        _upsert(
            channel, "format", f"{platform_key}/{fmt}", value,
            quality, measured, confidence, reason,
        )
        states[fmt] = {
            "state": state, "measured": measured, "published": published,
            "sample_size": measured, "confidence": confidence, "reliable": reliable,
            "fresh_measured": fresh, "stale_measured": stale,
            "evidence_status": evidence_status,
            "measured_evidenced": measured_evidenced if declares else measured,
            "measured_unevidenced": measured_unevidenced,
            "evidenced_sample_size": fresh_evidenced if declares else min(fresh, measured),
            "publication_evidence": publication_status,
        }
        written += 1

    return {
        "channel_id": channel,
        "platform": platform_key,
        "niche": niche,
        "written": written,
        "window_days": window,
        "min_sample": min_sample,
        "measured_total": measured_total,
        "published_total": int(report.get("published_total") or 0),
        "fresh_measured_total": int(report.get("fresh_measured_total") or 0),
        "stale_measured_total": int(report.get("stale_measured_total") or 0),
        "evidence_status": str(report.get("evidence_status") or ""),
        "stale_after_days": stale_after_days,
        # The same window, third question, taken from the report rather than
        # summed here so the two can never drift apart: what the farm can prove
        # it published, beside what it claims it published.
        "published_evidenced_total": int(report_publication.get("published_evidenced") or 0),
        "published_unevidenced_total": int(report_publication.get("published_unevidenced") or 0),
        "published_unrulled_total": int(report_publication.get("published_unrulled") or 0),
        "evidenced_measured_total": int(report_publication.get("sample_size") or 0),
        "publish_unknown_total": int(report_publication.get("publish_unknown") or 0),
        "publication_evidence": str(report_publication.get("evidence_status") or ""),
        "baseline_engagement": round(baseline, 4),
        "formats": states,
        "not_measured": sorted(not_measured),
        "unevidenced": sorted(unevidenced),
        "reason": (
            f"формат: {written} урок(ов) записано по {measured_total} измеренным публикациям"
            f" (доказано {int(report_publication.get('published_evidenced') or 0)})"
            if written
            else "нет ни одного измеренного формата — уроков не записано"
        ),
    }


def format_lessons(
    channel_id: str = "",
    *,
    platform: str = "",
    reliable_only: bool = False,
) -> list[dict[str, Any]]:
    """Stored format lessons: value, sample size, confidence and the reason.

    Rows are keyed ``platform/format`` and scoped per channel, so one format name
    on two platforms stays two lessons and can never be averaged into one number.
    `state` is read back from the row rather than re-guessed, which is what lets a
    caller tell «not measured» apart from «measured and weak» — and, since P73,
    «measured but not one publication provable» (`unevidenced`) apart from both.
    `reliable` is the state read back, never a re-derived threshold, so a row
    written under the evidence gate cannot be promoted by a reader that forgot it.
    """
    rows = lessons(channel_id, kind="format")
    wanted_platform = str(platform or "").strip().lower()
    out: list[dict[str, Any]] = []
    for row in rows:
        value = dict(row.get("value") or {})
        head, _, tail = str(row.get("key") or "").partition("/")
        fmt = str(value.get("format") or tail).strip().lower()
        row_platform = str(value.get("platform") or head).strip().lower()
        if wanted_platform and row_platform != wanted_platform:
            continue
        sample_size = int(row.get("sample_size") or 0)
        min_sample = max(1, int(value.get("min_sample") or FORMAT_MIN_SAMPLE))
        measured = int(value.get("measured") or sample_size)
        # A row that declares its provable subset is judged on that subset; a row
        # written before the gate existed declares nothing and keeps its old ladder.
        declared_evidenced = value.get("measured_evidenced")
        state = (
            str(value.get("state") or "")
            or _format_state(
                sample_size, measured, min_sample,
                evidenced=None if declared_evidenced is None else int(declared_evidenced),
            )
        )
        out.append(
            {
                **row,
                "format": fmt,
                "platform": row_platform,
                "measured": measured,
                "published": int(value.get("published") or 0),
                "min_sample": min_sample,
                "state": state,
                "reliable": state == "reliable",
                "measured_evidenced": (
                    measured if declared_evidenced is None else int(declared_evidenced)
                ),
                "measured_unevidenced": int(value.get("measured_unevidenced") or 0),
                "published_unevidenced": int(value.get("published_unevidenced") or 0),
                "publication_evidence": str(value.get("publication_evidence") or ""),
                "reason": row.get("note") or "",
            }
        )
    out.sort(
        key=lambda item: (
            -float(item.get("score") or 0.0),
            -int(item.get("sample_size") or 0),
            str(item.get("platform") or ""),
            str(item.get("format") or ""),
        )
    )
    if reliable_only:
        out = [item for item in out if item["reliable"]]
    return out


def format_state(channel_id: str, fmt: str, *, platform: str = "") -> dict[str, Any]:
    """Honest state of one format on one channel, with a reason either way.

    Missing history answers `not_measured` instead of pretending the format was
    tried and failed — that difference is exactly what keeps an unmeasured post
    from being read as a verdict.
    """
    wanted = str(fmt or "").strip().lower()
    for item in format_lessons(channel_id, platform=platform):
        if item["format"] == wanted:
            return item
    return {
        "channel_id": str(channel_id or ""),
        "platform": str(platform or "").strip().lower(),
        "key": f"{str(platform or '').strip().lower()}/{wanted}",
        "kind": "format",
        "value": {},
        "format": wanted,
        "score": 0.0,
        "sample_size": 0,
        "measured": 0,
        "published": 0,
        "measured_evidenced": 0,
        "measured_unevidenced": 0,
        "published_unevidenced": 0,
        "publication_evidence": "",
        "confidence": 0.0,
        "min_sample": FORMAT_MIN_SAMPLE,
        "state": "not_measured",
        "reliable": False,
        "reason": "нет ни одного измеренного наблюдения — вывод о формате сделать нельзя",
    }


def best_format_lesson(channel_id: str, *, platform: str = "") -> dict[str, Any] | None:
    """Best trustworthy format lesson, or `None` when history cannot be trusted.

    `None` is the point: with no honest sample the caller keeps its previous
    strategy rather than promoting a preference invented from two posts.
    """
    candidates = format_lessons(channel_id, platform=platform, reliable_only=True)
    return candidates[0] if candidates else None


def record_gate_failure(channel_id: str, gate: str, reason: str) -> None:
    """Accumulate repeated gate rejections instead of overwriting the count.

    A gate that has rejected 300 drafts and a gate that rejected one both looked
    like `sample_size=1` while the row was rewritten on every rejection, which
    erased the frequency signal S4 ranks on. The reason tail stays bounded so one
    noisy gate cannot grow the prompt without limit.
    """
    channel = str(channel_id or "").strip()
    gate_name = " ".join(str(gate or "gate").split())[:96] or "gate"
    safe_reason = " ".join(str(reason or "").split())[:300]
    if not channel:
        return

    key = f"gate:{gate_name}"[:128]
    with session_scope() as session:
        row = session.execute(
            select(LearningMemory).where(
                LearningMemory.channel_id == channel,
                LearningMemory.kind == "mistake",
                LearningMemory.key == key,
            )
        ).scalar_one_or_none()
        previous = dict(row.value or {}) if row is not None else {}
        count = max(
            int(row.sample_size or 0) if row is not None else 0,
            int(previous.get("occurrences") or 0),
        ) + 1
        recent = previous.get("recent_reasons")
        recent_reasons = [str(item)[:300] for item in recent] if isinstance(recent, list) else []
        recent_reasons.append(safe_reason)
        value = {
            "gate": gate_name,
            "reason": safe_reason,
            "occurrences": count,
            "recent_reasons": recent_reasons[-5:],
        }
        confidence = _sample_confidence(count)
        note = f"гейт {gate_name}: {count} отклонений; последний пример: {safe_reason[:120]}"
        if row is None:
            session.add(
                LearningMemory(
                    channel_id=channel,
                    kind="mistake",
                    key=key,
                    value=value,
                    score=0.0,
                    sample_size=count,
                    confidence=confidence,
                    note=note,
                )
            )
        else:
            row.value = value
            row.score = 0.0
            row.sample_size = count
            row.confidence = confidence
            row.note = note


def best_hours(channel_id):
    """The hours this channel's own measured slots earned, and nothing else.

    `sample_size >= MIN_SAMPLES` is the whole gate: below three measured posts in
    `slot` rows (written by `_learn_channel` from `recent_metrics`) an hour is an
    anecdote, and `s7_publish.plan_slot` doubles the weight of whatever comes back.
    """
    rows = lessons(channel_id, kind="slot")
    out = sorted(rows, key=lambda r: r["score"], reverse=True)
    return [int(r["key"]) for r in out if r["sample_size"] >= MIN_SAMPLES][:5]


def mistakes(channel_id):
    """Every `mistake` row stored for one channel, as plain dicts.

    The kind has one consumer: `tgfarm/s4_generation/_persona_block`, which ranks
    the rows by `sample_size` and renders at most five `Избегать:` lines into the
    draft prompt. `tests/test_mistake_lesson_consumer.py` pins that path row by row,
    including the live shape (`score=0.0`, `sample_size=1`, `confidence` 0.2-0.3),
    so a reader that grew a confidence threshold — which no production row reaches
    — fails a test instead of silently unplugging the loop. `score` and `confidence`
    themselves have no consumer for this kind: every writer stores `score=0.0`,
    because a rejected draft is a counted event, not a measured quality.
    """
    return lessons(channel_id, kind="mistake")


def _channel_ids():
    from tgfarm.config import get_config
    return [p.id for p in get_config().enabled_personas]


def _learn_channel(channel_id):
    learned = 0
    perf = angle_performance(14, channel_id=channel_id)
    if perf:
        baseline, evidence = _confidence_weighted_qualities(perf)
        # No `kind="angle"` row is written here any more. The loop that wrote one
        # per eligible angle cost 5 rows and 10 statements (a SELECT and an INSERT
        # per `_upsert`) per channel per cycle, ~1.9 ms each — 62% of this stage's
        # DB writes (measured: 55 of 88 upserts, 103 of 209 ms over an
        # 11-channel fleet) — and no code in `tgfarm/` ever read the kind back.
        # What the angle evidence decides is already stored under kinds that do
        # have readers, all computed from `evidence` below:
        # `strategy/angle_weights` (read by `s12_learning.best_angle_weights`,
        # consumed at `s3_router/__init__.py:514` and `:557`), `tactic/best_angle`
        # and `mistake/worst_angle` (both rendered by
        # `s4_generation/__init__.py:129-136`).
        # Do not overwrite a previously learned strategy until at least one
        # angle has enough observations to clear the sample-size threshold.
        qualities = {angle: item["quality"] for angle, item in evidence.items()}
        if qualities:
            try:
                from tgfarm.config import get_config
                persona = get_config().persona(channel_id)
                base = dict(persona.angle_weights or {"news": 1.0}) if persona else {"news": 1.0}
            except Exception:
                base = {"news": 1.0}

            adjusted = {}
            for angle, weight in base.items():
                q = qualities.get(angle, baseline)
                factor = 1.0 + (q - baseline)
                adjusted[angle] = max(0.05, round(float(weight) * factor, 4))
            for angle, q in qualities.items():
                if angle not in adjusted:
                    adjusted[angle] = max(0.05, round(q, 4))
            total = sum(adjusted.values()) or 1.0
            adjusted = {key: round(value / total, 4) for key, value in adjusted.items()}
            best = max(qualities, key=lambda angle: qualities[angle])
            worst = min(qualities, key=lambda angle: qualities[angle])
            sample_total = sum(item["n"] for item in evidence.values())
            _upsert(
                channel_id, "strategy", "angle_weights", adjusted,
                max(qualities.values()), sample_total, _sample_confidence(sample_total),
                "baseline=%.3f best=%s worst=%s" % (baseline, best, worst),
            )
            learned += 1
            _upsert(
                channel_id, "tactic", "best_angle", {"angle": best}, qualities[best],
                evidence[best]["n"], _sample_confidence(evidence[best]["n"]),
                "best angle: %s" % best,
            )
            if worst != best:
                _upsert(
                    channel_id, "mistake", "worst_angle",
                    {"angle": worst, "quality": qualities[worst]}, qualities[worst],
                    evidence[worst]["n"], _sample_confidence(evidence[worst]["n"]),
                    "weak angle: %s" % worst,
                )
                learned += 1

    style_perf = hook_style_performance(14, channel_id=channel_id)
    if style_perf:
        style_baseline, style_evidence = _confidence_weighted_qualities(style_perf)
        style_qualities = {
            style: item["quality"] for style, item in style_evidence.items()
        }
        if style_qualities:
            base_styles = {"fact": 0.55, "stat": 0.30, "question": 0.15}
            adjusted_styles: dict[str, float] = {}
            for style, weight in base_styles.items():
                q = style_qualities.get(style, style_baseline)
                factor = 1.0 + (q - style_baseline)
                adjusted_styles[style] = max(0.05, round(float(weight) * factor, 4))
            for style, q in style_qualities.items():
                if style not in adjusted_styles:
                    adjusted_styles[style] = max(0.05, round(q, 4))
            style_total = sum(adjusted_styles.values()) or 1.0
            adjusted_styles = {
                key: round(value / style_total, 4) for key, value in adjusted_styles.items()
            }
            best_style = max(style_qualities, key=lambda style: style_qualities[style])
            style_sample_total = sum(item["n"] for item in style_evidence.values())
            _upsert(
                channel_id, "strategy", "hook_style_weights", adjusted_styles,
                max(style_qualities.values()), style_sample_total,
                _sample_confidence(style_sample_total),
                "baseline=%.3f best hook style=%s" % (style_baseline, best_style),
            )
            learned += 1

    from tgfarm.core.run import ensure_utc
    try:
        formats = learn_format_lessons(channel_id)
    except Exception as exc:  # noqa: BLE001 - thin format data must not cost the other lessons
        formats = {"written": 0}
        # A skipped lesson is a lost learning cycle, and at debug level the farm
        # stops learning formats without ever saying so.
        log.warning("s12: format learning skipped for %s: %s", channel_id, exc)
    learned += int(formats.get("written", 0))

    channel_metrics = [m for m in recent_metrics(14) if m.channel_id == channel_id]
    buckets = defaultdict(list)
    qualified_scores = []
    for metric in channel_metrics:
        published = ensure_utc(metric.published_at)
        if published and (metric.impressions >= 100 or metric.views >= 30):
            buckets[published.hour].append(metric.quality)
            qualified_scores.append(metric.quality)
    slot_baseline = (
        sum(qualified_scores) / len(qualified_scores) if qualified_scores else 0.5
    )
    for hour, values in buckets.items():
        if len(values) < MIN_SAMPLES:
            continue
        observed = sum(values) / len(values)
        score = _shrink_quality(observed, slot_baseline, len(values))
        _upsert(
            channel_id, "slot", str(hour),
            {
                "hour": hour,
                "n": len(values),
                "quality": score,
                "observed_quality": round(observed, 4),
                "baseline": round(slot_baseline, 4),
            },
            score, len(values), _sample_confidence(len(values)),
            "slot %d: learned=%.3f observed=%.3f baseline=%.3f n=%d"
            % (hour, score, observed, slot_baseline, len(values)),
        )
        learned += 1

    try:
        from tgfarm.db.models import CrosspostLog, PipelineRun
        with session_scope() as session:
            runs = session.execute(
                select(PipelineRun).where(PipelineRun.status == "error").order_by(PipelineRun.created_at.desc()).limit(20)
            ).scalars().all()
            for r in runs:
                if r.error:
                    _upsert(channel_id, "mistake", "pipeline:%s" % r.stage,
                            {"stage": r.stage, "error": r.error[:300]}, 0.0, 1, 0.2,
                            "stage %s failed: %s" % (r.stage, r.error[:120]))
            logs = session.execute(
                select(CrosspostLog).where(CrosspostLog.status == "failed").order_by(CrosspostLog.created_at.desc()).limit(20)
            ).scalars().all()
            for lg in logs:
                if lg.error:
                    _upsert(channel_id, "mistake", "crosspost:%s" % lg.platform,
                            {"platform": lg.platform, "error": lg.error[:300]}, 0.0, 1, 0.2,
                            "crosspost %s failed: %s" % (lg.platform, lg.error[:120]))
    except Exception as exc:
        log.debug("mistake collection failed: %s", exc)

    return {"lessons": learned}


async def run():
    total = 0
    for cid in _channel_ids():
        try:
            res = await to_thread(_learn_channel, cid)
            total += int(res.get("lessons", 0))
        except Exception as exc:
            log.warning("s12: learning failed for %s: %s", cid, exc)
    log.info("s12: %d lessons updated across %d channels", total, len(_channel_ids()))
    return {"lessons": total, "channels": len(_channel_ids())}


__all__ = [
    "run",
    "lessons",
    "best_angle_weights",
    "best_hook_style_weights",
    "best_hours",
    "mistakes",
    "best_format_lesson",
    "format_lessons",
    "format_state",
    "learn_format_lessons",
]

