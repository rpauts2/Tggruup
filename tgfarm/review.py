"""Operator reconciliation board: what actually needs a human right now.

The farm can hold hundreds of posts in states that require a decision —
``publish_unknown`` after a lost outcome, terminal ``failed`` after an exhausted
retry budget, and ``gated`` posts that passed the gates but cannot be scheduled
(approval mode, an offline Dzen draft, or a saturated slot table). The panel had
no single place that answers "what needs me now, and what exactly can I do".

This module is presentation-only: it never changes state, it only reports the
bucket, the reason and the actions that the existing endpoints already support.

Two guarantees the operator can rely on, because every reason on this board is
untrusted platform text: a row is never rendered with an empty reason, and a
stored traceback or Playwright call log is never pasted into a card — it is
redacted (no credential survives the cut) and collapsed to the one line that
says why.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from tgfarm.core.security import redact_sensitive_text
from tgfarm.core.settings import get_settings
from tgfarm.db.models import Channel, Post
from tgfarm.db.session import session_scope

#: Actions per bucket, each backed by an existing API endpoint.
BUCKET_ACTIONS: dict[str, tuple[str, ...]] = {
    # POST /api/posts/{id}/reconcile: action=confirm_published | retry_after_check
    "unknown": ("confirm_published", "retry_after_check", "reject"),
    # POST /api/posts/{id}/retry or /api/posts/reject
    "terminal": ("retry", "reject"),
    # POST /api/posts/publish (immediate) or /api/posts/reject
    "held": ("publish_now", "reject"),
}

BUCKET_TITLES = {
    "unknown": "Неизвестный результат публикации",
    "terminal": "Финальный отказ",
    "held": "Готово, но без слота",
}


def _age_hours(value: datetime | None, now: datetime) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return round((now - value).total_seconds() / 3600, 1)


#: Lines that are log noise rather than a reason: Playwright's "Call log" block,
#: Python traceback frames, RST-style separators and anything indented (an echoed
#: source line or a `  - navigating to ...` bullet). The exception itself and a
#: driver's one-line error are never indented, so they survive.
_LOG_NOISE = re.compile(
    r"(?i)^(?:\s|call log\b|traceback \(most recent call last\)|stack ?trace\b"
    r"|during handling of|the above exception was|file \"|\^+|-{3,}|={3,})"
)


def _cause(value: object, *, limit: int) -> str:
    """One redacted line out of a stored provider error, with the cause kept.

    `publish_error` is untrusted platform text: it can carry a credential-bearing
    URL, a whole traceback, or Playwright's call log. Redaction runs before any
    slicing, so a cut can never leave half a token on the board. When the joined
    text would not fit, the last surviving line wins — in both a traceback and a
    driver dump the real exception is the last one.
    """
    text = redact_sensitive_text(value, limit=2000)
    kept = [line.strip() for line in text.splitlines() if line.strip() and not _LOG_NOISE.match(line)]
    if not kept:
        # A pure-noise error must still not render as an empty cell.
        return " ".join(text.split()).strip()[:limit].rstrip()
    one = " ".join(kept)
    if len(one) > limit and len(kept) > 1:
        one = kept[-1]
    return one[:limit].rstrip()


def _held_reason(post: Post, platform: str, settings: Any) -> str:
    if getattr(settings, "approval_required", False):
        return "Ждёт одобрения оператора: включён режим TGFARM_APPROVAL_REQUIRED."
    if (
        platform == "dzen"
        and getattr(settings, "dzen_requires_llm", False)
        and str(post.generation_provider or "") == "offline"
    ):
        return (
            "Офлайн-черновик Дзена: публикация запрещена политикой DZEN_REQUIRES_LLM, "
            "нужна перегенерация онлайн-моделью."
        )
    return "Прошёл гейты качества, но слот ещё не выделен (лимит площадки или календарь)."


def _terminal_reason(post: Post) -> str:
    error = str(post.publish_error or "").strip()
    if not error:
        return "Публикация окончательно не удалась; причина не записана."
    # Our own machine-readable close-outs ("expired: …") are already the reason;
    # raw provider errors get a Russian framing so the board reads like a
    # decision list, not a log dump. Either way the text is one redacted line:
    # a close-out can ride along with a Playwright call log behind it, and that
    # tail is neither bounded nor actionable.
    if error.startswith("expired:"):
        return _cause(error, limit=260) or "Публикация окончательно не удалась; причина не записана."
    return f"Публикация не состоялась (финальный отказ): {_cause(error, limit=260)}"


def _item(post: Post, platform: str, bucket: str, reason: str, now: datetime) -> dict[str, Any]:
    return {
        "public_id": post.public_id,
        "platform": platform or "unknown",
        "channel_id": post.channel_id,
        "hook": (post.hook or post.body or "")[:160],
        "status": post.status,
        "bucket": bucket,
        "attempts": int(post.attempts or 0),
        "reason": reason,
        "age_hours": _age_hours(post.updated_at or post.created_at, now),
        "published_at": post.published_at.isoformat() if post.published_at else None,
        "actions": list(BUCKET_ACTIONS[bucket]),
    }


def review_queue(*, limit: int = 25) -> dict[str, Any]:
    """Three triage buckets with reasons, oldest-first inside each bucket."""
    limit = max(1, min(int(limit), 200))
    settings = get_settings()
    now = datetime.now(UTC)
    buckets: dict[str, list[dict[str, Any]]] = {"unknown": [], "terminal": [], "held": []}
    counts = {"unknown": 0, "terminal": 0, "held": 0}
    with session_scope() as session:
        rows = session.execute(
            select(Post, Channel.platform)
            .select_from(Post)
            .join(Channel, Channel.id == Post.channel_id, isouter=True)
            .where(Post.status.in_(("publish_unknown", "failed", "gated")))
        ).all()
    for post, platform in rows:
        name = str(platform or "").lower()
        if post.status == "publish_unknown":
            bucket = "unknown"
            reason = (
                "Результат отправки неизвестен: проверь площадку перед повтором "
                "(автоматический повтор запрещён, чтобы не создать дубль)."
            )
            # Rows the publication-evidence repair moved here carry their own
            # English note ("history: marked published with no remote evidence…")
            # and a NULL `published_at`; both belong on the board, one line each.
            cause = _cause(post.publish_error, limit=200).strip() if post.publish_error else ""
            if cause:
                reason = f"{reason} {cause}"
        elif post.status == "failed":
            bucket = "terminal"
            reason = _terminal_reason(post)
        else:
            bucket = "held"
            reason = _held_reason(post, name, settings)
        counts[bucket] += 1
        buckets[bucket].append(_item(post, name, bucket, reason, now))

    for bucket, items in buckets.items():
        if bucket == "terminal":
            items.sort(key=lambda entry: entry["age_hours"] or 0)  # newest failures first
        else:
            items.sort(key=lambda entry: -(entry["age_hours"] or 0))  # oldest rot first
    return {
        "generated_at": now.isoformat(),
        "total": sum(counts.values()),
        "counts": counts,
        "limits": {"per_bucket": limit},
        "titles": BUCKET_TITLES,
        "buckets": {name: items[:limit] for name, items in buckets.items()},
    }


__all__ = ["BUCKET_ACTIONS", "BUCKET_TITLES", "review_queue"]
