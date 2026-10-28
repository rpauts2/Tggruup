"""Topic dossiers: connect publication history with recent source evidence.

This module is intentionally conservative. It detects likely updates and
possible numeric changes, but does not claim that a number is wrong solely
because a newer source contains a different number. A human or a real fact
checker must resolve metric/context differences.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Iterable

from sqlalchemy import or_, select

from tgfarm.core.text import content_tokens, normalize
from tgfarm.db.models import RawItem
from tgfarm.db.session import session_scope
from tgfarm.s4_generation.memory import _query_terms, search_archive

_NUMBER_RE = re.compile(r"(?<![\w])\d+(?:[.,]\d+)*(?![\w])")
MAX_CURRENT_CANDIDATES = 600
MAX_CURRENT_RESULTS = 16
MAX_CLAIM_REVIEW = 40


def _as_utc(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value) -> str | None:
    parsed = _as_utc(value)
    return parsed.isoformat(timespec="seconds") if parsed else None


def _number_set(value: str) -> set[str]:
    return {match.replace(",", ".").rstrip(".") for match in _NUMBER_RE.findall(value or "")}


def _topic_score(query_terms: list[str], text: str) -> float:
    if not query_terms:
        return 0.0
    tokens = set(content_tokens(text or ""))
    prefixes = {token[:5] for token in tokens if len(token) >= 5}
    found = sum(
        1 for term in query_terms
        if term in tokens or (len(term) >= 5 and term[:5] in prefixes)
    )
    denominator = min(len(query_terms), 8)
    return round(found / denominator, 4) if denominator else 0.0


def _source_payload(item, query_terms: list[str]) -> dict:
    title = str(getattr(item, "title", "") or "")
    body = str(getattr(item, "text", "") or "")
    published = getattr(item, "published_at", None)
    fetched = getattr(item, "fetched_at", None)
    effective_date = published or fetched
    return {
        "title": title[:300],
        "text": body,
        "excerpt": body[:520].strip(),
        "url": str(getattr(item, "url", "") or ""),
        "source_id": str(getattr(item, "source_id", "") or "unknown"),
        "published_at": _iso(published),
        "fetched_at": _iso(fetched),
        "date": _iso(effective_date),
        "score": _topic_score(query_terms, f"{title} {body[:1800]}"),
    }


def _find_current_sources(
    query: str,
    query_terms: list[str],
    *,
    days: int,
    now: datetime,
    limit: int = MAX_CURRENT_RESULTS,
) -> list[dict]:
    if not query_terms:
        return []
    from tgfarm.db.models import RawItem

    cutoff = now - timedelta(days=max(1, min(180, int(days))))
    clauses = []
    for term in query_terms[:10]:
        pattern = f"%{term}%"
        clauses.extend([RawItem.title.ilike(pattern), RawItem.text.ilike(pattern)])
    if not clauses:
        return []

    with session_scope() as session:
        rows = session.execute(
            select(RawItem)
            .where(RawItem.fetched_at >= cutoff, or_(*clauses))
            .order_by(RawItem.fetched_at.desc())
            .limit(MAX_CURRENT_CANDIDATES)
        ).scalars().all()

    candidates = []
    seen_urls: set[str] = set()
    for item in rows:
        source = _source_payload(item, query_terms)
        if source["score"] < 0.12:
            continue
        dedupe_key = source["url"] or f"{source['source_id']}:{source['title']}"
        if dedupe_key in seen_urls:
            continue
        seen_urls.add(dedupe_key)
        candidates.append(source)

    candidates.sort(key=lambda row: (row["score"], row["date"] or ""), reverse=True)
    return candidates[:max(1, min(40, int(limit)))]


def _current_sources_from_items(items: Iterable, query_terms: list[str], limit: int) -> list[dict]:
    candidates = []
    seen_urls: set[str] = set()
    for item in items or []:
        source = _source_payload(item, query_terms)
        if source["score"] < 0.08:
            continue
        dedupe_key = source["url"] or f"{source['source_id']}:{source['title']}"
        if dedupe_key in seen_urls:
            continue
        seen_urls.add(dedupe_key)
        candidates.append(source)
    candidates.sort(key=lambda row: (row["score"], row["date"] or ""), reverse=True)
    return candidates[:max(1, min(40, int(limit)))]


def _claim_review(claim: str, sources: list[dict]) -> tuple[str, str, float, list[str]]:
    claim = str(claim or "").strip()
    claim_terms = content_tokens(claim)
    if not claim or not sources:
        return "not_rechecked", "Нет свежих материалов для перепроверки.", 0.0, []

    best_score = 0.0
    best_source = None
    for source in sources:
        score = _topic_score(claim_terms[:16], f"{source.get('title', '')} {source.get('text', '')[:2200]}")
        if score > best_score:
            best_score = score
            best_source = source
    if best_source is None or best_score < 0.20:
        return "not_rechecked", "В свежих материалах не найдено достаточно похожего контекста.", best_score, []

    old_numbers = _number_set(claim)
    new_numbers = _number_set(f"{best_source.get('title', '')} {best_source.get('text', '')}")
    source_url = [best_source.get("url")] if best_source.get("url") else []
    if old_numbers:
        same = sorted(old_numbers & new_numbers)
        if same:
            return (
                "reappears",
                "Часть числовых значений встречается в свежем материале; это не заменяет проверку смысла и единиц измерения.",
                best_score,
                source_url,
            )
        if new_numbers and best_score >= 0.30:
            return (
                "possible_change",
                "В похожем свежем контексте встречаются другие числа. Возможное изменение, другая метрика или другой период: требуется ручная проверка.",
                best_score,
                source_url,
            )
        return "not_rechecked", "Текущий источник не подтвердил старые числа.", best_score, source_url

    if best_score >= 0.40:
        return (
            "reappears",
            "Похожие утверждения встречаются в свежем материале; это предварительное текстовое совпадение, не независимая проверка истины.",
            best_score,
            source_url,
        )
    return "not_rechecked", "Совпадение недостаточно сильное для перепроверки утверждения.", best_score, source_url


def _claim_history(posts: list[dict], current_sources: list[dict], limit: int = MAX_CLAIM_REVIEW) -> list[dict]:
    seen: set[str] = set()
    review = []
    for post in sorted(posts, key=lambda row: row.get("published_at") or row.get("created_at") or "", reverse=True):
        for claim in post.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            claim_text = str(claim.get("claim") or "").strip()
            key = normalize(claim_text)
            if not key or key in seen:
                continue
            seen.add(key)
            status, note, score, refs = _claim_review(claim_text, current_sources)
            review.append({
                "claim": claim_text[:500],
                "post_id": post["id"],
                "post_hook": post["hook"],
                "channel_id": post["channel_id"],
                "published_at": post.get("published_at"),
                "historically_verified": bool(claim.get("verified", False)),
                "current_status": status,
                "current_note": note,
                "evidence_score": score,
                "current_sources": refs,
            })
            if len(review) >= max(1, min(MAX_CLAIM_REVIEW, limit)):
                return review
    return review


def _latest_time(posts: list[dict]) -> datetime | None:
    values = [_as_utc(post.get("published_at") or post.get("created_at")) for post in posts]
    return max((value for value in values if value is not None), default=None)


def _search_topic_archive(
    query: str,
    *,
    channel_id: str | None,
    preferred_channel_id: str | None,
    limit: int,
) -> dict:
    """Page through the ranked FTS candidate window instead of silently reading only its first page."""
    cap = max(1, min(300, int(limit)))
    rows = []
    offset = 0
    total = 0
    while offset < cap:
        page_limit = min(100, cap - offset)
        page = search_archive(
            query,
            channel_id=channel_id,
            preferred_channel_id=preferred_channel_id,
            limit=page_limit,
            offset=offset,
        )
        total = int(page.get("total", 0))
        batch = page.get("results") or []
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        # search_archive ranks up to MAX_CANDIDATES candidates; avoid futile pages.
        if len(batch) < page_limit or offset >= min(total, 300):
            break
    return {
        "total": total,
        "results": rows,
        "scanned": len(rows),
        "truncated": total > len(rows),
    }


def build_topic_dossier(
    query: str,
    *,
    channel_id: str | None = None,
    preferred_channel_id: str | None = None,
    days: int = 30,
    archive_limit: int = 300,
    current_items: Iterable | None = None,
    now: datetime | None = None,
) -> dict:
    """Assemble a chronology, current source updates, claim checks, and next-story brief."""
    query = (query or "").strip()[:500]
    current_time = _as_utc(now or datetime.now(UTC))
    assert current_time is not None
    window_days = max(1, min(180, int(days)))
    terms = _query_terms(query)
    if len(query) < 2 or not terms:
        return {
            "query": query,
            "status": "query_too_short",
            "window_days": window_days,
            "timeline": [],
            "current_sources": [],
            "claim_checks": [],
            "continuation_ideas": [],
            "brief": "",
            "message": "Введи более конкретную тему, название или имя.",
        }

    archive = _search_topic_archive(
        query,
        channel_id=channel_id,
        preferred_channel_id=preferred_channel_id or channel_id,
        limit=archive_limit,
    )
    historical_posts = [
        row for row in archive.get("results", [])
        if row.get("score", 0.0) >= 0.08
    ]
    historical_posts.sort(
        key=lambda row: row.get("published_at") or row.get("created_at") or ""
    )
    latest_post_time = _latest_time(historical_posts)

    if current_items is None:
        current_sources = _find_current_sources(
            query, terms, days=window_days, now=current_time
        )
    else:
        current_sources = _current_sources_from_items(current_items, terms, MAX_CURRENT_RESULTS)

    for source in current_sources:
        source_time = _as_utc(source.get("date"))
        source["is_new_since_last_post"] = bool(
            source_time and (latest_post_time is None or source_time > latest_post_time)
        )

    new_sources = [source for source in current_sources if source["is_new_since_last_post"]]
    angles = Counter(str(row.get("angle") or "unknown") for row in historical_posts)
    covered_angles = [{"angle": key, "posts": value} for key, value in angles.most_common()]
    latest_post = historical_posts[-1] if historical_posts else None
    days_since_latest = (
        round((current_time - latest_post_time).total_seconds() / 86400, 1)
        if latest_post_time is not None else None
    )

    claim_checks = _claim_history(historical_posts, current_sources)
    continuation_ideas = []
    if new_sources:
        for source in new_sources[:4]:
            continuation_ideas.append({
                "kind": "update",
                "title": f"Что изменилось: {source['title'] or query}",
                "reason": "Источник найден позже последней подходящей публикации в архиве.",
                "source_url": source.get("url"),
            })
    if any(item["current_status"] == "possible_change" for item in claim_checks):
        continuation_ideas.append({
            "kind": "verify",
            "title": "Перепроверить цифры и условия",
            "reason": "В похожем свежем контексте обнаружены другие числовые значения; сначала проверь, относятся ли они к той же метрике и периоду.",
            "source_url": None,
        })
    if historical_posts and current_sources:
        continuation_ideas.extend([
            {
                "kind": "impact",
                "title": "Что это меняет для читателя",
                "reason": "Сопоставь новое подтверждённое событие с выводами прошлых материалов и не повторяй старую вводную.",
                "source_url": new_sources[0].get("url") if new_sources else current_sources[0].get("url"),
            },
            {
                "kind": "open_questions",
                "title": "Что пока неизвестно",
                "reason": "Раздели подтверждённое текущими источниками и вопросы, на которые данных ещё нет.",
                "source_url": current_sources[0].get("url"),
            },
        ])
    elif not historical_posts:
        continuation_ideas.append({
            "kind": "first_coverage",
            "title": "Первое освещение темы",
            "reason": "В архиве не найдено достаточно близких опубликованных материалов.",
            "source_url": current_sources[0].get("url") if current_sources else None,
        })
    else:
        continuation_ideas.append({
            "kind": "refresh",
            "title": "Проверить, появилось ли продолжение",
            "reason": "В выбранном окне не найдено достаточно свежих материалов, чтобы утверждать, что произошли изменения.",
            "source_url": None,
        })

    timeline = []
    for post in historical_posts:
        timeline.append({
            "id": post["id"],
            "date": post.get("published_at") or post.get("created_at"),
            "published_at": post.get("published_at"),
            "created_at": post.get("created_at"),
            "channel_id": post["channel_id"],
            "channel_title": post.get("channel_title") or post["channel_id"],
            "hook": post.get("hook") or "",
            "angle": post.get("angle") or "unknown",
            "post_format": post.get("post_format") or "unknown",
            "sources_count": len(post.get("sources") or []),
            "claims_count": len(post.get("claims") or []),
            "claims": [
                str(claim.get("claim") or "")[:180]
                for claim in (post.get("claims") or [])
                if isinstance(claim, dict) and claim.get("claim")
            ][:4],
        })

    brief_lines = [
        f"ДОСЬЕ ТЕМЫ: {query}",
        "История публикаций ниже показывает, что уже освещалось, а не доказывает сегодняшние факты.",
    ]
    if timeline:
        brief_lines.append("ХРОНОЛОГИЯ:")
        for post in timeline[-8:]:
            brief_lines.append(
                f"- {post.get('date')}: [{post['channel_title']}] {post['hook']} "
                f"(угол={post['angle']}, формат={post['post_format']})"
            )
    if new_sources:
        brief_lines.append("НОВЫЕ ИСТОЧНИКИ ПОСЛЕ ПОСЛЕДНЕГО ПОСТА:")
        for source in new_sources[:6]:
            brief_lines.append(
                f"- {source.get('date')}: {source['title']} | {source.get('url') or 'URL не указан'}"
            )
            if source.get("excerpt"):
                brief_lines.append(f"  Фрагмент: {source['excerpt'][:280]}")
    if claim_checks:
        brief_lines.append("СТАРЫЕ УТВЕРЖДЕНИЯ ДЛЯ ПЕРЕПРОВЕРКИ:")
        for item in claim_checks[:8]:
            brief_lines.append(
                f"- [{item['current_status']}] {item['claim']} — {item['current_note']}"
            )
    brief_lines.extend([
        "ТРЕБОВАНИЯ К ПРОДОЛЖЕНИЮ:",
        "- Не пересказывай прошлый пост; начни с подтверждённого изменения или полезного нового вопроса.",
        "- Используй старую хронологию только как контекст. Не считай старые цифры актуальными без повторной проверки.",
        "- Новые утверждения опирай только на текущий блок ИСТОЧНИКИ; если доказательств недостаточно, явно обозначь неопределённость.",
        "- Числа в источниках могут относиться к разным метрикам/периодам: не объявляй изменение фактом без проверки сопоставимости.",
        "- Если новых фактов нет, не выдумывай обновление ради публикации.",
        "Предлагаемые направления продолжения: " + "; ".join(item["title"] for item in continuation_ideas[:6]),
    ])

    limitations = [
        "Это эвристический поиск, не доказательство истинности или причинности.",
        "Числовые расхождения требуют проверки метрики, валюты, единиц и периода.",
        "Источники ограничены материалами, сохранёнными в БД за выбранное окно.",
    ]
    if archive.get("truncated"):
        limitations.append("По запросу найдено больше результатов, чем включено в это досье; сузь тему или увеличь лимит, чтобы изучить следующие материалы.")

    return {
        "query": query,
        "status": "ok",
        "window_days": window_days,
        "archive_total_matches": archive.get("total", 0),
        "archive_scanned": archive.get("scanned", 0),
        "history_truncated": bool(archive.get("truncated", False)),
        "history_count": len(timeline),
        "latest_post_at": _iso(latest_post_time),
        "days_since_latest_post": days_since_latest,
        "covered_angles": covered_angles,
        "updates_found": len(new_sources),
        "current_source_count": len(current_sources),
        "timeline": timeline[-max(1, min(100, int(archive_limit))):],
        "current_sources": [
            {key: value for key, value in source.items() if key != "text"}
            for source in current_sources
        ],
        "claim_checks": claim_checks,
        "continuation_ideas": continuation_ideas,
        "brief": "\n".join(brief_lines),
        "limitations": limitations,
    }


def build_topic_context(items: list, channel_id: str, *, archive_limit: int = 8) -> str:
    """Build topic-chain context for S4 from current source items and its own archive."""
    titles = [str(getattr(item, "title", "") or "") for item in (items or [])[:5]]
    query = " ".join(title for title in titles if title)
    if not query:
        return ""
    dossier = build_topic_dossier(
        query,
        preferred_channel_id=channel_id,
        days=30,
        archive_limit=archive_limit,
        current_items=items,
    )
    if dossier["history_count"] == 0:
        return ""
    return dossier["brief"]
