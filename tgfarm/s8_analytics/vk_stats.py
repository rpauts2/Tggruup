"""VK wall metrics for published posts.

The farm publishes most of its volume to VK, but ``post_stats`` had no VK rows at
all: only Dzen (browser Studio) and Telegram were measured, so the learning layer
was blind to its main channel. ``wall.getById`` returns views, likes, comments and
reposts for up to a hundred posts per call, which is enough to keep the whole
recent feed measured without a per-post request.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select

from tgfarm.core.logging import get_logger
from tgfarm.core.security import redact_sensitive_text
from tgfarm.core.settings import get_settings
from tgfarm.db.models import CrosspostLog
from tgfarm.db.session import session_scope

log = get_logger(__name__)

API_URL = "https://api.vk.com/method/wall.getById"
API_VERSION = "5.199"
BATCH_SIZE = 50


def _owner_id(group_id: str) -> str:
    raw = str(group_id or "").strip()
    if not raw:
        return ""
    return raw if raw.startswith("-") else f"-{raw}"


def recent_vk_posts(days: int = 14, limit: int = 100) -> list[tuple[str, str]]:
    """``(post_public_id, remote_id)`` for recently posted VK wall items.

    Remote ids come from the crosspost log: the publisher stores the wall post id
    ('406', with the group wall id in the url), not a Telegram message id.
    """
    cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
    with session_scope() as session:
        rows = session.execute(
            select(CrosspostLog.post_public_id, CrosspostLog.remote_id, CrosspostLog.created_at)
            .where(
                CrosspostLog.platform == "vk",
                CrosspostLog.status == "posted",
                CrosspostLog.remote_id.isnot(None),
                CrosspostLog.created_at >= cutoff,
            )
            .order_by(CrosspostLog.created_at.desc())
            .limit(max(1, limit))
        ).all()
        seen: dict[str, str] = {}
        for public_id, remote_id, _created in rows:
            remote = str(remote_id or "").strip()
            if public_id and remote and public_id not in seen:
                seen[public_id] = remote
        return list(seen.items())


def attributable_vk_posts(days: int = 14, limit: int = 100) -> int:
    """How many VK publications could be measured right now, id included or not.

    The sampling window is a budget, not a fact about the farm: live data has
    375 posted VK rows with a wall id while one run looks at 100 of the last 14
    days. The gap has to be visible or "saved: 0" reads as "nothing to measure".
    """
    cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
    with session_scope() as session:
        return int(
            session.execute(
                select(func.count(func.distinct(CrosspostLog.post_public_id))).where(
                    CrosspostLog.platform == "vk",
                    CrosspostLog.status == "posted",
                    CrosspostLog.remote_id.isnot(None),
                    CrosspostLog.remote_id != "",
                    CrosspostLog.created_at >= cutoff,
                )
            ).scalar_one()
            or 0
        )


def _items(payload: Any) -> list[dict[str, Any]]:
    """Normalise ``wall.getById`` across response shapes (list or items wrapper)."""
    body = payload.get("response") if isinstance(payload, dict) else payload
    if isinstance(body, dict):
        body = body.get("items")
    if not isinstance(body, list):
        return []
    return [item for item in body if isinstance(item, dict)]


async def _fetch(items_ids: list[str], *, token: str, timeout: float = 20.0) -> list[dict[str, Any]]:
    params = {
        "posts": ",".join(items_ids),
        "access_token": token,
        "v": API_VERSION,
        "extended": "0",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(API_URL, params=params)
    if response.status_code != 200:
        raise RuntimeError(f"vk wall.getById HTTP {response.status_code}")
    payload = response.json()
    if isinstance(payload, dict) and payload.get("error"):
        code = payload["error"].get("error_code")
        raise RuntimeError(f"vk wall.getById error {code}")
    return _items(payload)


def _metric(item: dict[str, Any], key: str) -> int:
    block = item.get(key)
    if isinstance(block, dict):
        return int(block.get("count") or 0)
    if isinstance(block, int):
        return block
    return 0


async def collect_and_record(*, days: int = 14, limit: int = 100) -> dict[str, Any]:
    """Read VK metrics for recent posts and append one snapshot per post.

    ``status`` is one of ``blocked`` (a precondition is missing or VK rejects
    the token), ``collected`` (at least one snapshot written), ``failed`` (fetch
    errors and no snapshots written) or ``empty`` (nothing to fetch or a successful
    read with nothing to write). ``reason`` explains zero-snapshot outcomes so an
    operator never has to guess whether zero means failure.
    """
    from tgfarm.s8_analytics import record_post_stat

    settings = get_settings()
    # A user token is the only supported reader for wall metrics: group tokens
    # are rejected by VK with error 27. The publisher token (wall,photos,groups,
    # offline) works for posting but must not be assumed to be a user token.
    token = str(getattr(settings, "vk_user_token", "") or "").strip()
    owner = _owner_id(str(getattr(settings, "vk_group_id", "") or ""))
    if not getattr(settings, "live", False):
        return {
            "status": "blocked",
            "checked": 0,
            "saved": 0,
            "reason": "режим не live: метрики ВК не собираются",
            "skipped": "vk metrics are only collected in live mode",
        }
    if not token or not owner:
        missing = ", ".join(
            name
            for name, present in (("VK_USER_TOKEN", bool(token)), ("VK_GROUP_ID", bool(owner)))
            if not present
        )
        return {
            "status": "blocked",
            "checked": 0,
            "saved": 0,
            "reason": f"нужен {missing}: групповой токен ВК читает wall.getById с ошибкой 27",
            "skipped": "vk metrics need VK_USER_TOKEN (a user token with the wall scope)",
        }

    posts = recent_vk_posts(days=days, limit=limit)
    attributable = attributable_vk_posts(days=days, limit=limit)
    if not posts:
        return {
            "status": "empty",
            "checked": 0,
            "saved": 0,
            "attributable": attributable,
            "reason": (
                f"за последние {days} дней нет опубликованных записей ВК с remote id "
                f"(сопоставимых публикаций в базе: {attributable})"
            ),
            "skipped": "no recent vk publications",
        }
    by_remote = {remote: public_id for public_id, remote in posts}
    wall_ids = [f"{owner}_{remote}" for remote in by_remote]
    saved = 0
    checked = 0
    unattributed = 0
    errors: list[str] = []
    for start in range(0, len(wall_ids), BATCH_SIZE):
        chunk = wall_ids[start : start + BATCH_SIZE]
        try:
            items = await _fetch(chunk, token=token)
        except Exception as exc:  # noqa: BLE001 - metrics must not break the cycle
            message = str(exc)
            errors.append(f"{type(exc).__name__}: {redact_sensitive_text(exc, limit=120)}")
            if "error 27" in message:
                return {
                    "status": "collected" if saved else "blocked",
                    "checked": checked,
                    "saved": saved,
                    "unattributed": unattributed,
                    "attributable": attributable,
                    "planned": len(wall_ids),
                    "errors": errors,
                    "reason": (
                        "ВК отклонил токен ошибкой 27: wall.getById читается только "
                        "пользовательским токеном (VK_USER_TOKEN), групповой не может"
                    ),
                    "skipped": (
                        "vk rejected the token with error 27: wall metrics need a "
                        "user token (VK_USER_TOKEN), a group token cannot read them"
                    ),
                }
            continue
        for item in items:
            checked += 1
            raw_id = str(item.get("id") or "")
            public_id = by_remote.get(raw_id)
            if not public_id:
                # The wall answered with a post we do not own the id of. Guessing
                # from the text would attach someone else's numbers to our post.
                unattributed += 1
                continue
            ok = record_post_stat(
                public_id,
                views=_metric(item, "views"),
                reactions=_metric(item, "likes"),
                comments=_metric(item, "comments"),
                forwards=_metric(item, "reposts"),
            )
            saved += int(bool(ok))
    if errors:
        log.warning("s8: vk metrics partially failed: %s", "; ".join(errors[:3]))
    log.info("s8: vk metrics checked=%d saved=%d unattributed=%d", checked, saved, unattributed)
    payload: dict[str, Any] = {
        "status": "collected" if saved else ("failed" if errors else "empty"),
        "checked": checked,
        "saved": saved,
        "unattributed": unattributed,
        "attributable": attributable,
        "planned": len(wall_ids),
        "errors": errors,
    }
    if not saved:
        if errors:
            payload["reason"] = (
                f"сбор метрик ВК не выполнился: {'; '.join(errors[:3])}; снимков не записано"
            )
        else:
            payload["reason"] = (
                f"ВК вернул {checked} записей, из них {unattributed} не сопоставлены с "
                f"нашими публикациями; снимков не записано"
            )
    return payload


__all__ = ["attributable_vk_posts", "collect_and_record", "recent_vk_posts"]
