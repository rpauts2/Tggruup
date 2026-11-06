"""Reconcile configured channels with Telegram reality.

`config/personas.yaml` is a wish list. This module finds out what is actually
true: which channels exist, how many people read them, whether the farm's bot
is an administrator there, and — the part that used to be missing entirely — what
the published posts actually earned.

Everything here is read-only against Telegram and writes only to our own
database.

Observability (W30-A): `sync_channels` used to be fire-and-forget — if it never
ran, nothing recorded that fact, and if Telegram was unreachable it raised into
whoever called it. Now every call, successful or not, produces a structured
result (seen / added / updated / removed_upstream / failed with reason codes)
and leaves a timestamped "ran" marker on disk that `sync_status()` reads back.
Channel text coming from Telegram is untrusted data: it is truncated and stored
inert, never interpreted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import func, select

from tgfarm.core.logging import get_logger
from tgfarm.db.models import Channel, Post, PostStat
from tgfarm.db.session import session_scope

log = get_logger(__name__)

# The single Telegram-session call in the sync; the list cap keeps memory flat.
CHAT_LIMIT = 300
# Caps for untrusted text as it leaves Telegram and enters our rows/reports.
TEXT_CAP = 200
USERNAME_CAP = 64
# How often pipeline._build_scheduler should call sync_channels (see spec).
SYNC_INTERVAL_MIN = 360

REASON_UNREACHABLE = "telegram_unavailable"
REASON_ADMIN_CHECK = "bot_admin_check_failed"
REASON_NOT_SEEN = "chat_not_seen_upstream"
REASON_STATE_WRITE = "state_write_failed"


#: Values the farm used to *invent* for "this channel was never synced".
#: `due_posts` fell back to `settings.channel_ids[0] or -1` and stamped the
#: result on the post, so a placeholder travelled through the codebase looking
#: exactly like an observed chat id.
UNKNOWN_CHAT_PLACEHOLDERS = frozenset({0, -1})


def real_tg_chat_id(value: object) -> int | None:
    """Return the chat id only when it names a chat Telegram could answer about.

    "We do not know the chat" is a distinct state, not a small number: a
    supergroup really is negative (`-100…`), so the sign proves nothing, while
    `None`, `0` and `-1` are placeholders this repository wrote for an unsynced
    channel. Reading them as ids made the collector ask Telegram about chat -1
    and let `s8_analytics.coverage` count an invented chat as an askable post.
    Callers that get `None` back must report "run/wait for `sync_channels`",
    never a number.
    """
    if value is None:
        return None
    try:
        chat_id = int(value)
    except (TypeError, ValueError):
        return None
    if chat_id in UNKNOWN_CHAT_PLACEHOLDERS:
        return None
    return chat_id


def _normalise(text: str) -> str:
    return "".join(c for c in (text or "").lower() if c.isalnum() or c.isspace()).strip()


def _safe_text(value: object, cap: int = TEXT_CAP) -> str:
    """Coerce untrusted Telegram text to a plain, length-capped string.

    Channel titles and usernames are attacker-controlled data. We keep them
    inert: truncated, never executed, never interpolated anywhere.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text[:cap]


def _failure(subject: str, reason_code: str, detail: object = "") -> dict:
    from tgfarm.core.security import redact_sensitive_text

    return {
        "subject": subject,
        "reason_code": reason_code,
        "detail": redact_sensitive_text(detail, limit=180),
    }


def _chat_preview(chat) -> dict:
    """A truncated, inert view of one Telegram chat for reports and panels."""
    return {
        "id": int(chat.id),
        "title": _safe_text(chat.title, TEXT_CAP),
        "username": _safe_text(chat.username, USERNAME_CAP) or None,
        "kind": _safe_text(chat.kind, 16) or "channel",
        "participants": int(chat.participants or 0),
    }


def _match(persona_id: str, title: str, username: str | None, chats: list) -> object | None:
    """Find the chat that corresponds to a configured channel.

    Username first — it is unambiguous. Then the linked discussion group, then a
    normalised title, then a fuzzy containment match. A wrong match here would
    attach statistics to the wrong channel, so the order matters.
    """
    if username:
        want = username.lower().lstrip("@")
        for chat in chats:
            if chat.username and chat.username.lower() == want:
                return chat
    key = _normalise(title)
    if not key:
        return None
    for chat in chats:
        if _normalise(chat.title) == key:
            return chat
    for chat in chats:
        if chat.linked_title and _normalise(chat.linked_title) == key:
            return chat
    for chat in chats:
        if chat.kind == "channel" and key and _normalise(chat.title) in key:
            return chat
    return None


