"""S7 — scheduling and publishing.

Telegram publishing uses the Bot API only. An MTProto userbot is not needed for
posting, is rate-limited far harder, and is the single fastest way to get an
account banned — so the farm never uses one. Dzen is a primary platform: its
articles are published through the web editor in a logged-in Chrome profile,
not crossposted from Telegram and not through a public API.

Slots are sampled from the measured hour/day distribution in config/slots.yaml
rather than spread evenly, so the fleet lands on the hours the platform is busy.
A per-hour fleet ceiling stops all channels firing in the same minute, which is
the clearest tell of an automated network.
"""

from __future__ import annotations

import asyncio
import random
from contextlib import suppress
from email.utils import parsedate_to_datetime
import uuid
from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path
from typing import Any, get_args

from sqlalchemy import func, select, update

from tgfarm.config import get_config
from tgfarm.core.errors import PublishError
from tgfarm.core.schemas import PostStatus
from tgfarm.core.logging import get_logger
from tgfarm.core.ratelimit import RATE
from tgfarm.core.run import StageTimer, to_thread
from tgfarm.core.security import redact_sensitive_text
from tgfarm.core.settings import get_settings
from tgfarm.core.text import truncate
from tgfarm.crosspost_journal import (
    JOURNAL_STATUS_DRY_RUN,
    JOURNAL_STATUS_PERMANENT,
    JOURNAL_STATUS_POSTED,
    JOURNAL_STATUS_SKIPPED,
    JOURNAL_STATUS_UNKNOWN,
)
from tgfarm.db.models import Channel as ChannelRow
from tgfarm.db.models import CrosspostLog as CrosspostLogRow
from tgfarm.db.models import Post as PostRow
from tgfarm.db.session import session_scope
from tgfarm.tg_sync import real_tg_chat_id

log = get_logger(__name__)

TELEGRAM_TEXT_LIMIT = 4096
DZEN_TEXT_LIMIT = 10_000
# VK wall posts are capped around 16k characters; keep a safety margin.
VK_TEXT_LIMIT = 15_000
CAPTION_LIMIT = 1024
MIN_SCHEDULE_LEAD_MIN = 8
PLATFORM_TEXT_LIMITS = {
    "telegram": TELEGRAM_TEXT_LIMIT,
    "dzen": DZEN_TEXT_LIMIT,
    "vk": VK_TEXT_LIMIT,
}
# A post left in `publishing` longer than this was interrupted (crash,
# timeout, kill). Its outcome becomes `publish_unknown` until reconciled.
STUCK_PUBLISHING_MIN = 15


# ---------------------------------------------------------- status vocabulary
#: The canonical persisted value of `posts.status`. This is not a copy of a prose
#: list: it is read out of `core.schemas.PostStatus`, the one `Literal` the domain
#: contracts already declare, so the publish path cannot grow a second spelling of
#: a state the panel and the backlog card count.
POST_STATUSES: frozenset[str] = frozenset(get_args(PostStatus))

STATUS_DRAFT = "draft"
STATUS_GATED = "gated"
STATUS_SCHEDULED = "scheduled"
STATUS_PUBLISHING = "publishing"
#: "we sent it and cannot tell whether the platform took it". 51 live rows depend
#: on this exact spelling. `JOURNAL_STATUS_UNKNOWN` is the *crosspost journal's*
#: word for the same fact (`CrosspostResult.status`, a different table, counted by
#: `tgfarm.distribution.build_distribution_plan`) and has never been a valid
#: `posts.status`. The two vocabularies are named here rather than reconciled:
#: unifying them is a data migration, not a rename.
STATUS_PUBLISH_UNKNOWN = "publish_unknown"
STATUS_PUBLISHED = "published"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"

#: Statuses the crosspost journal uses for the same facts. Present so the
#: distinction is checkable rather than remembered: none of these may be written
#: to `posts.status`. Every token is named through the owner's
#: `JOURNAL_STATUS_*` constants, so this set cannot paraphrase the vocabulary it
#: exists to keep apart.
CROSSPOST_ONLY_STATUSES: frozenset[str] = frozenset(
    {
        JOURNAL_STATUS_POSTED,
        JOURNAL_STATUS_SKIPPED,
        JOURNAL_STATUS_PERMANENT,
        JOURNAL_STATUS_UNKNOWN,
        JOURNAL_STATUS_DRY_RUN,
    }
)


#: States in which a row is still eligible to be handed to a publisher. A send
#: this worker could not record must not leave the row in one of them, or the next
#: tick sends material the platform already has.
SEND_AUTHORIZING_STATUSES: frozenset[str] = frozenset(
    {STATUS_SCHEDULED, STATUS_GATED, STATUS_DRAFT}
)


#: The states a resolved send may land in. `draft`, `gated` and `rejected` are not
#: outcomes of a send — they are what the generator, the gate and the rejection
#: path say — so a publisher that named one of them would be writing a verdict it
#: never observed, even while holding a legitimate claim.
PUBLISH_OUTCOME_TARGETS: frozenset[str] = frozenset(
    {STATUS_PUBLISHED, STATUS_PUBLISH_UNKNOWN, STATUS_FAILED, STATUS_SCHEDULED}
)


def canonical_post_status(value: str) -> str:
    """Return `value` when it is a persistable `posts.status`, else refuse loudly.

    A misspelled state is not cosmetic. `tgfarm/api.py` counts the waiting set as
    `publish_unknown`; `distribution.py` counts `unknown` on the journal. A row
    written with the wrong vocabulary's word is read by neither, and the operator
    looking for the 51 uncertain posts stops finding them.
    """
    if value not in POST_STATUSES:
        raise PublishError(
            f"{value!r} is not a canonical posts.status; the publish path persists only "
            f"{sorted(POST_STATUSES)}"
        )
    return value


def publisher_owns(status: str | None) -> bool:
    """True only while the row still says `publishing`.

    `_claim_post` earns `publishing` with a conditional UPDATE, which is the
    publish path's whole claim on a row. From the claim until it records one
    outcome, that is the only state the row may hold. If it says anything else
    when the outcome arrives, someone else has already described this attempt —
    the heartbeat watchdog moved it to `publish_unknown`, the operator reconciled
    it, an earlier send published it — and a second, unpreconditioned write does
    not record that attempt, it invents one.
    """
    return status == STATUS_PUBLISHING


def publish_outcome_allowed(current: str | None, target: str) -> bool:
    """Every outcome write of `posts.status` in this module passes through here.

    Two independent refusals, both about fabrication: the target must be a state a
    send can actually resolve to, and the row must still belong to this worker —
    `scheduled` may be claimed, `publishing` may be resolved, and nothing that
    already carries a verdict may be overwritten by a second one.
    """
    return target in PUBLISH_OUTCOME_TARGETS and publisher_owns(current)