async def _fetch_chats(limit: int = CHAT_LIMIT) -> list:
    """The one boundary where the sync touches the Telegram session.

    Everything above it is pure; tests monkeypatch this function.
    """
    from tgfarm.tguser import list_chats

    return await list_chats(limit=limit)


def _state_path():
    """Where the last-run marker lives. A file, not a table: the schema is
    owned elsewhere and the panel only needs the newest snapshot."""
    from tgfarm.core.settings import get_settings

    return get_settings().data_root() / "telegram_sync_state.json"


def _write_state(report: dict) -> bool:
    payload = {
        "ran_at": report["ran_at"],
        "ok": report["ok"],
        "reason": report["reason"],
        "seen": report["seen"],
        "configured": report["configured"],
        "matched": report["matched"],
        "added": report["added"],
        "updated": report["updated"],
        "removed_upstream": report["removed_upstream"],
        "failed": report["failed"],
    }
    try:
        _state_path().write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return True
    except OSError as exc:
        log.warning("telegram sync state not written: %s", str(exc)[:120])
        return False


def sync_status() -> dict:
    """The last recorded run of `sync_channels`, read without touching Telegram.

    Before W30-A the operator could not tell whether the sync had ever run;
    now `never_run: True` is an answer instead of a guess.
    """
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        return {"never_run": True, "ran_at": None, "ok": None, "reason": "never_run"}
    result = {"never_run": False}
    result.update(raw)
    return result


def scheduler_job_spec() -> dict:
    """Pure description of the scheduler entry the pipeline *should* register.

    This module does not own pipeline.py, so it only says what belongs there;
    wiring the job in is the coordinator's seam.
    """
    return {
        "func": sync_channels,
        "trigger": "interval",
        "minutes": SYNC_INTERVAL_MIN,
        "id": "telegram-channel-sync",
        "name": "telegram channel sync",
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": 3600,
    }


def _apply_reality(row: Channel, chat, bot_admin: dict[int, bool], now: datetime) -> bool:
    """Write one channel's observed reality onto its row.

    Returns True only if a persisted field actually changed — `tg_synced_at`
    is touched every run and deliberately does not count, so a second run over
    the same data reports zero updates.
    """
    changed = False
    observations: list[tuple[str, object]] = [
        ("tg_chat_id", int(chat.id)),
        ("tg_participants", int(chat.participants or 0) or None),
        ("tg_verified", bool(chat.is_verified)),
        ("username", _safe_text(chat.username, USERNAME_CAP) or None),
    ]
    admin = bot_admin.get(int(chat.id))
    if admin is not None:
        observations.append(("tg_bot_admin", admin))
    for field, value in observations:
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed = True
    row.tg_synced_at = now
    return changed


def _base_report(now: datetime, personas: list, chats: list, failed: list) -> dict:
    return {
        "ok": not failed,
        "reason": failed[0]["reason_code"] if failed else None,
        "ran_at": now.isoformat(),
        "synced_at": now.isoformat(),
        "seen": len(chats),
        "telegram_chats": len(chats),
        "configured": len(personas),
        "matched": 0,
        "added": [],
        "updated": [],
        "removed_upstream": [],
        "missing_in_telegram": [],
        "bot_not_admin": [],
        "unconfigured_channels": [],
        "failed": failed,
    }


async def sync_channels() -> dict:
    """Refresh channel reality from Telegram and report exactly what happened.

    Idempotent: a second run over unchanged data adds and updates nothing and
    says so. Failure-visible: an unreachable Telegram session or a failed
    permission check is returned as a stated outcome with a reason code, never
    raised-and-swallowed. Self-reporting: every call ends with a structured
    result and a timestamped marker that `sync_status()` can read back.
    """
    from tgfarm.config import get_config

    now = datetime.now(UTC)
    personas = get_config().by_platform("telegram")
    failed: list[dict] = []

    try:
        chats = await _fetch_chats()
    except Exception as exc:  # noqa: BLE001 - offline Telegram is a result, not a crash
        log.warning("telegram sync: session unreachable (%s)", type(exc).__name__)
        failed.append(_failure("list_chats", REASON_UNREACHABLE, type(exc).__name__))
        report = _base_report(now, personas, [], failed)
        report["ok"] = False
        _write_state(report)
        return report

    try:
        bot_admin = await _bot_admin_map()
    except Exception as exc:  # noqa: BLE001 - a failed permission check must not sink sync
        log.warning("telegram sync: bot admin check failed (%s)", type(exc).__name__)
        bot_admin = {}
        failed.append(_failure("getChatMember", REASON_ADMIN_CHECK, type(exc).__name__))

    matched: dict[str, object] = {}
    for persona in personas:
        found = _match(persona.id, persona.title, persona.tg_username, chats)
        if found is not None:
            matched[persona.id] = found

    added: list[str] = []
    updated: list[str] = []
    removed_upstream: list[str] = []
    missing: list[dict] = []

    with session_scope() as session:
        rows = {row.id: row for row in session.execute(select(Channel)).scalars().all()}

        for persona in personas:
            row = rows.get(persona.id)
            chat = matched.get(persona.id)
            if row is None:
                row = Channel(
                    id=persona.id,
                    platform="telegram",
                    title=_safe_text(persona.title, TEXT_CAP),
                    niche=_safe_text(persona.niche, 64),
                    persona=persona.as_dict(),
                    timezone=persona.timezone,
                    posts_per_day=persona.posts_per_day,
                    enabled=persona.enabled,
                )
                session.add(row)
                rows[persona.id] = row
                added.append(persona.id)
                if chat is not None:
                    _apply_reality(row, chat, bot_admin, now)
                else:
                    row.tg_synced_at = now
                continue
            if chat is None:
                if row.tg_chat_id:
                    removed_upstream.append(persona.id)
                missing.append({"id": persona.id, "title": _safe_text(persona.title)})
                continue
            if _apply_reality(row, chat, bot_admin, now):
                updated.append(persona.id)

        stored_ids = {row.tg_chat_id for row in rows.values() if row.tg_chat_id}
        not_admin = [
            {"id": row.id, "reason": "бот не администратор"}
            for row in rows.values()
            if row.tg_chat_id and row.tg_bot_admin is False
        ]

    configured_titles = {_normalise(p.title) for p in personas}
    unconfigured = [
        _chat_preview(c)
        for c in chats
        if c.id not in stored_ids
        and c.kind == "channel"
        and c.participants > 0
        and _normalise(c.title) not in configured_titles
    ]
    missing.extend(
        {"id": p.id, "title": _safe_text(p.title)}
        for p in personas
        if p.id not in matched and p.id not in {m["id"] for m in missing}
    )

    report = _base_report(now, personas, chats, failed)
    report.update(
        {
            "ok": not failed and not missing and not not_admin,
            "reason": failed[0]["reason_code"] if failed else None,
            "matched": len(matched),
            "added": added,
            "updated": updated,
            "removed_upstream": removed_upstream,
            "missing_in_telegram": missing,
            "bot_not_admin": not_admin,
            "unconfigured_channels": unconfigured[:25],
        }
    )
    if not _write_state(report):
        report["failed"].append(_failure("state", REASON_STATE_WRITE, ""))
    log.info(
        "telegram sync: %d/%d каналов найдено, %d чатов видно, +%d новых, %d изменено",
        len(matched), len(personas), len(chats), len(added), len(updated),
    )
    return report


async def _bot_admin_map() -> dict[int, bool]:
    """Whether the farm's bot can post in each configured channel.

    Keyed by *Telethon* entity id, not by the Bot API chat id, because that is
    what the sync compares against. The two are not interchangeable: the API
    spells a channel as `-1001234567890` while MTProto reports the bare
    `1234567890`.
    """
    import httpx

    from tgfarm.core.settings import get_settings

    settings = get_settings()
    token = settings.telegram_bot_token
    if not token:
        return {}

    ids: dict[int, bool] = {}
    async with httpx.AsyncClient(timeout=20.0) as client:
        me = await _bot_id(token)
        for chat_id in settings.channel_ids:
            try:
                resp = await client.get(
                    f"https://api.telegram.org/bot{token}/getChatMember",
                    params={"chat_id": chat_id, "user_id": me},
                )
                data = resp.json()
                status = (data.get("result") or {}).get("status", "")
                ids[entity_id(chat_id)] = status in ("administrator", "creator")
            except Exception as exc:  # noqa: BLE001 - a missing permission is a report
                log.info("getChatMember failed for %s: %s", chat_id, exc)
    return ids