# ------------------------------------------------------------------ scheduling
def plan_slot(
    now: datetime | None = None,
    rng: random.Random | None = None,
    channel_id: str | None = None,
) -> datetime:
    """Sample the next publish time from the measured distribution.

    Walks forward day by day until it finds an hour/day pair with non-zero
    weight, then applies jitter. Weights are used as relative probabilities, so
    the scale of the numbers in slots.yaml does not matter.
    """
    rng = rng or random.Random()
    schedule = get_config().schedule
    now = now or datetime.now(UTC)

    hours = schedule.hours()
    weights = [schedule.hour_probability(h) for h in hours]
    if channel_id:
        try:
            from tgfarm.s12_learning import best_hours
            learned = set(best_hours(channel_id) or [])
        except Exception:
            learned = set()
        if learned:
            weights = [
                (w if w <= 0 else w * (2.0 if h in learned else 1.0))
                for h, w in zip(hours, weights)
            ]
    if not hours or sum(weights) <= 0:
        return (now + timedelta(hours=2)).replace(second=0, microsecond=0)

    day_weights = [schedule.day_probability((now + timedelta(days=d)).weekday()) for d in range(9)]

    for offset in range(9):
        day = now + timedelta(days=offset)
        if day_weights[offset] <= 0:
            continue
        hour = rng.choices(hours, weights=weights, k=1)[0]
        jitter = rng.randint(schedule.jitter_min, schedule.jitter_max)
        # jitter must not push the slot out of the chosen hour, or the tail
        # spills into hours the distribution explicitly excludes
        minute = rng.randint(0, max(0, 59 - jitter)) + jitter
        candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now + timedelta(minutes=MIN_SCHEDULE_LEAD_MIN):
            continue
        return candidate

    # Same distribution, tomorrow — never fall back to an arbitrary hour, which
    # would drop the post into the dead zone outside every configured window.
    hour = rng.choices(hours, weights=weights, k=1)[0]
    jitter = rng.randint(schedule.jitter_min, schedule.jitter_max)
    minute = rng.randint(0, max(0, 59 - jitter)) + jitter
    return (now + timedelta(days=1)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; every comparison here is UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _last_slot_for(session, channel_id: str) -> datetime | None:
    """The latest time this channel already owns, published or queued."""
    value = session.execute(
        select(PostRow.scheduled_at)
        .where(
            PostRow.channel_id == channel_id,
            PostRow.status.in_(("scheduled", "publishing", "published")),
        )
        .order_by(PostRow.scheduled_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _as_utc(value)


def _maybe_prime_slot(slot: datetime, rng: random.Random) -> datetime:
    """Send a share of VK posts into the feed's prime hours.

    Interval spacing wins consistency; prime-hour snapping tests reach. The
    split is random per post, so variants compare the strategies honestly.
    """
    settings = get_settings()
    hours = [int(h) for h in (settings.vk_prime_hours or "").split(",") if h.strip().isdigit()]
    if not hours or rng.random() >= max(0.0, min(1.0, settings.vk_prime_share)):
        return slot
    target = min((h for h in hours), key=lambda h: (slot.hour - h) % 24)
    candidate = slot.replace(hour=target, minute=rng.randint(0, 59), second=0, microsecond=0)
    if candidate <= slot:
        candidate += timedelta(days=1)
    return candidate


def _inside_active_hours(slot: datetime, active_hours) -> datetime:
    """Push a slot to the next allowed window. `null` means 24/7."""
    if not active_hours:
        return slot
    try:
        start, end = int(active_hours[0]), int(active_hours[1])
    except (TypeError, ValueError, IndexError):
        return slot
    if start <= slot.hour < end:
        return slot
    target = slot if slot.hour < start else slot + timedelta(days=1)
    return target.replace(hour=start, minute=0, second=0, microsecond=0)


def _interval_slot(
    now: datetime,
    last: datetime | None,
    cadence: dict,
    rng: random.Random,
    posts_per_day: int = 0,
) -> datetime:
    """Next slot = last one + a random gap inside the allowed window.

    Continuous mode: the gap *is* the schedule. `posts_per_day` widens it so
    the channel spreads its quota over 24 hours instead of burning it in the
    first two hours: VK floods at ~50 wall posts a day, Dzen at one post per
    ten minutes. Together the two ceilings land on a post every 5-10 minutes.
    """
    gap_min = max(30, int(cadence.get("min_gap_s", 300) or 300))
    gap_max = max(gap_min, int(cadence.get("max_gap_s", 620) or gap_min))
    if posts_per_day > 0:
        average = 86_400 / posts_per_day
        gap_min = max(gap_min, int(average * 0.9))
        gap_max = max(gap_min + 1, int(average * 1.15))
    gap = rng.randint(gap_min, gap_max)
    base = now if last is None or last < now else last
    return _inside_active_hours(base + timedelta(seconds=gap), cadence.get("active_hours"))


def schedule_ready_posts(
    limit: int = 200,
    blocked: dict[str, int] | None = None,
) -> int:
    """Assign a slot to every quality-passed post that has none.

    Two modes come from config/slots.yaml: `interval` holds a 5-10 minute gap
    between posts of one channel, `weights` samples the peak-hour table.

    A post the editorial plan booked — `plan_slot_at`, written once by `persist_draft`
    — is scheduled at that instant instead: the cadence computes nothing for it, and
    a booking outside the channel's legal local hours is corrected to the first
    instant the channel may publish in, journalled under
    `GATE_SLOT_BOOKING_REBOUNDED`, while `plan_slot_at` keeps the refused minute.
    `scheduled_at` may be overwritten afterwards by a requeue or a retry;
    `plan_slot_at` is not, which is why the booking and not the queue position is
    what the plan can be audited against. The correction runs only behind
    `Settings.plan_execution_enabled`: with the plan layer off this is the base
    refusal, byte for byte, journal included.

    `blocked` is an optional out-parameter naming the gates that returned a post
    to the queue unscheduled. `scheduled 0` is legitimate most of the time, and
    the operator can only tell that apart from a stall when the zero arrives with
    its reason.
    """
    # Local import: `distribution` pulls in `platforms`/`s9_growth` lazily for its
    # own sake, so a module-level import here would close that cycle on s7.
    from tgfarm.distribution import clamp_channel_daily_cap

    # Local import for the same reason, in the other direction: `plan_execution`
    # imports this module at its own top level, so s7 may only reach back from
    # inside a call. It needs three things from there — the legal local hours and
    # the walk to the next one, both owned by that module, and the journal writer
    # that records what this pass corrected. The refusal it produces is counted
    # under this module's own gate code, kept byte-identical to
    # `plan_execution.REASON_LOCAL_HOURS` by a pin in the gate matrix, and the
    # correction likewise against `plan_execution.REASON_SLOT_REBOUND`.
    from tgfarm.plan_execution import (
        local_hours_allow,
        rebound_for_illegal_booking,
        record_slot_rebinds,
    )

    config = get_config()
    settings = get_settings()
    now = datetime.now(UTC)
    scheduled = 0
    # The switch that gates the plan layer's writes also gates this transition,
    # because the transition is the plan layer's judgement of a plan's booking:
    # with the flag off, a booked minute outside the window is refused exactly as
    # it was before W22-C and nothing is journalled.
    plan_layer_enabled = bool(getattr(settings, "plan_execution_enabled", False))
    rebinds: list[dict[str, object]] = []

    def _blocked(reason: str, count: int = 1) -> None:
        if blocked is not None:
            blocked[reason] = blocked.get(reason, 0) + count

    if settings.approval_required:
        # Human-in-the-loop mode: posts stay `gated` until approved from the
        # panel. The pipeline keeps generating; only the automatic hand to the
        # platform is held back.
        with session_scope() as session:
            waiting = len(
                session.execute(
                    select(PostRow.public_id).where(PostRow.status == "gated")
                ).scalars().all()
            )
        _blocked(GATE_APPROVAL_REQUIRED, waiting)
        log.info("s7: approval mode — %d posts wait for a human", waiting)
        return 0
    cadence = config.schedule.cadence or {}
    interval_mode = str(cadence.get("mode") or "weights").lower() == "interval"
    rng = random.Random()

    with session_scope() as session:
        posts = session.execute(
            select(PostRow).where(
                PostRow.status == "gated",
                PostRow.scheduled_at.is_(None),
            ).order_by(PostRow.created_at.asc()).limit(limit)
        ).scalars().all()

        per_hour: dict[datetime, int] = {}
        capacity = max(1, schedule_capacity())
        cursor: dict[str, datetime] = {}
        platform_daily_counts: dict[str, int] = {}

        for post in posts:
            persona = config.persona(post.channel_id)
            if persona is None or not persona.enabled:
                _blocked(GATE_PERSONA_DISABLED)
                continue

            channel = session.get(ChannelRow, post.channel_id)
            platform = ((channel.platform if channel else None) or "telegram").lower()
            if (
                platform == "dzen"
                and getattr(settings, "dzen_requires_llm", False)
                and str(post.generation_provider or "") == "offline"
            ):
                # An offline Dzen draft can never ship: the publisher holds
                # template text back (DZEN_REQUIRES_LLM). Scheduling it would
                # rot in the queue and be skipped on every tick. It is the same
                # fact the tick reports, so it names itself the same way — this
                # was the one gate in S7 that dropped a post silently.
                _blocked(GATE_DZEN_NEEDS_ONLINE_MODEL)
                continue
            if platform in PRIMARY_PLATFORM_DAILY_CAPS:
                if platform not in platform_daily_counts:
                    platform_daily_counts[platform] = _posts_platform_today(
                        session, platform, now
                    )
                today_count = platform_daily_counts[platform]
                daily_cap = clamp_channel_daily_cap(PRIMARY_PLATFORM_DAILY_CAPS[platform])
            else:
                today_count = _posts_today(session, post.channel_id, now)
                daily_cap = clamp_channel_daily_cap(max(1, int(persona.posts_per_day)))
            if today_count >= daily_cap:
                _blocked(GATE_DAILY_CAP)
                continue

            booked = _as_utc(getattr(post, "plan_slot_at", None))
            if booked is not None and platform != "dzen":
                # The plan already answered "when"; the cadence does not get to
                # re-decide it. `plan_slot_at` is written once at insert and never
                # touched again, so a booking is the only durable statement of what
                # was ordered — honouring anything else would make it a note nobody
                # reads. An overdue booking keeps its own instant, which makes the
                # post due on the next tick rather than re-slotting the plan.
                #
                # The one limit that outranks a booking is the channel's legal local
                # hours, and since W22-C it corrects instead of parking: quietly
                # re-slotting *the booking* is how a plan becomes unsound, so the
                # refused minute stays in `plan_slot_at` and the corrected one goes
                # to `scheduled_at`, the column this queue owns and that requeue and
                # retry already move. The window and the walk are both read through
                # `plan_execution`, which owns them, so the executor and this
                # scheduler cannot correct one booking to two instants. A Dzen
                # booking is not gated here because Dzen publishes at `now` whatever
                # the plan said: judging an instant this branch never writes would
                # refuse posts over a slot they were not given.
                if not local_hours_allow(booked, getattr(channel, "timezone", None), config=config):
                    corrected = None
                    if plan_layer_enabled and channel is not None:
                        corrected = rebound_for_illegal_booking(
                            session, channel, booked, config=config
                        )
                    if corrected is None:
                        # Nothing to correct to — no legal free minute in the
                        # horizon, or the plan layer is off. The base refusal is
                        # the honest answer, and it is still counted in full.
                        _blocked(GATE_SLOT_OUTSIDE_LOCAL_HOURS)
                        continue
                    _blocked(GATE_SLOT_BOOKING_REBOUNDED)
                    rebinds.append(
                        {
                            "post_public_id": str(post.public_id),
                            "cluster_public_id": str(post.cluster_public_id or ""),
                            "channel_id": str(channel.id),
                            "refused_slot_at": booked.isoformat(),
                            "corrected_slot_at": corrected.isoformat(),
                        }
                    )
                    slot = corrected
                else:
                    slot = booked
            elif interval_mode:
                last = cursor.get(post.channel_id) or _last_slot_for(session, post.channel_id)
                slot = _interval_slot(now, last, cadence, rng, persona.posts_per_day)
                if platform == "vk":
                    slot = _maybe_prime_slot(slot, rng)
                cursor[post.channel_id] = slot
            else:
                slot = plan_slot(now, channel_id=post.channel_id)
                for _ in range(8):
                    bucket = slot.replace(minute=0, second=0, microsecond=0)
                    if per_hour.get(bucket, 0) < capacity:
                        break
                    slot = plan_slot(slot + timedelta(minutes=61), channel_id=post.channel_id)
                per_hour[bucket] = per_hour.get(bucket, 0) + 1

            if platform == "dzen":
                # The Dzen editor is a browser, not an API: posts queue up and
                # publish_due drains them one per DZEN_MIN_GAP_S.
                post.scheduled_at = now
            else:
                post.scheduled_at = slot
            post.status = canonical_post_status(STATUS_SCHEDULED)
            if platform in PRIMARY_PLATFORM_DAILY_CAPS:
                platform_daily_counts[platform] = today_count + 1
            scheduled += 1

    # Outside the session on purpose: the journal is a fact about the pass, not
    # part of the write the pass makes, and `pipeline._record` swallows its own
    # failures — a correction this function could not write down is a missing
    # metric, never a post left unscheduled. One row per pass that rebound
    # anything, so five ticks that repaired one booking name it once.
    record_slot_rebinds(rebinds, source="publish_tick")

    log.info("s7: scheduled %d posts (mode=%s)", scheduled, "interval" if interval_mode else "weights")
    return scheduled


def schedule_capacity() -> int:
    return max(1, get_config().schedule.fleet_posts_per_hour)


PRIMARY_PLATFORM_DAILY_CAPS = {
    "dzen": 2,
    "vk": 3,
}


def _day_bucket_expression():
    return func.coalesce(PostRow.published_at, PostRow.scheduled_at, PostRow.created_at)


def _posts_today(session, channel_id: str, now: datetime) -> int:
    midnight = datetime.combine(now.date(), datetime.min.time()).replace(tzinfo=UTC)
    return int(
        session.execute(
            select(func.count()).select_from(PostRow).where(
                PostRow.channel_id == channel_id,
                PostRow.status.in_(("scheduled", "publishing", "published")),
                _day_bucket_expression() >= midnight,
            )
        ).scalar_one()
    )


def _posts_platform_today(session, platform: str, now: datetime) -> int:
    """One global primary-channel budget per platform, not one per persona."""
    midnight = datetime.combine(now.date(), datetime.min.time()).replace(tzinfo=UTC)
    return int(
        session.execute(
            select(func.count())
            .select_from(PostRow)
            .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
            .where(
                ChannelRow.platform == platform,
                ChannelRow.enabled.is_(True),
                PostRow.status.in_(("scheduled", "publishing", "published")),
                _day_bucket_expression() >= midnight,
            )
        ).scalar_one()
    )


# ------------------------------------------------------------------- assembly
def assemble_text(post: PostRow, platform: str = "telegram") -> str:
    """Re-render the stored fields into the final message for a platform.

    Post body was capped at the gate; the sources block and hashtags are added
    here so their length does not distort the quality measurement. Dzen carries
    the hook as its article title, so the body copy must not repeat it.
    """
    parts = [] if platform == "dzen" else [post.hook.strip()]
    parts.append(post.body.strip())
    if post.sources:
        lines = ["Источники:"]
        for source in post.sources[:3]:
            lines.append(f"— {source.get('title', '').strip()}")
            lines.append(f"  {source.get('url', '').strip()}")
        parts.append("\n".join(lines))
    if post.cta:
        parts.append(post.cta.strip())
    if post.hashtags:
        parts.append(" ".join(f"#{str(h).lstrip('#')}" for h in post.hashtags[:6]))
    limit = PLATFORM_TEXT_LIMITS.get(platform, TELEGRAM_TEXT_LIMIT)
    return truncate("\n\n".join(p for p in parts if p), limit)


# ------------------------------------------------------------------ publishing
class TelegramPublisher:
    def __init__(self) -> None:
        self._bot = None
        self._settings = get_settings()

    async def _ensure_bot(self):
        if self._bot is not None:
            return self._bot
        if not self._settings.telegram_bot_token:
            raise PublishError("TELEGRAM_BOT_TOKEN not set")
        from aiogram import Bot

        self._bot = Bot(token=self._settings.telegram_bot_token)
        return self._bot

    async def send_video(self, chat_id: int, text: str, video_path: str) -> int:
        """Video first, caption on the message, remainder as a follow-up.

        Bot API caps uploads at 50 MB and captions at 1024 chars, so the text is
        split rather than silently truncated.
        """
        from aiogram.types import FSInputFile

        await RATE.bot_api.take()
        bot = await self._ensure_bot()
        path = Path(video_path)
        size_mb = path.stat().st_size / 1e6 if path.exists() else 0.0
        if size_mb > 50:
            raise PublishError(f"video {size_mb:.1f} MB exceeds the Bot API 50 MB limit")

        video = FSInputFile(str(path), filename=path.name)
        message = await bot.send_video(
            chat_id=chat_id,
            video=video,
            caption=truncate(text, CAPTION_LIMIT),
            supports_streaming=True,
        )
        if len(text) > CAPTION_LIMIT:
            await bot.send_message(chat_id=chat_id, text=text[CAPTION_LIMIT:])
        await bot.session.close()
        return message.message_id

    async def send(self, chat_id: int, text: str, media_path: str | None) -> int:
        from aiogram.types import FSInputFile

        await RATE.bot_api.take()
        bot = await self._ensure_bot()

        if media_path and Path(media_path).exists():
            suffix = Path(media_path).suffix.lower()
            if suffix in {".mp4", ".m4v", ".webm", ".mov", ".mkv"}:
                return await self.send_video(chat_id, text, media_path)

            photo = FSInputFile(media_path, filename=Path(media_path).name)
            caption = truncate(text, CAPTION_LIMIT)
            message = await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption)
            if len(text) > CAPTION_LIMIT:
                await bot.send_message(chat_id=chat_id, text=text[CAPTION_LIMIT:])
        else:
            message = await bot.send_message(chat_id=chat_id, text=text)

        await bot.session.close()
        return message.message_id

    async def health(self) -> dict:
        settings = get_settings()
        if not settings.telegram_bot_token:
            return {"configured": False}
        try:
            bot = await self._ensure_bot()
            me = await bot.get_me()
            return {
                "configured": True,
                "username": me.username,
                "can_post": not bool(me.can_join_groups is False and settings.channel_ids),
                "channels": settings.channel_ids,
            }
        except Exception as exc:  # noqa: BLE001 - health must not raise
            return {"configured": True, "error": str(exc)[:200]}


PUBLISHER = TelegramPublisher()


def _llm_recently_working(minutes: int = 90) -> bool:
    """Compatibility wrapper: draft provenance is the honest LLM health signal."""
    from tgfarm.core.llm_health import llm_recently_working

    return llm_recently_working(minutes)


def _primary_vk_image(post_id: int) -> str | None:
    """Cover on disk for the VK post; videos stay Telegram-only for now."""
    from tgfarm.db.models import Media as MediaRow

    with session_scope() as session:
        row = session.execute(
            select(MediaRow)
            .where(MediaRow.post_id == post_id, MediaRow.is_primary.is_(True))
            .order_by((MediaRow.kind == "video"), MediaRow.id)
        ).scalars().first()
        if row and row.path and Path(row.path).exists():
            return row.path
    return None


def _vk_has_own_channel() -> bool:
    """True when a VK persona publishes its own posts.

    The Dzen->VK mirror exists for a farm that only has a Dzen channel. With a
    dedicated VK channel it would double-post every story, so the mirror is
    skipped instead of guessing.
    """
    return any(p.platform == "vk" and p.enabled for p in get_config().personas)


async def _publish_vk(post_id: int, text: str) -> dict:
    """Publish one post to the VK community wall via the official API."""
    from tgfarm.core.schemas import CrosspostPayload, CrosspostResult
    from tgfarm.s9_growth.adapters import VkAdapter, log_result

    with session_scope() as session:
        post = session.get(PostRow, post_id)
        if post is None:
            raise PublishError("post disappeared")
        public_id = post.public_id
        title = (post.hook or "").strip()

    settings = get_settings()
    if not settings.live:
        result = CrosspostResult(platform="vk", status="dry_run", remote_id=f"dry-vk-{post_id}")
        log_result(public_id, result)
        log.info("[dry-run] -> vk | %s", truncate(title, 90))
        return {"ok": True, "message_id": None, "remote_id": result.remote_id}

    adapter = VkAdapter()
    if not adapter.configured():
        raise PublishError("VK_ACCESS_TOKEN / VK_GROUP_ID not set")

    # `text` is already assembled by assemble_text(post, "vk"): hook, body,
    # sources and hashtags. Re-building a payload from the post would duplicate
    # the sources block.
    image_path = await to_thread(_primary_vk_image, post_id)
    payload = CrosspostPayload(
        platform="vk",
        title=title,
        text=text,
        meta={"image_path": image_path} if image_path else {},
    )
    result = await adapter.publish(payload)
    await to_thread(log_result, public_id, result)
    if result.status != JOURNAL_STATUS_POSTED:
        raise PublishError(f"vk: {result.error or result.status}")
    log.info("vk published: %s", result.url or result.remote_id)
    return {"ok": True, "message_id": None, "remote_id": result.remote_id}


async def _publish_dzen(
    post_id: int,
    text: str,
    cover_path: str | None = None,
    inline_image: str | None = None,
    inline_caption: str | None = None,
    inline_images: list[tuple[str, str]] | None = None,
) -> dict:
    """Publish one article through the Dzen web editor.

    Dzen has no public publishing API, so this is browser automation in the
    operator's logged-in profile, not an HTTP adapter.
    """
    from tgfarm.core.schemas import CrosspostResult
    from tgfarm.dzen_web import DzenWebPublisher
    from tgfarm.s9_growth.adapters import log_result

    with session_scope() as session:
        post = session.get(PostRow, post_id)
        if post is None:
            raise PublishError("post disappeared")
        public_id = post.public_id
        title = (post.hook or "").strip()

    settings = get_settings()
    if not settings.live:
        result = CrosspostResult(
            platform="dzen",
            status="dry_run",
            remote_id=f"dry-{post_id}",
        )
        log_result(public_id, result)
        log.info(
            "[dry-run] -> dzen | %s | inline: %s",
            truncate(title, 90),
            inline_images if inline_images is not None else (inline_image or "none"),
        )
        return {"ok": True, "message_id": None, "remote_id": result.remote_id}

    if inline_images is None:
        inline_images = [(inline_image, inline_caption)] if inline_image else []
    inline_images = [
        (str(path), caption)
        for path, caption in (inline_images or [])
        if path
    ]

    # Every Dzen publication must carry at least one image (lead photo or cover).
    if not (inline_images or cover_path):
        raise PublishError("dzen: publication without image is forbidden")

    published = await DzenWebPublisher().publish(
        title, text, cover_path, inline_images=inline_images,
        inline_image=inline_images[0][0] if inline_images else None,
        inline_caption=inline_images[0][1] if inline_images else None,
    )
    from tgfarm.dzen_web import canonical_publication_url, publication_ref

    # The identifier is only worth anything if it reaches the journal. A page that
    # named a URL but not in the shape the publisher parses still carries the id,
    # and an id without a URL is still exact, so both columns are completed from
    # whichever one exists before the row is written.
    remote_id = published.remote_id or publication_ref(published.url)
    url = canonical_publication_url(remote_id) or published.url
    result = CrosspostResult(
        platform="dzen",
        status=JOURNAL_STATUS_POSTED,
        remote_id=remote_id,
        url=url,
    )
    log_result(public_id, result)
    if remote_id:
        log.info("dzen published: %s", url or remote_id)
    else:
        # Not a failure: the article is live. It is the loss of the attribution
        # key, which leaves S8 to match this post by headline alone.
        log.warning(
            "dzen published %s without an article id; S8 must attribute it by headline",
            public_id,
        )

    # Mirror a Dzen post to VK only when VK has no channel of its own.
    if _vk_has_own_channel():
        return {"ok": True, "message_id": None, "remote_id": result.remote_id}

    try:
        from tgfarm.core.schemas import CrosspostResult as _CR
        from tgfarm.s9_growth import _load_post
        from tgfarm.s9_growth.adapters import VkAdapter, build_payload
        from tgfarm.s9_growth.adapters import log_result as _log_result

        vk_adapter = VkAdapter()
        if vk_adapter.configured():
            post_obj = await to_thread(_load_post, post_id)
            if post_obj is not None:
                payload = build_payload(post_obj)
                payload.platform = "vk"
                if settings.live:
                    vk_result = await vk_adapter.publish(payload)
                else:
                    vk_result = _CR(platform="vk", status="dry_run", remote_id=f"dry-vk-{post_id}")
                await to_thread(_log_result, public_id, vk_result)
                log.info("vk mirror from dzen: %s", vk_result.status)
    except Exception as exc:  # noqa: BLE001
        log.warning("vk mirror from dzen failed: %s", redact_sensitive_text(exc, limit=180))

    return {"ok": True, "message_id": None, "remote_id": result.remote_id}


def _claim_post(post_id: int) -> bool:
    """Take a post out of the queue atomically.

    The in-process publisher serializes batches, but this database claim is
    still required as a final guard against a second process or direct caller.
    Only one worker can move a given `scheduled` row to `publishing`.
    """
    with session_scope() as session:
        result = session.execute(
            update(PostRow)
            .where(PostRow.id == post_id, PostRow.status == "scheduled")
            .values(
                status=canonical_post_status(STATUS_PUBLISHING),
                attempts=PostRow.attempts + 1,
                # the claim timestamp is when publishing *starts*: the stuck
                # detector measures from here, not from queue time, or a slow
                # browser publish gets murdered mid-flight
                scheduled_at=datetime.now(UTC),
                publish_heartbeat_at=datetime.now(UTC),
            )
        )
        return result.rowcount == 1


def _is_ambiguous_publish_error(exc: BaseException) -> bool:
    """Return True when the remote platform may have accepted the post.

    A timeout/reset after a request was sent is not proof of failure. Retrying
    such a request blindly can create duplicates. Connection/DNS failures that
    happen before an HTTP connection is established remain retryable.
    """
    kind = type(exc).__name__.lower()
    message = f"{kind}: {exc}".lower()
    if isinstance(exc, PublishImageRequired):
        # The publisher stopped before opening a browser: nothing was submitted,
        # so this is a definite failure, not an unknown outcome.
        return False
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        if status_code >= 500 or status_code in (408, 425):
            # The server may have committed the post before returning an error.
            return True
        if 400 <= status_code < 500:
            # Explicit client errors, including rate limits, mean the request was
            # rejected; retry policy can handle them without duplicate risk.
            return False

    # These Dzen errors are detected before submitting the article.
    if kind in {"dzencaptcha", "dzennotloggedin"}:
        return False

    definitely_before_send = (
        "connecttimeout",
        "connecterror",
        "getaddrinfo",
        "name or service not known",
        "connection refused",
        "network is unreachable",
    )
    if any(marker in message for marker in definitely_before_send):
        return False

    ambiguous = (
        "readtimeout",
        "writetimeout",
        "request timeout",
        "timed out",
        "timeout",
        "connection reset",
        "connection aborted",
        "server disconnected",
        "remotedisconnected",
        "remoteprotocolerror",
        "incomplete read",
        "unexpected eof",
        "broken pipe",
    )
    if any(marker in message for marker in ambiguous):
        return True

    # An unknown exception can be raised after a remote side effect. Without a
    # platform-specific proof that nothing was submitted, manual reconciliation
    # is safer than automatically creating a second copy.
    return True


def _publish_retry_after_seconds(
    exc: BaseException,
    *,
    now: datetime | None = None,
    max_seconds: int = 86_400,
) -> int | None:
    """Read a platform retry-after hint without trusting unbounded values."""
    raw = getattr(exc, "retry_after", None)
    if raw is not None:
        if isinstance(raw, timedelta):
            seconds = ceil(raw.total_seconds())
        else:
            try:
                seconds = ceil(float(raw))
            except (TypeError, ValueError, OverflowError):
                seconds = -1
        if seconds >= 0:
            return min(seconds, max(0, int(max_seconds)))

    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) != 429:
        return None
    headers = getattr(response, "headers", {}) or {}
    raw_header = headers.get("Retry-After") or headers.get("retry-after")
    if raw_header is None:
        return None
    value = str(raw_header).strip()
    try:
        seconds = ceil(float(value))
    except (TypeError, ValueError, OverflowError):
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
        seconds = ceil((retry_at.astimezone(UTC) - current).total_seconds())
    return min(max(0, seconds), max(0, int(max_seconds)))


class PublishImageRequired(RuntimeError):
    """The destination refuses text-only posts and no cover could be produced."""


_COVER_KINDS = frozenset(
    {"image", "ai_art", "template", "infographic", "quote", "cover", "photo", "article_image"}
)


def _ensure_cover_for_post(post_id: int) -> str | None:
    """Return an existing image path, or render the deterministic cover now.

    Dzen rejects text-only articles ("publication without image is forbidden"),
    and retrying such a post only burns attempts: seven posts reached 13 attempts
    with zero media rows. The farm can always render its local template cover, so
    the publisher repairs the gap instead of failing blind.
    """
    from tgfarm.db.models import Media as MediaRow

    with session_scope() as session:
        post = session.get(PostRow, post_id)
        if post is None:
            return None
        public_id = post.public_id
        rows = session.execute(
            select(MediaRow).where(MediaRow.post_id == post_id)
        ).scalars().all()
        for row in rows:
            if row.kind in _COVER_KINDS and row.path and Path(row.path).exists():
                return str(row.path)

    from tgfarm.s5_quality import load_draft
    from tgfarm.s6_visual import persist_media, render_for_post

    loaded = load_draft(public_id)
    if loaded is None:
        return None
    draft, channel_id = loaded
    persona = get_config().persona(channel_id)
    if persona is None:
        return None
    try:
        asset = render_for_post(draft, persona)
    except Exception as exc:  # noqa: BLE001 - a broken template must not kill the tick
        log.error(
            "cover render failed for %s: %s",
            public_id,
            redact_sensitive_text(exc, limit=200),
        )
        return None
    if not asset.path:
        return None
    persist_media(public_id, asset)
    log.info("s7: rendered a missing cover for %s before publishing", public_id)
    return str(asset.path)


def _is_permanent_publish_error(exc: BaseException) -> bool:
    """Explicit client rejections are terminal; rate limits remain retryable."""
    if isinstance(exc, PublishImageRequired):
        # No cover and no way to render one: another attempt would fail the same
        # way, so stop burning the queue on it.
        return True
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if not isinstance(status_code, int):
        return False
    return 400 <= status_code < 500 and status_code not in (408, 425, 429)


def _requeue_stuck(minutes: int = STUCK_PUBLISHING_MIN) -> int:
    """Move interrupted attempts to manual reconciliation, never auto-retry.

    A process can die after the platform accepted a post but before the local
    database committed that fact. The old automatic requeue could publish a
    duplicate. Keep the original attempt timestamp and expose the uncertain
    outcome so an operator can verify the destination before retrying.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=max(1, minutes))
    with session_scope() as session:
        # Existing rows from before the heartbeat migration have NULL heartbeat,
        # so fall back to their original publishing timestamp.
        last_activity = func.coalesce(PostRow.publish_heartbeat_at, PostRow.scheduled_at)
        result = session.execute(
            update(PostRow)
            .where(
                PostRow.status == "publishing",
                last_activity <= cutoff,
            )
            .values(
                status=canonical_post_status(STATUS_PUBLISH_UNKNOWN),
                publish_heartbeat_at=None,
                publish_error=f"outcome unknown after {minutes} min without heartbeat; verify platform before retry",
            )
        )
        return result.rowcount or 0


#: Errors that prove the material never reached the platform's submit step.
#: Playwright reports DNS/connection failures from ``page.goto`` before any
#: button is clicked, so a retry cannot create a duplicate.
PRE_SUBMIT_ERROR_MARKERS = (
    "err_name_not_resolved",
    "err_internet_disconnected",
    "err_connection_refused",
    "err_address_unreachable",
    "err_network_access_denied",
    "err_connection_timed_out",
    "getaddrinfo",
    "name or service not known",
    "network is unreachable",
    "connection refused",
)

#: Errors that may have happened *after* the remote side accepted the material
#: (or that destroyed the driver mid-flight). They must never be auto-retried.
UNCERTAIN_DRIVER_MARKERS = (
    "connection closed while reading from the driver",
    "target page, context or browser has been closed",
    "target closed",
    "browser has been closed",
)


def _pre_submit_error(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in PRE_SUBMIT_ERROR_MARKERS)


def _uncertain_driver_error(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in UNCERTAIN_DRIVER_MARKERS)


def requeue_unsubmitted(
    *,
    platform: str | None = None,
    limit: int = 100,
    apply: bool = False,
    max_age_hours: float = 48.0,
) -> dict:
    """Reanimate posts that never left the farm; park or expire the rest.

    Three outcomes, each with a reason that survives in ``publish_error`` and in
    the operator log:

    * pre-submit transport failure and the material is still fresh — safe to
      retry, because nothing was submitted and the topic is not stale;
    * pre-submit transport failure but the material is older than
      ``max_age_hours`` — terminal failure ("expired"), never published as fresh
      news days later; the operator can still requeue it explicitly;
    * a driver error with unknown remote outcome — ``publish_unknown``, never
      retried automatically: a human verifies the destination first.

    ``apply=False`` is a dry run.
    """
    from tgfarm.db.models import Channel as ChannelRow
    from tgfarm.db.models import OperatorLog

    target_platform = str(platform or "").strip().lower()
    now = datetime.now(UTC)
    candidates: list[tuple[int, str, str, str]] = []
    with session_scope() as session:
        stmt = (
            select(PostRow, ChannelRow.platform)
            .join(ChannelRow, ChannelRow.id == PostRow.channel_id, isouter=True)
            .where(
                PostRow.status.in_(("scheduled", "failed")),
                PostRow.publish_error.isnot(None),
                PostRow.published_at.is_(None),
            )
            .order_by(PostRow.attempts.desc(), PostRow.id.asc())
            .limit(max(1, int(limit)))
        )
        for post, row_platform in session.execute(stmt).all():
            name = str(row_platform or "").lower()
            if target_platform and name != target_platform:
                continue
            error = str(post.publish_error or "")
            created = post.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age_hours = (now - created).total_seconds() / 3600 if created else 0.0
            if _pre_submit_error(error):
                action = "requeue" if age_hours <= max(0.0, max_age_hours) else "expire"
                candidates.append((post.id, post.public_id, action, error[:240]))
            elif _uncertain_driver_error(error):
                candidates.append((post.id, post.public_id, "unknown", error[:240]))

    requeued = [row[1] for row in candidates if row[2] == "requeue"]
    parked = [row[1] for row in candidates if row[2] == "unknown"]
    expired = [row[1] for row in candidates if row[2] == "expire"]
    summary = {
        "apply": bool(apply),
        "requeue": requeued,
        "park_as_unknown": parked,
        "expired": expired,
        "max_age_hours": float(max_age_hours),
        "changed": 0,
    }
    if not apply or not candidates:
        return summary

    changed = 0
    with session_scope() as session:
        for post_id, public_id, action, error in candidates:
            post = session.get(PostRow, post_id)
            if post is None or post.status not in ("scheduled", "failed") or post.published_at is not None:
                continue
            if action == "requeue":
                post.status = canonical_post_status(STATUS_SCHEDULED)
                post.attempts = 0
                post.scheduled_at = now
                post.publish_error = (
                    "requeued after pre-submit transport failure (nothing was submitted): " + error
                )[:400]
                log_action = "requeue-unsubmitted"
            elif action == "expire":
                post.status = canonical_post_status(STATUS_FAILED)
                post.scheduled_at = None
                post.publish_error = (
                    "expired: never submitted and older than "
                    f"{max_age_hours:g} h after a transport outage; content is stale. "
                    "Requeue manually if it is still relevant. Last error: " + error
                )[:400]
                log_action = "expire-stale-unsubmitted"
            else:
                # The candidate token that reaches this branch is spelled
                # `unknown` — the crosspost journal's word for the same fact. The
                # persisted value is this module's constant, never that token:
                # writing the action label itself would put a status in
                # `posts.status` that neither the backlog card nor the
                # reconciliation board counts, on the 51 rows that need one.
                post.status = canonical_post_status(STATUS_PUBLISH_UNKNOWN)
                post.scheduled_at = None
                post.publish_error = (
                    "outcome unknown: publisher driver died; verify the platform before retry. "
                    "Last error: " + error
                )[:400]
                log_action = "park-publish-unknown"
            session.add(
                OperatorLog(
                    action=log_action,
                    target=public_id[:64],
                    detail=error,
                )
            )
            changed += 1
    log.warning(
        "requeue_unsubmitted: %d requeued, %d expired as stale, %d parked as unknown",
        len(requeued),
        len(expired),
        len(parked),
    )
    summary["changed"] = changed
    return summary


def _touch_publish_heartbeat(post_id: int) -> bool:
    """Refresh liveness only while this worker still owns the publishing state."""
    with session_scope() as session:
        result = session.execute(
            update(PostRow)
            .where(PostRow.id == post_id, PostRow.status == "publishing")
            .values(publish_heartbeat_at=datetime.now(UTC))
        )
        return result.rowcount == 1


async def _publish_heartbeat_loop(post_id: int, interval_s: float = 30.0) -> None:
    """Keep long browser/API publishes alive without masking an abandoned worker."""
    while True:
        await asyncio.sleep(max(1.0, interval_s))
        try:
            still_publishing = await to_thread(_touch_publish_heartbeat, post_id)
        except Exception as exc:  # noqa: BLE001 - telemetry must not cancel a live publish
            log.warning("publish heartbeat update failed for post %s (%s)", post_id, type(exc).__name__)
            continue
        if not still_publishing:
            return


async def _stop_publish_heartbeat(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


class PublishTimeout(RuntimeError):
    """One publication exceeded its own budget; the batch keeps going.

    This is deliberately a per-post deadline, not a tick-wide one: a hung browser
    or a stalled HTTP request must not consume the whole publication window and
    starve every other post.
    """


#: Conservative per-platform budgets. Dzen is a browser flow and needs the
#: long deadline; API publishers should answer in seconds.
DEFAULT_PUBLISH_TIMEOUT_S = {"telegram": 60.0, "vk": 120.0, "dzen": 600.0}
MIN_PUBLISH_TIMEOUT_S = 10.0
DEFAULT_PUBLISH_CANCEL_GRACE_S = 15.0


def publish_timeout_seconds(platform: str, settings=None) -> float:
    """Per-publication budget in seconds, overridable through settings."""
    key = str(platform or "").lower()
    if settings is None:
        settings = get_settings()
    raw = getattr(settings, f"{key}_publish_timeout_s", None)
    if raw is None:
        raw = DEFAULT_PUBLISH_TIMEOUT_S.get(key, 120.0)
    try:
        return max(MIN_PUBLISH_TIMEOUT_S, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_PUBLISH_TIMEOUT_S.get(key, 120.0)


def publish_cancel_grace_seconds(settings=None) -> float:
    if settings is None:
        settings = get_settings()
    try:
        return max(0.0, float(getattr(settings, "publish_cancel_grace_s", DEFAULT_PUBLISH_CANCEL_GRACE_S)))
    except (TypeError, ValueError):
        return DEFAULT_PUBLISH_CANCEL_GRACE_S


def _discard_task_result(task: "asyncio.Task") -> None:
    """Consume an abandoned publisher's result so asyncio does not warn."""
    if task.cancelled() or not task.done():
        # ``cancelling`` is not ``done``: asking for the exception of a task that
        # is still unwinding raises InvalidStateError.
        return
    try:
        exc = task.exception()
    except asyncio.CancelledError:  # pragma: no cover - defensive
        return
    if exc is not None:
        log.error(
            "abandoned publish worker failed: %s",
            redact_sensitive_text(exc, limit=300),
        )


async def _await_publish(
    coro,
    *,
    timeout_s: float,
    platform: str,
    post_id: int,
    cancel_grace_s: float | None = None,
):
    """Await one platform call with its own deadline and bounded cancellation.

    ``asyncio.wait_for`` alone is not enough here: a browser driver that swallows
    cancellation would still block the tick. The coroutine is therefore wrapped in
    a task, cancelled at the deadline, given a short grace period, and abandoned
    (with its result consumed) if it refuses to die.
    """
    grace = DEFAULT_PUBLISH_CANCEL_GRACE_S if cancel_grace_s is None else cancel_grace_s
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=timeout_s)
    if task in done:
        return task.result()
    task.cancel()
    done, pending = await asyncio.wait({task}, timeout=grace)
    if pending:
        log.error(
            "publish worker ignored cancellation for %s (%s); abandoning it to keep the tick alive",
            post_id,
            platform,
        )
    _discard_task_result(task)
    raise PublishTimeout(
        f"{platform} publish exceeded {timeout_s:g}s (deadline); outcome is unknown"
    )