def entity_id(api_chat_id: int) -> int:
    """Bot API chat id → MTProto entity id.

    Channels and megagroups are exposed as `-100` + entity_id; plain chats use a
    negative id. Getting this wrong silently checks the permission of a chat that
    does not exist.
    """
    raw = str(api_chat_id)
    if raw.startswith("-100"):
        return int(raw[4:])
    return abs(api_chat_id)


_bot_id_cache: int | None = None


async def _bot_id(token: str) -> int:
    global _bot_id_cache
    if _bot_id_cache is not None:
        return _bot_id_cache
    import httpx

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        _bot_id_cache = int((resp.json().get("result") or {}).get("id", 0))
    return _bot_id_cache


async def collect_post_stats(limit: int = 500) -> dict:
    """Pull live statistics for published posts and store them as a daily snapshot.

    A post is only askable when the farm can name it on Telegram: a message id
    *and* the chat it lives in. `sync_channels` fills the chat id on the channel,
    the publisher fills it on the post, and either is enough — reading only the
    channel used to make every post published before the first sync unmeasurable,
    which is how the learning loop stayed hungry even after the collector worked.

    "Enough" means a real id, not any stored number: `real_tg_chat_id` rejects
    the `0`/`-1` placeholders the old queue builder wrote, so the collector never
    asks Telegram about a chat that does not exist.
    """
    from tgfarm.tguser import fetch_post_stats

    with session_scope() as session:
        rows = list(
            session.execute(
                select(Post)
                .where(Post.status == "published", Post.tg_message_id.isnot(None))
                .order_by(Post.published_at.desc())
                .limit(limit)
            ).scalars().all()
        )
        pairs: list[tuple[int, int]] = []
        chat_of: dict[int, int] = {}
        unknown_chat = 0
        for row in rows:
            channel = session.get(Channel, row.channel_id)
            chat_id = real_tg_chat_id(getattr(channel, "tg_chat_id", None)) or real_tg_chat_id(
                row.tg_chat_id
            )
            if not chat_id:
                unknown_chat += 1
                continue
            pairs.append((chat_id, row.tg_message_id))
            chat_of[row.id] = chat_id

    if not pairs:
        if rows:
            return {
                "ok": True,
                "posts": 0,
                "askable": 0,
                "published_with_message_id": len(rows),
                "without_real_chat_id": unknown_chat,
                "reason": (
                    "posts have a message id but no real chat id: "
                    f"{unknown_chat} of {len(rows)} name no chat (0/-1 is a placeholder, "
                    "not an id) — run or wait for `sync_channels`"
                ),
            }
        return {
            "ok": True,
            "posts": 0,
            "askable": 0,
            "published_with_message_id": 0,
            "reason": "no posts with a telegram message id",
        }

    stats = await fetch_post_stats(pairs)
    if not stats:
        return {"ok": True, "posts": 0, "reason": "telegram returned nothing visible"}

    now = datetime.now(UTC)
    today = now.date().isoformat()
    written = 0
    with session_scope() as session:
        for row_id, chat_id in chat_of.items():
            key = (chat_id, session.get(Post, row_id).tg_message_id or 0)
            data = stats.get(key)
            if data is None:
                continue
            existing = session.execute(
                select(PostStat).where(
                    PostStat.post_id == row_id,
                    func.date(PostStat.collected_at) == today,
                )
            ).scalar_one_or_none()
            if existing is None:
                existing = PostStat(post_id=row_id)
                session.add(existing)
            existing.collected_at = now
            existing.views = data["views"]
            existing.reactions = data["reactions"]
            existing.forwards = data["forwards"]
            existing.comments = 0
            written += 1

    log.info("collected live stats for %d posts (%d/%d visible)", written, len(stats), len(pairs))
    return {
        "ok": True,
        "posts": written,
        "visible": len(stats),
        "requested": len(pairs),
        "day": today,
    }


def channel_reality() -> list[dict]:
    """What we last learned about each configured channel, for the dashboard."""
    with session_scope() as session:
        rows = session.execute(select(Channel).order_by(Channel.id)).scalars().all()
        return [
            {
                "id": r.id,
                "title": r.title,
                "tg_chat_id": r.tg_chat_id,
                "username": r.username,
                "participants": r.tg_participants,
                "verified": r.tg_verified,
                "bot_admin": r.tg_bot_admin,
                "synced_at": r.tg_synced_at.isoformat() if r.tg_synced_at else None,
            }
            for r in rows
        ]


__all__ = [
    "channel_reality",
    "collect_post_stats",
    "real_tg_chat_id",
    "scheduler_job_spec",
    "sync_channels",
    "sync_status",
]