def _dry_run_remote_id(platform: str, post_id: int) -> str:
    """The placeholder id the off-mode adapters have always journalled."""
    return f"dry-{post_id}" if platform == "dzen" else f"dry-{platform}-{post_id}"


def has_publication_evidence(
    session,
    *,
    public_id: str,
    platform: str,
    post_id: int,
    message_id: Any,
    remote_id: Any = None,
) -> bool:
    """True when the destination now holds something this row can point at.

    The claim "`published`" is the denominator of every evidence number
    downstream — `format_performance`, `measurement_coverage`, the S12 lessons,
    the panel cards — so it may only be written when at least one of three
    measurable facts holds:

    * `message_id` is not `None`. Telegram's Bot API returns it for every message
      it accepted, and `maintenance.publication_evidence_repair` names a
      `published` row with `tg_message_id IS NULL` as the original defect (15 live
      rows on 2026-10-26);
    * the platform returned a real remote id. `dry-*` placeholders are not that:
      they are what the off-mode adapters invent, and `_publish_dzen` /
      `_publish_vk` hand them back with `ok=True`;
    * the crosspost journal holds a `posted` row for this post and platform. That
      is where Dzen and VK leave their identifier — the publishers call
      `log_result()` rather than writing a column on the post — and `maintenance`
      already counts such a row as evidence. Measured on the live base 2026-11-05:
      35 of the 57 `published` Dzen rows have such a journal row and 27 of those
      carry an empty `remote_id`, so treating a missing *id* as a missing
      *publication* would un-publish real work; VK is 403 of 403 and Telegram 0 of
      15, which is the class `publication_evidence_repair` was written for.
    """
    if message_id is not None:
        return True
    text = str(remote_id or "").strip()
    if text and text != _dry_run_remote_id(platform, post_id):
        return True
    return (
        session.execute(
            select(CrosspostLogRow.id)
            .where(
                CrosspostLogRow.post_public_id == public_id,
                CrosspostLogRow.platform == platform,
                CrosspostLogRow.status == JOURNAL_STATUS_POSTED,
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _dry_run_reason(platform: str, settings: Any) -> str | None:
    """Name the outcome when nothing could possibly have reached the platform.

    Two facts mean "we know for certain this was not sent": the farm is not in
    `live` mode, and the Telegram bot token is missing. Each is reported under
    its own code so the journal says which one the operator has to fix.

    This is deliberately *not* `publish_unknown`: that state means "the remote
    side may have it, verify before retrying", and a dry run is its opposite.
    """
    if not getattr(settings, "live", False):
        return GATE_FARM_NOT_LIVE
    if platform == "telegram" and not getattr(settings, "telegram_bot_token", ""):
        return GATE_BOT_TOKEN_MISSING
    return None


def _journal_primary_dry_run(public_id: str, platform: str, post_id: int) -> bool:
    """Record the skipped delivery once, the way the crosspost path does.

    `tgfarm/distribution.py` treats a journalled `dry_run` as "this post was
    already reported as not sent" and only offers it again once the farm goes
    live; a fresh row per tick would grow the journal forever. Dzen and VK always
    journalled their off-mode primary publish (the publisher is shared with the
    crosspost path); Telegram has no crosspost journal, so its dry runs are
    counted in the tick's decision ledger only.
    """
    from tgfarm.core.schemas import CrosspostResult
    from tgfarm.db.models import CrosspostLog
    from tgfarm.s9_growth.adapters import log_result

    if platform not in {"dzen", "vk"}:
        return False
    with session_scope() as session:
        already = session.execute(
            select(CrosspostLog.id)
            .where(
                CrosspostLog.post_public_id == public_id,
                CrosspostLog.platform == platform,
                CrosspostLog.status == JOURNAL_STATUS_DRY_RUN,
            )
            .limit(1)
        ).scalar_one_or_none()
    if already is not None:
        return False
    log_result(
        public_id,
        CrosspostResult(
            platform=platform,
            status=JOURNAL_STATUS_DRY_RUN,
            remote_id=_dry_run_remote_id(platform, post_id),
        ),
    )
    return True


async def _publish_one(
    post_id: int,
    target: int | str,
    text: str,
    media: str | None,
    platform: str = "telegram",
    inline_media: tuple[str, str] | None = None,
) -> dict:
    """Deliver one post, or say precisely why nothing was delivered.

    A publish that did not happen must not be recorded as a publication: the
    dry-run branch below returns before the post is claimed, so `status`,
    `published_at`, `tg_message_id` and `tg_chat_id` keep their previous values
    and no attempt is spent. What the post does afterwards is the queued-post
    behaviour of `GATE_PLATFORM_DISABLED` — it stays `scheduled` with its slot,
    and the reason is counted in the tick's ledger every tick.
    """
    public_id = ""
    with session_scope() as session:
        post = session.get(PostRow, post_id)
        if post is None:
            return {"ok": False, "error": "post disappeared"}
        if post.status == STATUS_PUBLISHED:
            return {"ok": True, "skipped": True, "message_id": post.tg_message_id}
        public_id = post.public_id
    from tgfarm.platforms import is_platform_enabled

    if not is_platform_enabled(platform):
        return {"ok": True, "skipped": True, "reason": GATE_PLATFORM_DISABLED}
    if platform not in {"telegram", "dzen", "vk"}:
        # Reddit/MAX/X are crosspost-only here; never fall through to Telegram.
        # The code must be one the tick can ledger: a reason outside
        # `empty_gate_report()` is a post that vanished without a trace, which is
        # how the dry-run fall-through stayed invisible for as long as it did.
        return {"ok": True, "skipped": True, "reason": GATE_PLATFORM_NOT_DELIVERABLE}
    settings = get_settings()

    if platform == "telegram" and real_tg_chat_id(target) is None:
        # Live or not, a chat we cannot name is not a destination: `due_posts`
        # stops this post from ever reaching here, and the guard is repeated for
        # direct callers so `tg_chat_id = -1` cannot come back.
        log.info(
            "s7: post %s has no known telegram chat; wait for `sync_channels`",
            public_id or post_id,
        )
        return {
            "ok": True,
            "skipped": True,
            "reason": GATE_UNKNOWN_TG_CHAT,
            "post_id": post_id,
        }

    dry_reason = _dry_run_reason(platform, settings)
    if dry_reason is not None:
        await to_thread(_journal_primary_dry_run, public_id, platform, post_id)
        log.info(
            "[dry-run] -> %s %s | %s (not sent: %s)",
            platform,
            target,
            truncate(text, 90),
            dry_reason,
        )
        return {"ok": True, "dry_run": True, "reason": dry_reason, "post_id": post_id}

    if not _claim_post(post_id):
        return {"ok": True, "skipped": True, "reason": "already claimed"}

    heartbeat_task = asyncio.create_task(_publish_heartbeat_loop(post_id))
    budget = publish_timeout_seconds(platform, settings)
    message_id = None
    #: Where Dzen and VK put their identifier: on the journal, not on the post.
    remote_id: Any = None
    try:
        if not settings.live:
            # Unreachable by construction, and stated as the raw fact rather than
            # as a call to `_dry_run_reason`, so a mutation that disables that
            # predicate cannot disable this guard. `_publish_dzen` and `_publish_vk`
            # log a dry run of their own and hand back `{"ok": True,
            # "message_id": None}`; before this line existed, a `dzen`/`vk`
            # dispatch reached with the farm off stamped `published` with no
            # message id — the exact shape of the 15 rows measured on 2026-10-26.
            # The telegram branch already refuses on its own below.
            raise PublishError(
                f"{platform} send branch reached while the farm is not live"
            )
        if platform == "dzen" and not media:
            media = await to_thread(_ensure_cover_for_post, post_id)
            if not media:
                raise PublishImageRequired(
                    "dzen rejects text-only posts and no cover could be rendered"
                )
        if platform == "dzen":
            inline_images = inline_media
            if isinstance(inline_media, tuple):
                inline_images = [inline_media] if inline_media else None
            outcome = await _await_publish(
                _publish_dzen(
                    post_id,
                    text,
                    media,
                    inline_images=inline_images,
                ),
                timeout_s=budget,
                platform=platform,
                post_id=post_id,
            )
            message_id = outcome.get("message_id")
            remote_id = outcome.get("remote_id")
        elif platform == "vk":
            outcome = await _await_publish(
                _publish_vk(post_id, text),
                timeout_s=budget,
                platform=platform,
                post_id=post_id,
            )
            message_id = outcome.get("message_id")
            remote_id = outcome.get("remote_id")
        elif settings.live and settings.telegram_bot_token:
            message_id = await _await_publish(
                PUBLISHER.send(real_tg_chat_id(target), text, media),
                timeout_s=budget,
                platform=platform,
                post_id=post_id,
            )
        else:
            # Unreachable by construction: `_dry_run_reason` returned for every
            # telegram post the farm could not actually send. Landing here means
            # the predicate and this branch disagree, and the old behaviour was to
            # log a dry run and then stamp the post `published` — the defect that
            # left 15 live rows with no message id. Fail loudly instead.
            raise PublishError(
                "telegram send branch reached while the farm cannot publish"
            )
    except asyncio.CancelledError:
        # Cancellation can happen during shutdown or a watchdog recovery. The
        # remote side effect may already have happened, so never leave a live
        # heartbeat behind and never put this post straight back in the queue.
        await _stop_publish_heartbeat(heartbeat_task)
        with session_scope() as session:
            post = session.get(PostRow, post_id)
            # `publisher_owns`, spelled as the predicate so the one guard that
            # already existed here and the four that did not cannot drift apart.
            if post and publisher_owns(post.status):
                post.status = canonical_post_status(STATUS_PUBLISH_UNKNOWN)
                post.publish_heartbeat_at = None
                post.publish_error = "publisher task cancelled; verify platform before retry"
        log.error(
            "publish task cancelled for %s (%s); reconciliation required",
            post_id,
            platform,
        )
        raise
    except Exception as exc:  # noqa: BLE001 - retry later
        await _stop_publish_heartbeat(heartbeat_task)
        retry_min = 5
        if platform == "dzen":
            from tgfarm.dzen_web import DzenCaptcha, DzenNotLoggedIn

            if isinstance(exc, (DzenNotLoggedIn, DzenCaptcha)):
                retry_min = 60
        raw_error_text = str(exc)
        error_text = redact_sensitive_text(raw_error_text, limit=400)
        if isinstance(exc, PublishTimeout):
            log.error(
                "publish deadline exceeded for %s (%s): %s", post_id, platform, error_text
            )
        # A timeout/reset after sending may mean the platform already accepted it.
        ambiguous = _is_ambiguous_publish_error(exc)
        permanent = _is_permanent_publish_error(exc)
        retry_after_seconds = _publish_retry_after_seconds(exc)
        lower_error = raw_error_text.lower()
        transport = any(
            marker in lower_error
            for marker in ("getaddrinfo", "network is unreachable", "connection refused", "connecttimeout", "connecterror")
        )
        try:
            max_attempts = max(1, int(getattr(settings, "primary_publish_max_attempts", 5)))
        except (TypeError, ValueError):
            max_attempts = 5
        terminal_failure = False
        attempts = 1
        outcome_refused: str | None = None
        with session_scope() as session:
            post = session.get(PostRow, post_id)
            if post:
                attempts = max(1, int(post.attempts or 1))
                if not publisher_owns(post.status):
                    # The row left `publishing` while this worker was waiting on
                    # the platform, so another path has already recorded this
                    # attempt. Writing `failed`/`scheduled`/`publish_unknown` over
                    # it would be a second, unasked-for verdict on the same send —
                    # and over a `published` row, an erasure of a real one.
                    outcome_refused = (
                        f"publish failure not recorded: row is {post.status!r}, not "
                        f"{STATUS_PUBLISHING!r}; its outcome already belongs to another path"
                    )
                else:
                    post.publish_error = error_text[:400]
                    post.publish_heartbeat_at = None
                    if ambiguous:
                        post.status = canonical_post_status(STATUS_PUBLISH_UNKNOWN)
                        # Keep scheduled_at as the start timestamp for this attempt.
                    elif permanent or attempts >= max_attempts:
                        # Stop retrying a definite failure after the configured cap.
                        # The operator can explicitly requeue it after fixing the cause.
                        post.status = canonical_post_status(STATUS_FAILED)
                        post.scheduled_at = None
                        terminal_failure = True
                    else:
                        if retry_after_seconds is not None:
                            next_attempt_at = datetime.now(UTC) + timedelta(seconds=retry_after_seconds)
                        else:
                            if transport:
                                retry_min = min(360, 10 * 2 ** min(attempts - 1, 5))
                            next_attempt_at = datetime.now(UTC) + timedelta(minutes=retry_min)
                        post.status = canonical_post_status(STATUS_SCHEDULED)
                        post.scheduled_at = next_attempt_at
        if outcome_refused is not None:
            log.error(
                "s7: %s for post %s (%s): %s", outcome_refused, post_id, platform, error_text
            )
        elif ambiguous:
            log.error(
                "publish outcome unknown for %s (%s): %s; operator reconciliation required",
                post_id, platform, error_text,
            )
        elif terminal_failure:
            reason = "permanent rejection" if permanent else f"attempt limit {max_attempts} reached"
            log.error(
                "publish stopped for %s (%s) after %s attempt(s): %s; operator retry required",
                post_id, platform, attempts, reason,
            )
        else:
            delay_label = (
                f"{retry_after_seconds}s (Retry-After)"
                if retry_after_seconds is not None
                else f"{retry_min} min"
            )
            log.warning(
                "publish failed for %s (%s): %s (retry in %s; attempt %s/%s)",
                post_id, platform, error_text, delay_label, attempts, max_attempts,
            )
        return {
            "ok": False,
            "error": outcome_refused or error_text,
            "outcome_unknown": ambiguous,
            "terminal_failure": terminal_failure,
            **({"outcome_not_recorded": True} if outcome_refused else {}),
        }

    await _stop_publish_heartbeat(heartbeat_task)
    refusal: str | None = None
    with session_scope() as session:
        # A lock here loses the publish outcome: the platform already has the
        # post, so the DB write retries hard before admitting "unknown".
        import time as _time

        from sqlalchemy.exc import OperationalError as _OpErr

        for attempt in range(3):
            try:
                row = session.get(PostRow, post_id)
                if row is None:
                    break
                if not publish_outcome_allowed(row.status, STATUS_PUBLISHED):
                    # The only writer of `published` in this module, and the one
                    # place a fabrication still fitted: the heartbeat watchdog, an
                    # operator reconciliation or a second worker can put this row
                    # anywhere between the claim and here, and an unpreconditioned
                    # write would stamp `published` on a verdict the farm has
                    # already recorded — including `publish_unknown`, whose whole
                    # meaning is "a human has not checked yet".
                    refusal = (
                        f"publication not recorded: the row left {STATUS_PUBLISHING!r} and "
                        f"is now {row.status!r}; the destination may hold a copy this "
                        "database no longer owns"
                    )
                    if row.status in SEND_AUTHORIZING_STATUSES:
                        # The other half of the same fact: the platform has the
                        # post, and a row that still says "queued" invites a second
                        # send of it. Park it where the watchdog parks an attempt it
                        # could not resolve, so nobody re-submits without checking.
                        row.status = canonical_post_status(STATUS_PUBLISH_UNKNOWN)
                        row.publish_heartbeat_at = None
                        row.publish_error = refusal[:400]
                        refusal += f"; parked as {STATUS_PUBLISH_UNKNOWN} so it is not sent twice"
                    break
                if not has_publication_evidence(
                    session,
                    public_id=public_id,
                    platform=platform,
                    post_id=post_id,
                    message_id=message_id,
                    remote_id=remote_id,
                ):
                    # Nothing was sent, or something was sent and no identifier
                    # came back — either way there is nothing to point at. The
                    # honest state is the one `maintenance.publication_evidence_repair`
                    # already moves these rows to after the fact.
                    row.status = canonical_post_status(STATUS_PUBLISH_UNKNOWN)
                    row.publish_heartbeat_at = None
                    row.publish_error = (
                        f"{platform} returned ok with no remote identifier of any kind "
                        "(no message id, no non-placeholder remote id, no journalled "
                        f"`posted` row); verify the destination before trusting or "
                        "retrying it"
                    )[:400]
                    refusal = "publication without evidence: recorded as publish_unknown"
                    break
                row.status = canonical_post_status(STATUS_PUBLISHED)
                row.published_at = datetime.now(UTC)
                row.tg_message_id = message_id
                row.publish_heartbeat_at = None
                if platform == "telegram":
                    # Named by the chat guard before the post was claimed: an
                    # unknown chat can no longer be written as `-1`.
                    row.tg_chat_id = real_tg_chat_id(target)
                row.publish_error = None
                break
            except _OpErr as exc:
                if "locked" not in str(exc) or attempt == 2:
                    log.error("publish outcome UNKNOWN for %s: %s", post_id, exc)
                    raise
                session.rollback()
                _time.sleep(5 * (attempt + 1))

    if refusal is not None:
        log.error("s7: %s for post %s (%s)", refusal, post_id, platform)
        return {
            "ok": False,
            "error": refusal[:400],
            "outcome_unknown": True,
            "outcome_not_recorded": True,
            "post_id": post_id,
        }

    return {"ok": True, "message_id": message_id, "post_id": post_id}


DELIVERABLE_PLATFORMS = frozenset({"telegram", "dzen", "vk"})

#: Every branch that can drop an otherwise-eligible post names itself here.
#: "published 0" is only an actionable fact when it says which gate did it, so a
#: new gate that returns nothing must add its reason or the journal goes blind.
GATE_PLATFORM_DISABLED = "platform_disabled"
GATE_PLATFORM_NOT_DELIVERABLE = "platform_not_deliverable"
GATE_APPROVAL_REQUIRED = "approval_required"
GATE_PERSONA_DISABLED = "persona_disabled"
GATE_DAILY_CAP = "daily_cap"
GATE_DZEN_QUEUE_GAP = "dzen_queue_gap"
GATE_DZEN_NEEDS_ONLINE_MODEL = "dzen_needs_online_model"
#: The farm is not in `live` mode: nothing was sent, and the posts are still
#: queued. Counted here so a tick that "published 0" because it never sends says
#: so, instead of looking like an empty queue.
GATE_FARM_NOT_LIVE = "farm_not_live"
#: `live` mode without a bot token — the same certain "not sent", different fix.
GATE_BOT_TOKEN_MISSING = "telegram_bot_token_missing"
#: The channel has no Telegram chat id yet (`sync_channels` fills it on the
#: scheduler's clock). Named instead of guessed at `-1`.
GATE_UNKNOWN_TG_CHAT = "unknown_tg_chat"
#: A slot the editorial plan booked falls outside the channel's legal local
#: hours, so this scheduler refuses it. The plan layer calls the same fact
#: `plan_execution.REASON_LOCAL_HOURS`, and the two spellings are pinned equal
#: by `tests/test_publish_gate_matrix.py`: one fact, one string, in whichever
#: ledger it is counted.
GATE_SLOT_OUTSIDE_LOCAL_HOURS = "slot_outside_local_hours"
#: The same booking, corrected. Since W22-C a minute the channel may never
#: publish in is not left to rot in the queue: this scheduler asks the planner's
#: own slot arithmetic for the first legal instant at or after the refused one,
#: writes *that* to `scheduled_at` and counts the replacement here. The booking
#: column keeps the refused minute (P100 — written once, and it is what the plan
#: can be audited against), so the two numbers together are the whole fact: this
#: is why the outcome gets its own code instead of hiding inside `scheduled N`.
#: The plan consumer names the same transition `plan_execution.REASON_SLOT_REBOUND`,
#: and the equality is pinned by `tests/test_publish_gate_matrix.py` exactly like
#: the refusal above.
GATE_SLOT_BOOKING_REBOUNDED = "slot_booking_rebounded"


def empty_gate_report() -> dict[str, int]:
    """Zero-filled ledger so a missing reason never reads as "gate absent"."""
    return {
        GATE_PLATFORM_DISABLED: 0,
        GATE_PLATFORM_NOT_DELIVERABLE: 0,
        GATE_APPROVAL_REQUIRED: 0,
        GATE_PERSONA_DISABLED: 0,
        GATE_DAILY_CAP: 0,
        GATE_DZEN_QUEUE_GAP: 0,
        GATE_DZEN_NEEDS_ONLINE_MODEL: 0,
        GATE_FARM_NOT_LIVE: 0,
        GATE_BOT_TOKEN_MISSING: 0,
        GATE_UNKNOWN_TG_CHAT: 0,
        GATE_SLOT_OUTSIDE_LOCAL_HOURS: 0,
        GATE_SLOT_BOOKING_REBOUNDED: 0,
    }


def due_posts(
    limit: int = 20,
    post_public_ids: list[str] | None = None,
    blocked: dict[str, int] | None = None,
) -> list[tuple[int, str, int | str, str]]:
    """Load due posts, optionally restricted to a specific operator selection.

    `blocked` is an optional out-parameter: when given, the two gates that drop a
    queued post without changing its state count themselves into it. Those posts
    are the whole explanation for a tick that loads rows and still publishes
    none, so the reason has to travel with the selection instead of vanishing
    with the `continue`.
    """
    if post_public_ids is not None and not post_public_ids:
        return []
    now = datetime.now(UTC)
    out: list[tuple[int, str, int | str, str]] = []
    from tgfarm.platforms import is_platform_enabled

    def _blocked(reason: str) -> None:
        if blocked is not None:
            blocked[reason] = blocked.get(reason, 0) + 1

    with session_scope() as session:
        stmt = (
            select(PostRow)
            .where(PostRow.status == "scheduled", PostRow.scheduled_at <= now)
            .order_by(PostRow.scheduled_at.asc())
        )
        if post_public_ids is not None:
            stmt = stmt.where(PostRow.public_id.in_(post_public_ids))
        rows = session.execute(stmt.limit(limit)).scalars().all()
        for row in rows:
            channel = session.get(ChannelRow, row.channel_id)
            platform = ((channel.platform if channel else None) or "telegram").lower()
            if not is_platform_enabled(platform):
                # Pause delivery without changing the queued post's state.
                # Re-enabling the platform makes the same queued post eligible again.
                _blocked(GATE_PLATFORM_DISABLED)
                continue
            if platform not in DELIVERABLE_PLATFORMS:
                # Crosspost-only services must never be sent to the Telegram API.
                _blocked(GATE_PLATFORM_NOT_DELIVERABLE)
                continue
            if platform == "dzen":
                target: int | str = channel.id if channel else "dzen"
                out.append((row.id, assemble_text(row, platform), target, platform))
                continue

            if platform == "vk":
                out.append((row.id, assemble_text(row, "vk"), "vk", platform))
                continue

            chat_id = real_tg_chat_id(channel.tg_chat_id if channel else None)
            if chat_id is None:
                # "We do not know the chat" is a named state, not a number. The
                # old fallback to `settings.channel_ids[0] or -1` posted one
                # persona's text into another operator's channel and stamped the
                # invented `-1` on the post as if Telegram had reported it.
                # `sync_channels` fills the real id on the scheduler's clock.
                _blocked(GATE_UNKNOWN_TG_CHAT)
                continue
            out.append((row.id, assemble_text(row), chat_id, platform))
    return out


def _media_for(post_ids: list[int]) -> dict[int, str]:
    """Primary media per post. Video wins over cover art when both exist — a post
    with footage should not open with a still frame."""
    from tgfarm.db.models import Media as MediaRow

    if not post_ids:
        return {}
    with session_scope() as session:
        rows = session.execute(
            select(MediaRow).where(
                MediaRow.post_id.in_(post_ids), MediaRow.is_primary.is_(True)
            )
        ).scalars().all()
        chosen: dict[int, str] = {}
        for row in rows:
            if row.kind == "video":
                chosen[row.post_id] = row.path
            elif row.post_id not in chosen:
                chosen[row.post_id] = row.path
        return chosen


def _inline_media_for(post_ids: list[int]) -> dict[int, list[tuple[str, str]]]:
    """Lead photos stored for inline Dzen use, independent of the primary asset."""
    from tgfarm.db.models import Media as MediaRow

    if not post_ids:
        return {}
    with session_scope() as session:
        rows = session.execute(
            select(MediaRow).where(
                MediaRow.post_id.in_(post_ids), MediaRow.kind == "article_image"
            )
        ).scalars().all()
        chosen: dict[int, list[tuple[str, str]]] = {}
        for row in rows:
            if row.path and Path(row.path).exists():
                chosen.setdefault(row.post_id, []).append(
                    (row.path, row.caption or "")
                )
        return chosen


def _last_dzen_publish_time() -> datetime | None:
    with session_scope() as session:
        value = session.execute(
            select(PostRow.published_at)
            .join(ChannelRow, ChannelRow.id == PostRow.channel_id)
            .where(ChannelRow.platform == "dzen", PostRow.status == "published")
            .order_by(PostRow.published_at.desc())
            .limit(1)
        ).scalar_one_or_none()
    if value is not None and getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=UTC)
    return value


async def _publish_due_once(
    limit: int = 1000,
    post_public_ids: list[str] | None = None,
) -> dict[str, Any]:
    get_settings()
    gates = empty_gate_report()
    with StageTimer("s7_publish") as timer:
        timer.items_in = 0
        requeued = await to_thread(_requeue_stuck)
        if requeued:
            log.warning(
                "s7: moved %d interrupted posts to publish_unknown; reconcile before retry",
                requeued,
            )
        pending = await to_thread(due_posts, limit, post_public_ids, gates)
        timer.items_in = len(pending)
        if not pending:
            timer.items_out = 0
            return {"published": 0, "failed": 0, "due": 0, "gates": gates}

        post_ids = [p[0] for p in pending]
        media = await to_thread(_media_for, post_ids)
        inline = await to_thread(_inline_media_for, post_ids)
        sent = failed = skipped = dry_runs = 0
        errors: list[str] = []
        last_dzen = getattr(publish_due, "_last_dzen", None)
        db_last = await to_thread(_last_dzen_publish_time)
        if db_last is not None and (last_dzen is None or db_last > last_dzen):
            last_dzen = db_last
        publish_due._last_dzen = last_dzen
        dzen_gap_s = max(0, get_settings().dzen_min_gap_s)
        settings_now = get_settings()
        for post_id, text, target, platform in pending:
            if platform == "dzen":
                now_dt = datetime.now(UTC)
                if last_dzen is not None and (now_dt - last_dzen).total_seconds() < dzen_gap_s:
                    gates[GATE_DZEN_QUEUE_GAP] += 1
                    continue
                if settings_now.dzen_requires_llm and not await to_thread(
                _llm_recently_working
            ):
                    # Template text on Dzen is the fastest route to demotion:
                    # the post waits while the last drafts are still coming
                    # from an online model. A health-flag cache lies in both
                    # directions — the actual output is the honest signal.
                    gates[GATE_DZEN_NEEDS_ONLINE_MODEL] += 1
                    continue
            try:
                result = await _publish_one(
                    post_id,
                    target,
                    text,
                    media.get(post_id),
                    platform,
                    inline.get(post_id),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad post must not stop the batch
                failed += 1
                errors.append(f"{platform}:{type(exc).__name__}")
                log.error(
                    "publish worker crashed for %s (%s): %s",
                    post_id,
                    platform,
                    redact_sensitive_text(exc, limit=300),
                )
                continue
            if result.get("ok"):
                if result.get("dry_run"):
                    # Nothing was sent, so nothing is counted as sent. The reason
                    # belongs in the ledger beside the gates: "published 0" in a
                    # farm that is not live is a queue waiting, not a delivery.
                    reason = str(result.get("reason") or GATE_FARM_NOT_LIVE)
                    gates[reason] = gates.get(reason, 0) + 1
                    dry_runs += 1
                    continue
                if result.get("skipped"):
                    skipped += 1
                    reason = str(result.get("reason") or "")
                    if reason == GATE_UNKNOWN_TG_CHAT:
                        gates[reason] = gates.get(reason, 0) + 1
                    continue
                sent += 1
                if platform == "dzen":
                    last_dzen = datetime.now(UTC)
                    publish_due._last_dzen = last_dzen
            else:
                failed += 1
                if result.get("error"):
                    errors.append(str(result.get("error")))

        timer.items_out = sent
        return {
            "published": sent,
            "failed": failed,
            "due": len(pending),
            "errors": errors,
            # `gates` names every post that was eligible and still not sent.
            # `skipped_llm` stays as the historical alias for the Dzen model
            # gate so existing readers of the stage payload keep working.
            "gates": gates,
            **({"skipped_llm": gates[GATE_DZEN_NEEDS_ONLINE_MODEL]}
               if gates[GATE_DZEN_NEEDS_ONLINE_MODEL] else {}),
            **({"skipped": skipped} if skipped else {}),
            # The count of posts this tick held back because nothing could be
            # sent. Named separately from `skipped`, which also covers the posts
            # another worker claimed first.
            **({"dry_run": dry_runs} if dry_runs else {}),
        }


_active_publish_task: asyncio.Task | None = None


async def publish_due(
    limit: int = 1000,
    post_public_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Serialize publisher batches so platform-level spacing rules stay valid."""
    global _active_publish_task

    current_task = asyncio.current_task()
    active = _active_publish_task
    if active is not None and active is not current_task and not active.done():
        log.info("s7: publish batch skipped because another batch is still running")
        return {"published": 0, "failed": 0, "skipped_concurrent": 1}

    if current_task is not None:
        _active_publish_task = current_task
    try:
        return await _publish_due_once(limit, post_public_ids)
    finally:
        if current_task is not None and _active_publish_task is current_task:
            _active_publish_task = None


async def run() -> dict[str, Any]:
    """Schedule what is ready, then deliver what is due, and say why not.

    The returned `gates` block is the only place the two halves of the stage meet:
    a tick that publishes nothing has to be able to name the gate that held it,
    because `published: 0` alone cannot tell a healthy queue from a stalled one.
    """
    gates = empty_gate_report()
    scheduled = await to_thread(schedule_ready_posts, 200, gates)
    published = await publish_due()
    report = dict(gates)
    for reason, count in (published.get("gates") or {}).items():
        report[reason] = report.get(reason, 0) + int(count)
    return {"scheduled": scheduled, **{k: v for k, v in published.items() if k != "gates"},
            "gates": report}


def unpublish(posts_public_ids: list[str], *, at: datetime | None = None) -> int:
    """Schedule eligible posts, optionally at an instant the caller was given.

    A single conditional UPDATE is important here: selecting ORM objects and
    saving them later can overwrite a worker's concurrent transition to
    `publishing`, reopening a duplicate-send race.

    `at` exists for one caller: the plan's consumer. `plan_execution` judges a
    `schedule` entry against the minute the plan reserved (`slot_at`) — it holds
    the entry until that minute arrives — so the instant the handover has to
    stamp is the reserved minute, not the executor's own clock. Without it the
    reservation never reaches the row, and `posts.scheduled_at` can then only
    disagree with `posts.plan_slot_at`, which is the disagreement W20-C reads as
    `shifted` and nothing else in the store can undo.

    Naming an instant is the scheduler's own rule, not a new one:
    `schedule_ready_posts` already writes `scheduled_at = booked` for a post the
    plan reserved, an overdue booking included, so a past `at` makes the post due
    on the next publish tick instead of re-slotting the plan.

    `at=None` is the base behaviour and the one every other caller relies on —
    the panel's requeue (`tgfarm/api.py`), the CLI, the state-machine matrix:
    `scheduled_at = datetime.now(UTC)`.
    """
    moment = _as_utc(at) if at is not None else datetime.now(UTC)
    with session_scope() as session:
        result = session.execute(
            update(PostRow)
            .where(
                PostRow.public_id.in_(posts_public_ids),
                PostRow.status.in_(("scheduled", "gated")),
            )
            .values(status=canonical_post_status(STATUS_SCHEDULED), scheduled_at=moment)
        )
        return result.rowcount or 0


def new_run_id() -> str:
    return uuid.uuid4().hex[:10]
