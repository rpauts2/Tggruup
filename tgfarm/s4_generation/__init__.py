"""S4 — content generation and fact grounding.

Two LLM calls per post:
  1. draft  — persona-aware writing from the cluster's sources
  2. verify — every factual claim checked against the sources, producing the
              `claims` list that the quality gate and the `sources` block both
              depend on.

If the LLM chain is unavailable the deterministic composer produces a valid,
sourced post so the farm never stalls.
"""

from __future__ import annotations

import random
import re
import time
import uuid
from dataclasses import replace
from datetime import date, datetime
from typing import Any

from sqlalchemy import select, update

from tgfarm.config import Persona
from tgfarm.core.errors import ProviderUnavailable
from tgfarm.core.logging import get_logger
from tgfarm.core.run import StageTimer, ensure_utc, to_thread
from tgfarm.core.schemas import Claim, DraftPost, ItemScored
from tgfarm.core.schemas import Cluster as ClusterSchema
from tgfarm.core.settings import get_settings
from tgfarm.core.text import detect_lang, truncate
from tgfarm.db.models import Channel as ChannelRow
from tgfarm.db.models import Cluster as ClusterRow
from tgfarm.db.models import Media as MediaRow
from tgfarm.db.models import Post as PostRow
from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.session import session_scope
from tgfarm.db.vector import pack
from tgfarm.editorial_plan import MAX_ENTRIES
from tgfarm.llm import policy
from tgfarm.llm.client import (
    OFFLINE_MODEL,
    OFFLINE_PROVIDER,
    LLMResponse,
    compose_offline_post,
    get_llm,
)
from tgfarm.llm.embedding import get_embedder
from tgfarm.s3_router import ChannelPlan
from tgfarm.s4_generation.memory import DEFAULT_DUPLICATE_SIMILARITY, duplicate_threshold

log = get_logger(__name__)

# How many cluster items may be probed for an attached video. Items whose feed
# entry already carried video are tried first, so this budget only pays for
# article-page fetches when nothing was declared in the feed.
MAX_VIDEO_PROBES = 3

ANGLE_HINTS = {
    "news": "Суть новости: что произошло, кто это сделал, цифры.",
    "analysis": "Разбор: что это значит и почему произошло именно так.",
    "why_it_matters": "Влияние на читателя: что ему делать или знать.",
    "checklist": "Практический список действий или критериев.",
    "comparison": "Сравнение вариантов с явными выводами.",
    "history": "Краткая история: как пришли к текущему состоянию.",
    "listicle": "Информационный топ/список: 5-10 пунктов с краткими пояснениями и источниками.",
}

_DRAFT_SYSTEM = (
    "Ты — автор канала. Пишешь для своей аудитории, не для алгоритма.\n"
    "ЖЁСТКИЕ ПРАВИЛА:\n"
    "1. Только факты из блока ИСТОЧНИКИ. Ничего не додумывай.\n"
    "2. Никаких кликбейта и рекламных превосходных степеней.\n"
    "3. Каждый абзац — одна мысль, 1–3 предложения.\n"
    "4. Пиши на языке persona.language.\n"
    "5. Источники копируй из входа, не выдумывай URL.\n"
    "6. Статья должна быть профессиональной, информативной и содержательной, с лёгким уместным юмором.\n"
    "7. Соблюдай формат и длину канала; не пиши слишком коротко.\n"
    "8. Если платформа Дзен — пиши развёрнутую статью из 5-7 абзацев: что произошло, цифры, контекст, почему это важно читателю, что дальше, источники.\n9. Допустимы информационные материалы-списки (топ-5/топ-10), обзоры и гиды, если они опираются на источники.\n"
    "Ответ — ТОЛЬКО JSON: "
    '{"hook": "...", "body": "...", "hashtags": ["..."], "cta": null}'
)

_VERIFY_SYSTEM = (
    "Ты — фактчекер. Тебе дают текст поста и список источников.\n"
    "Разбей текст на отдельные проверяемые утверждения. Для каждого укажи "
    "источник, который его подтверждает, либо поставь verified=false.\n"
    "Ответ — ТОЛЬКО JSON: "
    '{"claims": [{"claim": "...", "source_url": "...", "verified": true}]}'
)


def _rank_learning_mistakes(mistakes: list[dict], limit: int = 5) -> list[dict]:
    """Most-repeated lessons first; `updated_at` only breaks a genuine tie.

    The lessons come back in whatever order the database stored them, so slicing
    straight to five could drop the gate that rejects most often.
    """

    def rank(item: dict) -> tuple[int, str]:
        try:
            count = max(0, int(item.get("sample_size") or 0))
        except (TypeError, ValueError):
            count = 0
        return count, str(item.get("updated_at") or "")

    return sorted(mistakes, key=rank, reverse=True)[: max(0, int(limit))]


def _persona_block(persona: Persona, angle: str, plan=None) -> str:
    max_chars = plan.max_chars if plan else persona.max_chars
    min_chars = plan.min_chars if plan else persona.min_chars
    platform = {"dzen": "Дзен", "vk": "ВКонтакте"}.get(persona.platform, "Telegram")
    lines = [
        f"ИМЯ КАНАЛА: {persona.title}",
        f"ПЛАТФОРМА: {platform}",
        f"НИША: {persona.niche}",
        f"ЯЗЫК: {persona.language}",
        f"АУДИТОРИЯ: {persona.audience}",
        f"ТОН: {', '.join(persona.tone) or 'factual'}",
        f"ФОРМАТ ПОСТА: {plan.fmt if plan else 'news'}"
        + (f" ({plan.reason})" if plan and plan.reason else ""),
        f"ДЛИНА: {min_chars}–{max_chars} символов",
        f"УГОЛ: {angle} — {ANGLE_HINTS.get(angle, '')}",
        f"ЗАПРЕЩЁННЫЕ СЛОВА: {', '.join(persona.banned_words) if persona.banned_words else 'нет'}",
    ]
    if plan and plan.outline:
        lines.append("СТРУКТУРА:")
        lines.extend(f"- {step}" for step in plan.outline)
    if persona.style_notes:
        lines.append("ПРАВИЛА СТИЛЯ:")
        lines.extend(f"- {note}" for note in persona.style_notes)
    if persona.disclaimer:
        lines.append(f"ОБЯЗАТЕЛЬНАЯ ОГОВОРКА В КОНЦЕ: {persona.disclaimer}")
    try:
        from tgfarm.s12_learning import lessons as _lessons
        tactics = _lessons(persona.id, kind="tactic")
        mistakes_list = _rank_learning_mistakes(_lessons(persona.id, kind="mistake"), limit=5)
        if tactics or mistakes_list:
            lines.append("ОПЫТ И ОШИБКИ (самообучение):")
            for t in tactics[:5]:
                lines.append(f"- {t.get('note') or t.get('key')}")
            for m in mistakes_list[:5]:
                lines.append(f"- Избегать: {m.get('note') or m.get('key')}")
    except Exception:
        pass
    return "\n".join(lines)


def _source_block(items: list[ItemScored]) -> str:
    lines = []
    for item in items[:5]:
        body = item.text[:1200].strip()
        lines.append(f"ЗАГОЛОВОК: {item.title}\nТЕКСТ: {body}\nURL: {item.url}")
    return "\n\n".join(lines)


HOOK_STYLE_HINTS = {
    "fact": "Хук — сухой факт без цифры, одним предложением.",
    "stat": "Хук строится вокруг цифры: число и что оно значит.",
    "question": "Хук — короткий вопрос по сути новости, без кликбейта.",
}

_HOOK_STYLE_RE = re.compile(r"\d+(?:[.,]\d+)*")

# Topics where a single joke costs the channel its face. Checked against the
# raw source titles/text; the prompt then bans humor for this post entirely.
SOMBER_MARKERS = (
    "погиб", "погибла", "погибли", "катастрофа", "теракт", "жертв",
    "скончался", "скончалась", "умер", "умерла", "похорон", "массовое убийство",
    "войн", "обстрел", "авария со смертям", "утонул", "утонула", "сгорел",
)


def _is_somber_topic(items: list[ItemScored]) -> bool:
    blob = " ".join(f"{i.title} {i.text[:400]}" for i in items[:5]).lower()
    return any(marker in blob for marker in SOMBER_MARKERS)


def classify_hook_style(hook: str) -> str:
    """Deterministic label of the hook pattern, for the reward loop."""
    text = (hook or "").strip()
    if text.endswith(("?", "?", "?")):
        return "question"
    if _HOOK_STYLE_RE.search(text):
        return "stat"
    return "fact"


def _pick_hook_style(channel_id: str, rng: random.Random | None = None) -> str:
    """Sample a hook style from the learned weights (bandit-lite).

    Weights start from the editorial prior and are nudged by measured quality
    in S12, the same way angles are; exploration comes from the sampling
    itself.
    """
    rng = rng or random.Random()
    try:
        from tgfarm.s12_learning import best_hook_style_weights

        learned = best_hook_style_weights(channel_id)
    except Exception:  # noqa: BLE001 - learning must not break generation
        learned = None
    weights = learned or {"fact": 0.55, "stat": 0.30, "question": 0.15}
    options = list(weights)
    total = sum(max(0.0, float(v)) for v in weights.values())
    if not options or total <= 0:
        return "fact"
    return rng.choices(options, weights=[max(0.0, float(weights[k])) for k in options], k=1)[0]


def _best_examples(channel_id: str, limit: int = 2) -> list[tuple[str, str]]:
    """The channel's best recent posts, for few-shot prompting.

    Examples carry the voice, never the facts: the prompt tells the model to
    borrow intonation and humor while content stays bound to the sources.
    Ranked by the mean of the quality-gate scores — deterministic, cheap, and
    available from the very first published post, unlike ER.
    """
    from sqlalchemy import select

    from tgfarm.db.session import session_scope

    with session_scope() as session:
        rows = session.execute(
            select(PostRow.hook, PostRow.body, PostRow.quality)
            .where(PostRow.channel_id == channel_id, PostRow.status == "published")
            .order_by(PostRow.created_at.desc())
            .limit(80)
        ).all()

    scored: list[tuple[float, str, str]] = []
    for hook, body, quality in rows:
        scores = (quality or {}).get("scores") or {}
        if not scores:
            continue
        mean = sum(float(v) for v in scores.values()) / len(scores)
        scored.append((mean, hook, body))
    scored.sort(key=lambda row: row[0], reverse=True)
    return [(hook, body) for _mean, hook, body in scored[:limit]]


def _examples_block(examples: list[tuple[str, str]]) -> str:
    if not examples:
        return ""
    lines = [
        "ПРИМЕРЫ ЛУЧШИХ ПОСТОВ ЭТОГО КАНАЛА (перенимай интонацию, ритм и уместность"
        " юмора; факты и содержание — только из блока ИСТОЧНИКИ):"
    ]
    for index, (hook, body) in enumerate(examples, 1):
        lines.append(f"{index}. {hook}")
        snippet = truncate(" ".join(body.split()), 320)
        if snippet:
            lines.append(snippet)
    return "\n".join(lines) + "\n\n"


def _to_scored(items) -> list[ItemScored]:
    out: list[ItemScored] = []
    for raw in items:
        if isinstance(raw, ItemScored):
            out.append(raw)
        else:
            out.append(
                ItemScored(
                    source_id=raw.source_id,
                    external_id=raw.external_id,
                    url=raw.url,
                    title=raw.title,
                    text=raw.text,
                    lang=raw.lang,
                    published_at=raw.published_at,
                    niche_hint=list(raw.niche_hint or []),
                    source_weight=raw.source_weight,
                )
            )
    return out


def normalise(payload: dict, persona: Persona, angle: str, items: list[ItemScored]) -> DraftPost:
    """Coerce whatever the model returned into a valid DraftPost."""
    hook = str(payload.get("hook") or "").strip()
    body = str(payload.get("body") or "").strip()

    if not hook and items:
        hook = items[0].title
    if not body and items:
        body = items[0].text[:600]

    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        seen: set[str] = set()
        sources = []
        for item in items[: persona.max_sources]:
            if item.url in seen:
                continue
            seen.add(item.url)
            sources.append({"title": item.title[:160], "url": item.url, "source": item.source_id})
    else:
        allowed = {item.url for item in items}
        cleaned = []
        for entry in sources[: persona.max_sources]:
            if isinstance(entry, dict) and entry.get("url") in allowed:
                cleaned.append(
                    {
                        "title": str(entry.get("title", ""))[:160],
                        "url": entry["url"],
                        "source": entry.get("source", "unknown"),
                    }
                )
        sources = cleaned or [
            {"title": items[0].title[:160], "url": items[0].url, "source": items[0].source_id}
        ]

    cta = payload.get("cta")
    if persona.cta_policy == "never":
        cta = None
    elif persona.cta_policy == "sometimes":
        cta = cta or persona.cta
    else:
        cta = cta or persona.cta

    if persona.disclaimer and persona.disclaimer.lower() not in body.lower():
        body = f"{body}\n\n{persona.disclaimer}".strip()

    tags = payload.get("hashtags")
    if not isinstance(tags, list) or not tags:
        tags = list(persona.hashtags)

    return DraftPost(
        hook=hook[:200],
        body=body,
        sources=sources,
        hashtags=[str(t) for t in tags],
        cta=str(cta) if cta else None,
        angle=angle,
        language=persona.language,
    )


async def generate(
    plan: ChannelPlan,
    raw_items: list,
    format_plan=None,
    *,
    video=None,
    strategy_enabled: bool = True,
    holdout_override: bool | None = None,
    examples_override: list[tuple[str, str]] | None = None,
    hook_style_override: str | None = None,
    usage_scope: str = "",
) -> DraftPost:
    """Write one post.

    `format_plan` (S10) decides the shape before a word is written; `video` is an
    allowed VideoRef when the story ships with licensed footage.
    """
    items = _to_scored(raw_items)
    persona = plan.persona

    llm = get_llm()
    offline = compose_offline_post(items, persona.as_dict(), plan.angle, format_plan)

    fmt = format_plan.fmt if format_plan else "news"
    max_chars = format_plan.max_chars if format_plan else persona.max_chars
    min_chars = format_plan.min_chars if format_plan else persona.min_chars

    structure_hint = ""
    if fmt == "article":
        structure_hint = (
            " Это ДЛИННАЯ СТАТЬЯ: используй подзаголовки, абзацы по 2-4 "
            "предложения, раздели текст на '\n\n'. Не пиши списки."
        )
    elif fmt == "stat":
        structure_hint = " Первое предложение — сама цифра и что она значит."
    elif fmt == "video":
        structure_hint = (
            " К видео: не пересказывай происходящее в ролике, дай контекст "
            "в 2-4 предложениях."
        )

    # Holdout bucket: a fixed share of drafts is written without the few-shot
    # examples, so the measurement loop can compare voices honestly.
    holdout = (
        bool(holdout_override)
        if holdout_override is not None
        else random.random() < max(0.0, min(1.0, get_settings().holdout_share))
    )
    examples = (
        list(examples_override)
        if examples_override is not None
        else [] if holdout else await to_thread(_best_examples, plan.channel_id)
    )
    hook_style = hook_style_override or _pick_hook_style(plan.channel_id)
    somber = _is_somber_topic(items)
    somber_line = (
        "ТЕМА ТРАГИЧЕСКАЯ: юмор и ирония ЗАПРЕЩЕНЫ в этом посте полностью. "
        "Тон — спокойный, точный, уважительный.\n" if somber else ""
    )
    prompt = (
        f"{_persona_block(persona, plan.angle, format_plan)}\n\n"
        f"{_examples_block(examples)}"
        f"{somber_line}"
        f"СТИЛЬ ХУКА: {HOOK_STYLE_HINTS.get(hook_style, HOOK_STYLE_HINTS['fact'])}\n"
        f"ИСТОЧНИКИ:\n{_source_block(items)}\n\n"
        f"Напиши пост формата «{fmt}».{structure_hint} "
        f"Хук — до 60 символов, конкретный. Общий объём {min_chars}–{max_chars} "
        f"символов (хук + тело). Хештеги — 3 штуки из списка persona или свои."
    )

    offline_chars = len(f"{offline.get('hook', '')} {offline.get('body', '')}")
    use_ai, reason = policy.should_use_ai(
        "s4_draft",
        angle=plan.angle,
        source_count=len({item.source_id for item in items}),
        offline_chars=offline_chars,
        persona=persona,
    )
    if use_ai:
        decision = await to_thread(
            policy.reserve,
            "s4_draft",
            purpose=fmt,
            cluster_public_id=usage_scope or plan.cluster_public_id,
        )
    else:
        decision = None
        await to_thread(
            policy.record_skip,
            "s4_draft",
            purpose=fmt,
            reason=reason,
            cluster_public_id=usage_scope or plan.cluster_public_id,
        )

    if decision is not None and decision.allowed:
        # Pull archive context only when an online draft will actually be requested.
        # The archive guides novelty, never factual grounding; S4 verification still
        # binds every factual claim to today's source block.
        editorial_strategy: dict = {"role": "editorial_strategist", "status": "skipped", "reason": "not_run"}
        try:
            from tgfarm.s4_generation.topic_memory import build_topic_context

            topic_memory = await to_thread(
                build_topic_context, items, plan.channel_id
            )
            if topic_memory:
                prompt = f"{prompt}\n\n{topic_memory}"
        except Exception as exc:  # noqa: BLE001 - memory must never stall generation
            log.warning("topic memory lookup failed: %s", exc)

        # The strategist is a separate, budgeted role. The benchmark control
        # arm omits this block while holding every other prompt input constant.
        if strategy_enabled:
            try:
                from tgfarm.agents.editorial import create_editorial_strategy, render_strategy

                editorial_strategy = await create_editorial_strategy(
                    items,
                    persona,
                    plan.angle,
                    fmt,
                    cluster_public_id=usage_scope or plan.cluster_public_id,
                    format_reason=(format_plan.reason if format_plan else ""),
                )
                strategy_block = render_strategy(editorial_strategy)
                if strategy_block:
                    prompt += f"\\n\\nОТДЕЛЬНЫЙ ПЛАН РЕДАКЦИОННОГО СТРАТЕГА:\\n{strategy_block}"
            except Exception as exc:  # noqa: BLE001 - optional agent must not stop generation
                editorial_strategy = {
                    "role": "editorial_strategist",
                    "status": "fallback",
                    "reason": f"Стратег недоступен ({type(exc).__name__}); использован базовый план.",
                }
                log.warning("editorial strategy failed: %s", type(exc).__name__)
        else:
            editorial_strategy = {
                "role": "editorial_strategist",
                "status": "disabled",
                "reason": "paired benchmark control arm",
            }

        payload, response = await llm.complete_json(
            _DRAFT_SYSTEM,
            prompt,
            # Thinking models spend budget before the JSON starts; a truncated
            # response falls to the offline composer and dies on the copyright
            # gate, so generosity here is cheaper than rejection.
            max_tokens=3500 if fmt == "article" else 2400,
            offline_fallback=offline,
        )
        await to_thread(
            policy.finalize,
            decision.usage_id,
            provider=response.provider,
            model=response.model,
            latency_ms=response.latency_ms,
            used=response.provider != "offline",
        )
    else:
        payload, response = offline, LLMResponse("", OFFLINE_PROVIDER, OFFLINE_MODEL, 0)

    if response.provider == OFFLINE_PROVIDER and not payload.get("hook"):
        raise ProviderUnavailable(
            f"offline composer has no {persona.language} source for "
            f"{plan.cluster_public_id}"
        )
    draft = normalise(payload, persona, plan.angle, items)
    # `LLMResponse` already guarantees the pair: an offline result carries
    # provider `offline` and model `none`, corrected in its constructor. Copying
    # it again here with a conditional was how two spellings for the same
    # unpublished fact could survive side by side.
    draft.generation_provider = response.provider
    draft.generation_model = response.model
    draft.hook_style = classify_hook_style(draft.hook)
    draft.variant = "holdout" if holdout else "a"
    draft.post_format = fmt
    draft.editorial_strategy = editorial_strategy if decision is not None and decision.allowed else {
        "role": "editorial_strategist", "status": "skipped", "reason": "online draft not selected"
    }

    verified = await verify(
        draft,
        items,
        generation_provider=draft.generation_provider,
        usage_scope=usage_scope,
    )
    draft.claims = verified

    log.info(
        "draft %s/%s format=%s angle=%s provider=%s editorial=%d total=%d claims=%d",
        plan.channel_id,
        plan.cluster_public_id,
        fmt,
        plan.angle,
        response.provider,
        draft.editorial_len,
        draft.char_len,
        len(verified),
    )
    return draft


async def verify(
    draft: DraftPost,
    items: list[ItemScored],
    *,
    generation_provider: str = "offline",
    usage_scope: str = "",
) -> list[Claim]:
    """Ask the model to bind each claim to a source; fall back to lexical binding."""
    allowed = {item.url: item for item in items}
    if not allowed:
        return []

    llm = get_llm()
    prompt = (
        f"ТЕКСТ ПОСТА:\n{draft.hook}\n{draft.body}\n\n"
        f"ДОСТУПНЫЕ ИСТОЧНИКИ:\n" + "\n".join(f"- {url}" for url in allowed)
    )

    fallback = {
        "claims": [
            {
                "claim": draft.hook[:200],
                "source_url": items[0].url,
                "verified": _lexically_supported(draft.hook, items[0]),
            }
        ]
    }
    use_ai, reason = policy.should_use_ai(
        "s4_verify", generation_provider=generation_provider
    )
    if not use_ai:
        await to_thread(
            policy.record_skip,
            "s4_verify",
            purpose="verify",
            reason=reason,
            cluster_public_id=usage_scope,
        )
        return _fallback_claims(draft, items)

    decision = await to_thread(
        policy.reserve,
        "s4_verify",
        purpose="verify",
        cluster_public_id=usage_scope,
    )
    if not decision.allowed:
        return _fallback_claims(draft, items)

    payload, response = await llm.complete_json(
        _VERIFY_SYSTEM, prompt, max_tokens=1600, offline_fallback=fallback
    )
    await to_thread(
        policy.finalize,
        decision.usage_id,
        provider=response.provider,
        model=response.model,
        latency_ms=response.latency_ms,
        used=response.provider != "offline",
    )

    claims: list[Claim] = []
    raw_claims = payload.get("claims")
    if not isinstance(raw_claims, list):
        return _fallback_claims(draft, items)

    for entry in raw_claims[:8]:
        if not isinstance(entry, dict):
            continue
        claim_text = str(entry.get("claim", "")).strip()
        url = str(entry.get("url") or entry.get("source_url") or "").strip()
        if not claim_text:
            continue
        # never trust a model-invented URL
        url = url if url in allowed else items[0].url
        verified = bool(entry.get("verified")) and _lexically_supported(claim_text, allowed[url])
        claims.append(Claim(claim=claim_text, source_url=url, verified=verified))

    # Independently audit every numeric sentence, even when the model omitted
    # it from its claim list. This also runs in offline/budget-limited mode.
    claims.extend(_numeric_audit_claims(draft, items))
    claims.extend(_date_audit_claims(draft, items))
    return _dedupe_claims(claims)


# A number followed by an optional unit. Deliberately strict about the decimal
# point (`\d+(?:[.,]\d+)*`) so a sentence-ending period is never swallowed into
# the token — "GPT-5.5." must normalise to "5.5", not "5.5.".
_NUMBER_RE = re.compile(
    r"\d+(?:(?:[ \u00a0]\d{3})|(?:[.,]\d+))*(?:\s*(?:%|млрд|млн|тыс|млрд\.|руб(?:\.|лей)?|RUB|USD|EUR|\$))?",
    re.I,
)
_DIGIT_STRIP = re.compile(r"[^\d]")


def _normalise_number(token: str) -> str:
    """Canonicalise a numeric token the same way the haystack is canonicalised.

    Both sides must go through the identical transform or nothing ever matches:
    the token drops its unit ("40% USD" -> "40"), and commas become dots because
    the RU/EU sources write decimal commas ("0,02" -> "0.02").
    """
    cleaned = (token or "").replace(" ", "").replace(" ", "")
    cleaned = re.sub(r"[^\d.,]", "", cleaned).rstrip(".,")
    if "," in cleaned and "." not in cleaned:
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+", cleaned):
            return cleaned.replace(",", "")
        return cleaned.replace(",", ".")
    return cleaned.replace(",", ".")


def _normalise_haystack(text: str) -> str:
    collapsed = re.sub(r"\s+", "", text or "").lower()
    return re.sub(r"(?<=\d),(?=\d)", ".", collapsed)


def _checkable_numbers(text: str) -> list[str]:
    """Canonical numeric values, excluding leading numbered-list markers only."""
    text = text or ""
    values: list[str] = []
    for match in _NUMBER_RE.finditer(text):
        digits = _normalise_number(match.group())
        if not digits:
            continue

        # Don't treat "1." or "12)" at the start of a line as a content claim.
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.start())
        if line_end < 0:
            line_end = len(text)
        line = text[line_start:line_end]
        marker = re.match(r"\s*(?:[-*•]\s*)?(\d+)[.)](?:\s+|$)", line)
        if marker:
            marker_start = line_start + marker.start(1)
            marker_end = line_start + marker.end(1)
            if match.start() == marker_start and match.end() <= marker_end:
                continue
        values.append(digits)
    return values


def _lexically_supported(claim: str, item) -> bool:
    """Every checkable number in a claim must occur as a whole value in a source.

    This includes single-digit measurements and percentages. Enumerated list
    markers are ignored so they don't become bogus factual claims.
    """
    source_numbers = set(_checkable_numbers(f"{item.title} {item.text}"))
    return all(
        digits in source_numbers
        for digits in _checkable_numbers(claim or "")
    )


def _numeric_claim_sentences(draft: DraftPost) -> list[str]:
    """Return sentences containing numeric assertions, including single-digit values.

    Splitting only at punctuation followed by whitespace avoids splitting decimal
    numbers such as 0.02. Leading enumerated-list markers are filtered separately.
    """
    parts = [draft.hook, draft.body]
    if draft.cta:
        parts.append(draft.cta)
    text = "\n".join(part for part in parts if part)
    candidates = re.split(r"(?<=[.!?…])\s+|\n+", text)
    out: list[str] = []
    seen: set[str] = set()
    for sentence in candidates:
        sentence = " ".join(sentence.split()).strip()
        if not sentence:
            continue
        checkable = _checkable_numbers(sentence)
        key = sentence.casefold()
        if checkable and key not in seen:
            seen.add(key)
            out.append(sentence)
    return out


def _numeric_audit_claims(draft: DraftPost, items: list[ItemScored]) -> list[Claim]:
    """Ground all numeric sentences against sources independently of the LLM."""
    if not items:
        return []
    audits: list[Claim] = []
    for sentence in _numeric_claim_sentences(draft):
        source = next((item for item in items if _lexically_supported(sentence, item)), None)
        audits.append(
            Claim(
                claim=sentence[:400],
                source_url=(source or items[0]).url,
                verified=source is not None,
                note="numeric_audit",
            )
        )
    return audits


_RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
_EN_MONTHS = {
    name: month
    for month, names in enumerate((
        ("jan", "january"), ("feb", "february"), ("mar", "march"),
        ("apr", "april"), ("may",), ("jun", "june"), ("jul", "july"),
        ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"),
        ("nov", "november"), ("dec", "december"),
    ), start=1)
    for name in names
}
_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(?P<year>(?:19|20)\d{2})[-/.](?P<month>\d{1,2})[-/.](?P<day>\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)(?P<day>\d{1,2})[./-](?P<month>\d{1,2})[./-](?P<year>(?:19|20)\d{2})(?!\d)"),
    re.compile(
        r"(?P<day>\d{1,2})\s+(?P<month>января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(?P<year>(?:19|20)\d{2})",
        re.I,
    ),
    re.compile(
        r"(?P<month>january|jan\.?|february|feb\.?|march|mar\.?|april|apr\.?|may|june|jun\.?|july|jul\.?|august|aug\.?|september|sept?\.?|october|oct\.?|november|nov\.?|december|dec\.?)[ ]+(?P<day>\d{1,2})(?:st|nd|rd|th)?[,]?[ ]+(?P<year>(?:19|20)\d{2})",
        re.I,
    ),
    re.compile(
        r"(?P<day>\d{1,2})[ ]+(?P<month>january|jan\.?|february|feb\.?|march|mar\.?|april|apr\.?|may|june|jun\.?|july|jul\.?|august|aug\.?|september|sept?\.?|october|oct\.?|november|nov\.?|december|dec\.?)[,]?[ ]+(?P<year>(?:19|20)\d{2})",
        re.I,
    ),
)


def _canonical_date(year: str, month: str | int, day: str) -> date | None:
    try:
        month_number = int(month) if str(month).isdigit() else (
            _RU_MONTHS.get(str(month).lower())
            or _EN_MONTHS.get(str(month).lower().rstrip("."))
        )
        if month_number is None:
            return None
        return date(int(year), month_number, int(day))
    except (TypeError, ValueError, OverflowError):
        return None


def _explicit_dates(text: str) -> set[date]:
    """Extract unambiguous full calendar dates and normalize formatting variants."""
    dates: set[date] = set()
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text or ""):
            groups = match.groupdict()
            parsed = _canonical_date(groups["year"], groups["month"], groups["day"])
            if parsed is not None:
                dates.add(parsed)
    return dates


def _date_claim_sentences(draft: DraftPost) -> list[str]:
    parts = [draft.hook, draft.body]
    if draft.cta:
        parts.append(draft.cta)
    text = "\n".join(part for part in parts if part)
    candidates = re.split(r"(?<=[.!?…])\s+|\n+", text)
    out: list[str] = []
    seen: set[str] = set()
    for sentence in candidates:
        sentence = " ".join(sentence.split()).strip()
        if not sentence or not _explicit_dates(sentence):
            continue
        key = sentence.casefold()
        if key not in seen:
            seen.add(key)
            out.append(sentence)
    return out


def _date_audit_claims(draft: DraftPost, items: list[ItemScored]) -> list[Claim]:
    """Require every explicit calendar date in a sentence to appear in one cited source."""
    if not items:
        return []
    source_dates = [
        (item, _explicit_dates(f"{item.title} {item.text}"))
        for item in items
    ]
    audits: list[Claim] = []
    for sentence in _date_claim_sentences(draft):
        dates = _explicit_dates(sentence)
        source = next(
            (item for item, dates_in_source in source_dates if dates.issubset(dates_in_source)),
            None,
        )
        audits.append(
            Claim(
                claim=sentence[:400],
                source_url=(source or items[0]).url,
                verified=source is not None,
                note="date_audit",
            )
        )
    return audits


def _fallback_claims(draft: DraftPost, items: list[ItemScored]) -> list[Claim]:
    """Deterministic claim set for offline mode and exhausted AI budget."""
    if not items:
        return []
    base = Claim(
        claim=draft.hook[:200],
        source_url=items[0].url,
        verified=_lexically_supported(draft.hook, items[0]),
    )
    return _dedupe_claims([
        base,
        *_numeric_audit_claims(draft, items),
        *_date_audit_claims(draft, items),
    ])


def _dedupe_claims(claims: list[Claim]) -> list[Claim]:
    """Deduplicate claims without letting a permissive duplicate mask a failure."""
    out: list[Claim] = []
    positions: dict[str, int] = {}
    for claim in claims:
        key = " ".join(claim.claim.casefold().split())
        if not key:
            continue
        if key not in positions:
            positions[key] = len(out)
            out.append(claim)
            continue
        index = positions[key]
        existing = out[index]
        if not claim.verified and existing.verified:
            out[index] = claim
        elif not existing.verified and claim.verified:
            # Numeric corroboration validates the figures, not the whole semantic
            # assertion. Preserve an existing unverified model claim.
            continue
        elif claim.note == "numeric_audit" and existing.note != "numeric_audit":
            out[index] = claim
    return out


def _channel_history_vectors(channel_id: str, limit: int = 60) -> list:
    """Recent draft/post vectors of one channel, for duplicate suppression."""
    from sqlalchemy import select

    from tgfarm.db.models import Post as PostRow
    from tgfarm.db.session import session_scope
    from tgfarm.db.vector import unpack

    with session_scope() as session:
        rows = session.execute(
            select(PostRow.embedding)
            .where(PostRow.channel_id == channel_id, PostRow.embedding.isnot(None))
            .order_by(PostRow.created_at.desc())
            .limit(limit)
        ).scalars().all()
    return [vec for vec in (unpack(blob) for blob in rows) if vec is not None]


# ------------------------------------------------------------------ persistence
def persist_draft(
    draft: DraftPost,
    plan: ChannelPlan,
    format_plan=None,
    video=None,
    *,
    plan_entry_id: int | None = None,
    plan_slot_at: datetime | None = None,
) -> str:
    """Write a draft as a `posts` row and return its public id.

    `plan_entry_id` and `plan_slot_at` are the editorial plan's booking for this
    topic, stored on the row it ordered: the id answers "which entry created
    this", the slot answers "when was it meant to go out". Both are `None` when no
    plan ordered the draft — the flag off, a manual run, a demo — and the row is
    then written exactly as it was before this pair existed. They are written once,
    at insert, because the question is about creation: a later snapshot that books
    the topic again does not move the link on a row it did not create.
    """
    public_id = uuid.uuid4().hex[:12]
    embedder = get_embedder()

    with session_scope() as session:
        post = PostRow(
            public_id=public_id,
            channel_id=plan.channel_id,
            cluster_public_id=plan.cluster_public_id,
            angle=plan.angle,
            post_format=format_plan.fmt if format_plan else "news",
            format_reason=(format_plan.reason if format_plan else None),
            hook=draft.hook,
            body=draft.body,
            cta=draft.cta,
            hashtags=list(draft.hashtags),
            sources=list(draft.sources),
            claims=[c.model_dump() for c in draft.claims],
            quality={
                "agent_team": {
                    "version": 1,
                    "roles": {
                        "topic_router": {
                            "status": "completed",
                            "angle": plan.angle,
                            "score": round(float(plan.score), 3),
                            "reason": str(plan.reason or "")[:240],
                        },
                        "format_planner": {
                            "status": "completed" if format_plan else "fallback",
                            "format": format_plan.fmt if format_plan else "news",
                            "reason": str(format_plan.reason if format_plan else "default format")[:240],
                        },
                        "editorial_strategist": dict(draft.editorial_strategy or {
                            "role": "editorial_strategist", "status": "skipped", "reason": "not_run"
                        }),
                        "writer": {
                            "status": "completed",
                            "provider": draft.generation_provider,
                        },
                        "fact_checker": {
                            "status": "completed",
                            "claim_count": len(draft.claims),
                            "verified_count": sum(1 for claim in draft.claims if claim.verified),
                        },
                    },
                },
            },
            language=draft.language or detect_lang(draft.text),
            generation_provider=draft.generation_provider,
            status="draft",
            embedding=pack(embedder.embed(draft.text)),
            hook_style=getattr(draft, "hook_style", None),
            variant=getattr(draft, "variant", "a"),
            char_len=draft.editorial_len,
            plan_entry_id=plan_entry_id,
            plan_slot_at=plan_slot_at,
        )
        session.add(post)

        if video is not None and getattr(video, "local_path", None):
            session.flush()
            session.add(
                MediaRow(
                    post_id=post.id,
                    kind="video",
                    path=video.local_path,
                    sha=None,
                    caption=(video.title or "")[:200] or None,
                    is_primary=True,
                )
            )

        channel = session.get(ChannelRow, plan.channel_id)
        if channel is not None:
            channel.niche = plan.persona.niche

    return public_id


# ------------------------------------------------- the plan's booking on the row
#: Open orders carried per call. One plan snapshot holds at most `MAX_ENTRIES`
#: entries, so this is the whole ledger, not a slice — read from the owner
#: instead of copied, because `MAX_OPEN_ORDERS`, `MAX_ENTRIES_PER_RUN` and this
#: bound are one fact about one snapshot and used to agree only by luck.
MAX_BOOKINGS_PER_RUN = MAX_ENTRIES

#: The named outcomes of one booking attempt. A booking that did not happen has
#: to say why, the same rule `plan_execution`'s reason codes and
#: `s7_publish.empty_gate_report` follow: a bare zero cannot tell a healthy
#: queue from a stall.
PLAN_BOOKING_BOOKED = "booked"
PLAN_BOOKING_DISABLED = "execution_disabled"
PLAN_BOOKING_NO_PLAN = "no_active_plan"
PLAN_BOOKING_NO_ENTRY = "no_schedule_entry"
PLAN_BOOKING_UNREADABLE_SLOT = "schedule_entry_slot_unreadable"
PLAN_BOOKING_NO_DRAFT = "no_unbooked_draft"
PLAN_BOOKING_CLAIM_LOST = "claim_lost"
PLAN_BOOKING_STATES = (
    PLAN_BOOKING_BOOKED,
    PLAN_BOOKING_DISABLED,
    PLAN_BOOKING_NO_PLAN,
    PLAN_BOOKING_NO_ENTRY,
    PLAN_BOOKING_UNREADABLE_SLOT,
    PLAN_BOOKING_NO_DRAFT,
    PLAN_BOOKING_CLAIM_LOST,
)

# ------------------------------------------------ the scheduler's pass over it
#: Job id, journal stage and rhythm of the third plan job, declared here rather
#: than in `plan_execution.py` because that module belongs to another package
#: this wave, while the work it would schedule is this one's: `book_plan_slots`
#: and its id live in the same file so a rename cannot leave the registry naming
#: a function that no longer exists. `pipeline._build_scheduler` reads all three,
#: exactly as it reads `plan_execution.PLAN_BUILD_JOB_ID` and friends.
#:
#: The interval matches the apply tick's because the two are one decision told in
#: two steps: the apply hand-over needs a row the scheduler will obey, and the
#: booking writes exactly that instant. A pass slower than the apply would let
#: an entry age into `slot_not_due` before anything reserved it; a faster one
#: would re-read a ledger the plan has not changed yet.
PLAN_BOOKING_JOB_ID = "editorial-plan-booking"
STAGE_BOOKING = "s4_plan_booking"
BOOKING_INTERVAL_MINUTES = 10


def _plan_moment(value: Any) -> datetime | None:
    """The plan's `slot_at` as an aware UTC instant, or None when it is not one.

    `editorial_plan._entry_dict` hands out ISO strings, so this is the reading
    the plan's own writer produced — no second calendar is introduced here.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    try:
        return ensure_utc(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def apply_plan_booking(cluster_public_id: str) -> dict[str, Any]:
    """Carry the ACTIVE plan's `schedule` booking onto the gated draft it ordered.

    Why this writer exists, against the measured base: `plan_execution.apply_plan`
    judges a `schedule` entry and hands the row over with `s7_publish.unpublish`,
    which stamps `scheduled_at = now` — the instant the plan reserved never
    reaches the row, so `schedule_ready_posts`, which since `0d8fd6f` obeys
    `posts.plan_slot_at`, is never given one to obey. And `persist_draft` can only
    book what the entry says at insert time, which for a `produce` entry is
    nothing: `editorial_plan` gives produce no slot at all. Measured at `d848eee`
    the only writer that ever put a booked instant on a draft was the panel's
    rewrite endpoint in `tgfarm/api.py` — HTTP plus a human — which is exactly the
    hole this closes.

    Narrow on purpose. It books *when*, never *what*: the format stays whatever
    the evidence-gated planner chose, because a plan entry is not a measurement
    and a topic with no measured posts may not have its shape rewritten after the
    fact (§6). It writes `plan_slot_at` only where that column is still NULL, so
    the "written once" contract the column's own comment states holds, and it
    leaves `plan_entry_id` alone — the produce entry stays the answer to "which
    entry ordered this post", while the entry that reserved the instant is
    auditable through the slot equality itself. The row is claimed with one
    conditional UPDATE, the same discipline `unpublish` uses, so a concurrent
    worker that already moved the post is reported and not overwritten.
    """
    from tgfarm.editorial_plan import get_active_plan

    decision: dict[str, Any] = {
        "cluster_public_id": cluster_public_id,
        "state": PLAN_BOOKING_BOOKED,
        "post_public_id": None,
        "plan_public_id": None,
        "action": None,
        "slot_at": None,
        "channel_id": None,
        "format": None,
        "reasons": [],
    }
    if not bool(getattr(get_settings(), "plan_execution_enabled", False)):
        # Off the executor's switch there is no consumer allowed to move rows,
        # and a booking nobody may honour is a claim, not a decision.
        decision["state"] = PLAN_BOOKING_DISABLED
        return decision

    plan = get_active_plan()
    if plan is None:
        decision["state"] = PLAN_BOOKING_NO_PLAN
        return decision
    decision["plan_public_id"] = plan.get("public_id")

    entry: dict[str, Any] | None = None
    for candidate in plan.get("entries") or []:
        if str(candidate.get("cluster_public_id") or "") != cluster_public_id:
            continue
        if decision["action"] is None:
            # The first entry for the topic names what the plan decided at all,
            # so a `produce`-only plan reports `no_schedule_entry` with the action
            # it really read instead of a bare "nothing found".
            decision["action"] = str(candidate.get("action") or "")
        if str(candidate.get("action") or "") == "schedule":
            entry = candidate
            decision["action"] = "schedule"
            break
    if entry is None:
        decision["state"] = PLAN_BOOKING_NO_ENTRY
        return decision

    decision["channel_id"] = entry.get("channel_id")
    decision["format"] = entry.get("format")
    decision["reasons"] = [str(part) for part in (entry.get("reasons") or [])]
    slot = _plan_moment(entry.get("slot_at"))
    if slot is None:
        decision["state"] = PLAN_BOOKING_UNREADABLE_SLOT
        return decision
    decision["slot_at"] = slot.isoformat()

    with session_scope() as session:
        target = session.execute(
            select(PostRow.public_id)
            .where(
                PostRow.cluster_public_id == cluster_public_id,
                PostRow.status == "gated",
                PostRow.scheduled_at.is_(None),
                PostRow.plan_slot_at.is_(None),
            )
            .order_by(PostRow.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if target is None:
            decision["state"] = PLAN_BOOKING_NO_DRAFT
            return decision
        decision["post_public_id"] = target
        claimed = session.execute(
            update(PostRow)
            .where(
                PostRow.public_id == target,
                PostRow.status == "gated",
                PostRow.scheduled_at.is_(None),
                PostRow.plan_slot_at.is_(None),
            )
            .values(plan_slot_at=slot)
        )
        if not claimed.rowcount:
            decision["state"] = PLAN_BOOKING_CLAIM_LOST
            return decision

    log.info(
        "s4: plan %s booked %s for %s at %s (%s)",
        decision["plan_public_id"],
        target,
        cluster_public_id,
        decision["slot_at"],
        "; ".join(str(part) for part in decision["reasons"])[:160],
    )
    return decision


def book_plan_slots(*, limit: int = MAX_BOOKINGS_PER_RUN) -> dict[str, Any]:
    """Walk every open editorial order and write its booked slot on the draft.

    This is the self-running offline entry the autonomy chain and `tgfarm demo`
    drive: no HTTP session, no panel click, no human, and nothing that can leave
    the process — it reads `editorial_plans` and writes one column of `posts`.
    Together with `editorial.open_order_cluster_ids`, which is what makes the
    plan see these topics at all, it turns the plan's `schedule` verdict into a
    `posts.plan_slot_at` the scheduler then obeys, which is the step that was
    missing between "the plan said when" and "the queue moved at that instant".
    """
    from tgfarm.editorial import open_order_cluster_ids

    with StageTimer(STAGE_BOOKING) as timer:
        cluster_ids = open_order_cluster_ids(limit)
        decisions = [apply_plan_booking(public_id) for public_id in cluster_ids]
        states: dict[str, int] = {}
        for decision in decisions:
            states[str(decision["state"])] = states.get(str(decision["state"]), 0) + 1
        booked = [decision for decision in decisions if decision["state"] == PLAN_BOOKING_BOOKED]
        timer.items_in = len(cluster_ids)
        timer.items_out = len(booked)
    result = {
        "considered": len(cluster_ids),
        "booked": len(booked),
        "states": states,
        "decisions": decisions,
    }
    log.info(
        "s4: plan bookings written for %d of %d open orders (%s)",
        len(booked),
        len(cluster_ids),
        states or "no open order to carry",
    )
    return result


def _journal_booking_tick(
    status: str,
    items_in: int | None,
    items_out: int | None,
    duration_ms: int,
    meta: dict[str, Any],
    error: str | None = None,
) -> None:
    """Journal one scheduled booking pass with the pipeline's own writer.

    Imported lazily and deliberately: `pipeline` registers this tick, so a
    module-level import would be a cycle, and `pipeline._record` owns the
    three-state counter rule (`None` is stored as 0 with the truth written
    beside it in `meta`), which is the thing P34 was filed for. Telemetry must
    never break a stage, and the writer already swallows its own failures.
    """
    from tgfarm.pipeline import _record as pipeline_record

    pipeline_record(
        f"s4-plan-{uuid.uuid4().hex[:8]}",
        STAGE_BOOKING,
        status,
        items_in=items_in,
        items_out=items_out,
        duration_ms=duration_ms,
        error=error,
        meta=meta,
    )


async def book_plan_tick(*, limit: int = MAX_BOOKINGS_PER_RUN) -> dict[str, Any]:
    """Give the booking pass the live caller it never had.

    Measured at the base of this package, `book_plan_slots` had exactly one
    caller in the repository — the stage the offline chain walks — so a
    self-running farm built the plan (`editorial-plan-build`), applied its
    verdicts (`editorial-plan-apply`), and never wrote `posts.plan_slot_at`.
    The plan's minute stayed in `editorial_plan_entries` while
    `schedule_ready_posts` re-derived a slot for the row from cadence, which is
    the gap between "the plan said when" and "the farm published when". This is
    the third job that closes it, and it is deliberately a wrapper:

    * it calls `book_plan_slots` and nothing else — no plan build, no hand-over,
      no publish, no status write; those are the two `plan_execution` ticks' and
      `s7_publish`'s own jobs, and a tick that could do them would be a second
      scheduler with a second set of bugs.
    * it journals what the pass returned under `STAGE_BOOKING`, the same stage
      name the cycle writes when it runs the identical work, so the panel can
      read one row per booking pass whoever triggered it.
    * a database failure is merged, not raised: APScheduler would log it and
      drop the job, and the farm would look healthy while the ledger stopped
      being answered. The two neighbouring ticks already answer the same way —
      `log.warning`, an `error` journal row with unmeasured counters, and a
      report that names the exception — so this tick says it in the same words.
    * off `PLAN_EXECUTION_ENABLED` it returns before opening a session, which is
      how the neighbours keep a flag-off pass byte-identical to base.
    """
    if not bool(getattr(get_settings(), "plan_execution_enabled", False)):
        return {"skipped": PLAN_BOOKING_DISABLED}
    started = time.monotonic()
    try:
        report = await to_thread(book_plan_slots, limit=limit)
    except Exception as exc:  # noqa: BLE001 - a booking failure must not kill the scheduler
        message = f"{type(exc).__name__}: {exc}"
        log.warning("s4: plan booking tick failed: %s", message[:200])
        _journal_booking_tick(
            "error",
            None,
            None,
            int((time.monotonic() - started) * 1000),
            {"job": PLAN_BOOKING_JOB_ID},
            error=message,
        )
        return {"error": message}
    _journal_booking_tick(
        "ok",
        int(report["considered"]),
        int(report["booked"]),
        int((time.monotonic() - started) * 1000),
        {
            "job": PLAN_BOOKING_JOB_ID,
            "states": dict(report["states"]),
        },
    )
    return report


DUP_THRESHOLD = DEFAULT_DUPLICATE_SIMILARITY


def _cosine(a, b) -> float:
    import numpy as np

    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b) / denom if denom else 0.0


def _too_similar_vec(
    vec, pool: list[dict], channel_id: str, threshold: float | None = None
) -> bool:
    """True when the draft embedding is a near-duplicate of the channel's recent output.

    `threshold=None` — the only way the pipeline calls this — reads
    `DUPLICATE_SIMILARITY` live on every draft, instead of the module constant
    this used to default to.
    """
    if threshold is None:
        threshold = duplicate_threshold()
    for other in pool:
        if other.get("channel_id") != channel_id:
            continue
        if _cosine(other["vec"], vec) >= threshold:
            return True
    return False


def plans_after_llm_gate(plans: list, *, requires_llm: bool, llm_ok: bool) -> list:
    """Drop Dzen channel plans while only the offline composer can answer.

    Dzen demotes deterministic, template-looking text, and its own length gate
    rejects the short offline drafts anyway (153 of 156 drafts in one 6 h window
    were thrown away). Drafting for it in that state burns source slots and hides
    the real outage behind a wall of "rejected" posts. With ``DZEN_REQUIRES_LLM``
    disabled the caller keeps every plan, so an offline-only setup still works.
    """
    if not requires_llm or llm_ok:
        return plans
    return [plan for plan in plans if getattr(plan, "platform", "") != "dzen"]


def _media_kinds(raw_items: list, video) -> set[str]:
    """Media the story can actually be told with, as the planner expects it.

    `video` is passed separately: footage licensed by S11 counts even when no
    feed entry carried one.
    """
    kinds = {str(getattr(item, "media_kind", "") or "").strip().lower() for item in raw_items}
    kinds.discard("")
    if video is not None:
        kinds.add("video")
    return kinds


#: Which evidence a format choice rests on. Recorded in the plan reason and in
#: the log so the operator can always tell *what* decided, not only *what* won.
PREFERENCE_DURABLE_LESSON = "durable_lesson"
PREFERENCE_LIVE_WINDOW = "live_window"
PREFERENCE_NO_DATA = "no_data"

_PREFERENCE_LABELS = {
    PREFERENCE_DURABLE_LESSON: "урок S12",
    PREFERENCE_LIVE_WINDOW: "окно S8",
    PREFERENCE_NO_DATA: "только правила",
}


def _no_lesson_preference(
    state: str, detail: str, *, platform: str = "", **extra
) -> dict[str, Any]:
    """A preference that names no format, so the previous strategy applies."""
    return {
        "state": state, "platform": platform, "format": "", "history": {},
        "sample_size": 0, "detail": detail, **extra,
    }


#: Lesson evidence states that name no live measurement — the same set S8
#: publishes as `EVIDENCE_UNTRUSTED`, spelled here to keep S4's imports lazy.
_UNTRUSTED_LESSON_EVIDENCE = frozenset({"stale", "never", "blocked"})

#: The two reasons a durable lesson can be refused, named apart because they ask
#: the operator to fix different things: stale snapshots need the collector, an
#: unevidenced publication needs the publisher to journal an identifier.
LESSON_REFUSAL_STALE = "stale_evidence"
LESSON_REFUSAL_UNEVIDENCED = "unevidenced"


def _int_or_none(value: Any) -> int | None:
    """An honest integer count from a lesson value; anything else says nothing."""
    if value is None or isinstance(value, bool):
        return None
    try:
        size = int(float(value))
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


def _float_or_none(value: Any) -> float | None:
    """A non-negative average from a lesson value, or `None` when it says nothing.

    `None` is the honest absence (`formats._rate` over an empty evidenced sample),
    and `0.0` is a measurement that earned nothing; collapsing the two is the
    reading this repo has already ruled out twice.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number < 0:
        return None
    return number


def _lesson_evidence_view(value: dict[str, Any], measured: int, engagement: float) -> dict[str, Any]:
    """How much of a durable lesson is live measurement the farm can *prove*.

    Two independent vetoes over the same stored row, both read from what S12 wrote
    and both spelled the way `s8_analytics.formats.planner_history` spells them for
    the live feed, so the durable lesson and the window answer «may this steer
    work» with one word rather than two:

    *freshness* — a lesson is meant to outlive the S8 window (that is the whole
    point of P32), but the snapshots behind it are not meant to outlive their own
    collector. S12 records how many measurements were inside
    ``FORMAT_EVIDENCE_STALE_AFTER_DAYS`` when the lesson was written and what they
    averaged, so the planner acts on those instead of on a blend:

    * nothing declared (a lesson written before this existed) — the lesson keeps
      its old numbers; refusing it on a marker it never got would silently change
      plans the operator did not ask to change, and the next learning cycle
      rewrites the row with the marker anyway;
    * `stale`/`never`/`blocked` — no live measurement at all, so no preference;
    * `mixed` — only the live count and the live average are handed on, so a dead
      snapshot can neither inflate the sample nor drag the engagement;
    * `ok` — live and total counts are the same numbers.

    *publication evidence* — of those live measurements, `evidenced_sample_size`
    are backed by proof the publication happened (a telegram message id, a
    journalled `posted` row). A snapshot of a send the platform's own rule
    disproves says nothing about a format, so it is taken out of the count *and*
    out of the average handed on, and what was taken out is named in
    `unevidenced_measured` instead of vanishing. With a row declaring nothing on
    this axis either, the lesson keeps the numbers it has always had.
    """
    status = str(value.get("evidence_status") or "").strip().lower()
    fresh = _int_or_none(value.get("fresh_measured"))
    stale = _int_or_none(value.get("stale_measured"))
    view: dict[str, Any] = {
        "status": status,
        "measured": measured,
        "engagement": engagement,
        "refuse": "",
        # Which refusal a non-empty `refuse` is: the caller names the state, and
        # «measured, but weeks ago» and «measured, but nothing was provably sent»
        # are two different facts for an operator reading the plan reason.
        "refusal": "",
        "extra": {},
    }
    if status:
        view["extra"] = {
            "evidence_status": status,
            "fresh_measured": fresh if fresh is not None else measured,
            "stale_measured": stale if stale is not None else 0,
        }
        if status in _UNTRUSTED_LESSON_EVIDENCE:
            view["measured"] = 0
            view["engagement"] = 0.0
            view["refuse"] = (
                f"живых измерений нет: все снимки этого урока старше порога "
                f"{_int_or_none(value.get('evidence_stale_after_days')) or 0} дн(а) — "
                "урок наблюдение, решает окно S8"
            )
            view["refusal"] = LESSON_REFUSAL_STALE
            return view
        if fresh is not None:
            view["measured"] = min(measured, fresh)
        if status == "mixed":
            try:
                fresh_average = float(value.get("fresh_avg_engagement"))
            except (TypeError, ValueError):
                fresh_average = float("nan")
            if fresh_average == fresh_average and fresh_average >= 0:
                view["engagement"] = fresh_average
            if view["measured"] < 1:
                view["refuse"] = (
                    f"из {measured} измерений живых {view['measured']}: мёртвые снимки "
                    "не могут разбавлять выборку — решает окно S8"
                )
                view["refusal"] = LESSON_REFUSAL_STALE
    # The publication veto, on top of whatever freshness left standing.
    provable = _int_or_none(value.get("evidenced_sample_size"))
    if provable is None:
        return view
    live = max(0, int(view["measured"]))
    gated = min(live, max(0, provable))
    unevidenced_live = _int_or_none(value.get("unevidenced_fresh")) or 0
    unrulled_published = _int_or_none(value.get("published_unrulled")) or 0
    view["extra"] = {
        **view["extra"],
        # The same names `planner_history` uses for the same facts, so a panel or a
        # reason string can read either feed without learning a second vocabulary.
        "publication_evidence": str(value.get("publication_evidence") or ""),
        "unevidenced_measured": unevidenced_live,
        "unevidenced_published": _int_or_none(value.get("published_unevidenced")) or 0,
        "unrulled_published": unrulled_published,
        "sample_size": gated,
    }
    average = _float_or_none(value.get("fresh_evidenced_avg_engagement"))
    if average is None:
        # No evidenced measurement: no rate. `0.0` here would be a verdict a
        # planner could rank, which is exactly what `formats._rate` refuses to write.
        average = float("nan")
    view["engagement"] = average
    view["measured"] = gated
    if gated < 1 and live >= 1 and not view["refuse"]:
        view["refuse"] = (
            f"из {live} живых измерений ни одного нельзя подтвердить доказательством "
            f"отправки ({unevidenced_live} без доказательства) — публикация, которую "
            "нечем показать, не исход, решает окно S8"
        )
        view["refusal"] = LESSON_REFUSAL_UNEVIDENCED
    return view


def durable_format_preference(
    channel_id: str,
    platform_id: str,
    *,
    has_video: bool = False,
) -> dict[str, Any]:
    """What the durable S12 lesson says about this destination's format.

    S12 keeps the measurement S8 took, so a lesson survives the window the live
    report reads and stops evaporating as soon as those posts age out. This
    answers with *either* a one-row history the planner may act on *or* nothing,
    never a blend of the two and never a lesson from another platform:

    * no channel, no platform or an unreadable S12 — no lesson is consulted at
      all, because an unknown destination is exactly where a Reddit number would
      otherwise leak into a Дзен plan;
    * only `below_sample`/`not_measured` lessons — `best_format_lesson` already
      refuses them via `reliable_only`, and an unmeasured format is a blind spot,
      not a verdict, so it cannot steer generation either;
    * a lesson whose own measurement is dead (every snapshot behind it older than
      ``FORMAT_EVIDENCE_STALE_AFTER_DAYS``) is answered `stale_evidence`: it was
      measured, just not while anyone was collecting, so it does not get to steer
      and the live window does instead;
    * a lesson whose live measurements are all publications nothing can point at
      (no telegram message id, no journalled `posted` row) is answered
      `unevidenced`, and a partly unevidenced one hands on only its provable count
      and provable average — the veto `planner_history` applies to the window,
      applied to the durable row too, because two standards for «may this steer
      work» is how a phantom publication ends up deciding a draft;
    * a trustworthy lesson naming a format this material cannot carry (outside the
      length budgets, `video` without licensed footage, or a sample under the
      planner's own threshold) is marked `unusable`;
    * otherwise `state="trusted"` with `history` = that one format's row, so the
      lesson — not an average of lesson and window — decides. For a mixed lesson
      (part of its snapshots live, part dead) that row carries the *live* count
      and the *live* average, declares both, and the dead half is named in the
      detail instead of being averaged into the number or the confidence.

    Every non-`trusted` answer leaves the live S8 window in charge, which is the
    strategy that ran before this existed.
    """
    channel = str(channel_id or "").strip()
    platform = str(platform_id or "").strip().lower()
    if not platform:
        return _no_lesson_preference(
            "no_platform", "площадка плана не названа — урок другой площадки не принимается"
        )
    if not channel:
        return _no_lesson_preference(
            "no_channel", "канал плана не назван — личного урока формата нет", platform=platform
        )

    try:
        from tgfarm.s10_format import LENGTH_BUDGET, MIN_HISTORY_SAMPLE
        from tgfarm.s12_learning import best_format_lesson, format_lessons

        lesson = best_format_lesson(channel, platform=platform)
    except Exception as exc:  # noqa: BLE001 - a missing lesson is not a broken cycle
        log.warning(
            "s4: durable format lesson unreadable for %s/%s (%s) — решает окно S8",
            channel, platform, exc,
        )
        return _no_lesson_preference(
            "unreadable", "урок S12 не читается", platform=platform
        )

    if lesson is None:
        observed: list[dict[str, Any]] = []
        try:
            observed = format_lessons(channel, platform=platform)
        except Exception as exc:  # noqa: BLE001 - the explanation may fail, the plan may not
            log.debug("s4: format lessons unreadable for %s/%s: %s", channel, platform, exc)
        if observed:
            best_measured = max(int(row.get("measured") or 0) for row in observed)
            threshold = max(int(row.get("min_sample") or 0) for row in observed)
            # `measured_evidenced` is the same sample S12 gated the verdict on, and
            # it defaults to `measured` for a row that predates it. A platform whose
            # lessons are all phantoms must not be reported as «measured, just too
            # few»: more of the same posts would not change anything there.
            best_evidenced = max(int(row.get("measured_evidenced") or 0) for row in observed)
            unevidenced_total = sum(int(row.get("measured_unevidenced") or 0) for row in observed)
            if best_evidenced <= 0 and unevidenced_total > 0:
                return _no_lesson_preference(
                    LESSON_REFUSAL_UNEVIDENCED,
                    f"уроки S12 по {platform}: {best_measured} измерений, из них "
                    f"{unevidenced_total} без доказательства отправки — доказанных нет, "
                    "наблюдение, не вывод",
                    platform=platform,
                    sample_size=best_measured,
                    min_sample=threshold,
                    lesson_count=len(observed),
                    unevidenced_measured=unevidenced_total,
                )
            return _no_lesson_preference(
                "below_sample",
                f"уроки S12 по {platform}: лучше всего измерено {best_measured} из порога "
                f"{threshold} — наблюдение, не вывод",
                platform=platform,
                sample_size=best_measured,
                min_sample=threshold,
                lesson_count=len(observed),
            )
        return _no_lesson_preference(
            "not_measured", f"уроков S12 по {platform} не записано", platform=platform
        )

    value = dict(lesson.get("value") or {})
    fmt = str(lesson.get("format") or value.get("format") or "").strip().lower()
    measured = int(lesson.get("measured") or lesson.get("sample_size") or 0)
    published = int(lesson.get("published") or value.get("published") or 0)
    try:
        engagement = float(value.get("avg_engagement") or 0.0)
    except (TypeError, ValueError):
        engagement = float("nan")
    evidence = _lesson_evidence_view(value, measured, engagement)
    live_measured = int(evidence["measured"])
    live_engagement = float(evidence["engagement"])

    if evidence["refuse"]:
        # Measured, but not while anyone was collecting, or not with a publication
        # anyone can point at: the lesson stays an observation and the live window
        # (or the rules) decides, exactly as it did before this lesson was trusted.
        return _no_lesson_preference(
            str(evidence["refusal"] or LESSON_REFUSAL_STALE),
            f"урок S12 «{fmt}»: {evidence['refuse']}",
            platform=platform,
            sample_size=measured,
            format=fmt,
            live_sample_size=live_measured,
            evidence_status=str(evidence["status"]),
            publication_evidence=str(evidence["extra"].get("publication_evidence") or ""),
            unevidenced_measured=int(evidence["extra"].get("unevidenced_measured") or 0),
        )
    if fmt not in LENGTH_BUDGET:
        return _no_lesson_preference(
            "unusable",
            f"урок S12 называет формат «{fmt}», для которого нет бюджета длины",
            platform=platform, sample_size=measured,
        )
    if fmt == "video" and not has_video:
        return _no_lesson_preference(
            "unusable",
            "урок S12 просит видео, а лицензированного ролика нет",
            platform=platform, sample_size=measured, format=fmt,
        )
    if live_measured < MIN_HISTORY_SAMPLE:
        return _no_lesson_preference(
            "unusable",
            f"урок S12 «{fmt}» опирается на {live_measured} измерений — план требует "
            f"{MIN_HISTORY_SAMPLE}, решает окно S8",
            platform=platform, sample_size=measured, format=fmt,
        )
    if live_engagement != live_engagement or live_engagement < 0:
        return _no_lesson_preference(
            "unusable",
            f"урок S12 «{fmt}» не несёт проверяемой вовлечённости — решает окно S8",
            platform=platform, sample_size=measured, format=fmt,
        )

    live_tail = (
        f", живых {live_measured} из {measured}"
        if evidence["status"] and live_measured != measured
        else ""
    )
    return {
        "state": "trusted",
        "platform": platform,
        "format": fmt,
        "sample_size": live_measured,
        "detail": (
            f"«{fmt}» по {platform}: измерено {live_measured} из {published}, "
            f"avg {live_engagement:.2f}{live_tail}"
        ),
        "history": {
            fmt: {
                "measured": live_measured,
                "published": published,
                "avg_engagement": live_engagement,
                "reliable": True,
                **evidence["extra"],
            }
        },
    }


def format_plan_for(
    plan,
    *,
    cluster_view,
    topic: str,
    media_kinds: set[str],
    has_video: bool,
    history: dict[str, dict[str, Any]] | None,
    use_performance: bool,
    preference: dict[str, Any] | None = None,
) -> Any:
    """One plan's format choice: rules only, or rules plus measured performance.

    With ``use_performance`` off the planner is never consulted, no lesson is
    read and the result is byte-identical to `plan_for_cluster`, so switching the
    flag off restores the previous generation strategy exactly.

    With it on the preference order is: the trustworthy durable S12 lesson for
    *this* platform, else the live S8 window, else the rules. `preference` may be
    passed in already resolved (the cycle resolves and caches one per channel and
    platform); `None` means look it up here. The two bodies of evidence are never
    merged: whichever one speaks alone sets the format, and the reason says which
    one did (`урок S12` / `окно S8` / `только правила`) and why the other stayed
    silent.
    """
    from tgfarm.s10_format import as_format_plan, plan_for_cluster, plan_with_history

    if not use_performance:
        return plan_for_cluster(cluster_view, plan.persona, has_video=has_video)

    platform_id = str(getattr(plan, "platform", "") or "").strip().lower()
    if preference is None:
        preference = durable_format_preference(
            str(getattr(plan, "channel_id", "") or ""),
            platform_id,
            has_video=bool(has_video) or "video" in (media_kinds or set()),
        )
    lesson_history = (
        dict(preference.get("history") or {})
        if preference.get("state") == "trusted"
        else {}
    )
    # One source of evidence only: the lesson replaces the window for this plan,
    # it does not join it.
    chosen_history = lesson_history or (history or {})

    chosen = plan_with_history(
        plan.persona,
        platform_id=platform_id,
        media_kinds=media_kinds,
        topic=topic,
        history=chosen_history,
        cluster=cluster_view,
    )
    if chosen["source"] == "history":
        preference_key = (
            PREFERENCE_DURABLE_LESSON if lesson_history else PREFERENCE_LIVE_WINDOW
        )
    else:
        preference_key = PREFERENCE_NO_DATA
    label = _PREFERENCE_LABELS[preference_key]
    detail = str(preference.get("detail") or "")
    # The reason names the evidence that decided and, when it was not the lesson,
    # why the lesson stayed silent.
    tail = f"довод: {label}"
    if detail:
        tail = f"{tail} — {detail}"

    measured = sum(int(row.get("measured") or 0) for row in chosen["history"].values())
    # How the evidence the planner was handed declared itself: a row of live
    # measurements and a row of dead ones both arrive with a number, and the
    # operator deciding whether to trust the flag needs to see which it was.
    live = sum(1 for row in chosen["history"].values() if row.get("evidence_status") == "ok")
    dead = sum(
        1 for row in chosen["history"].values()
        if str(row.get("evidence_status") or "") in _UNTRUSTED_LESSON_EVIDENCE
    )
    log.info(
        "s4 format plan %s/%s: %s source=%s preference=%s lesson_state=%s "
        "confidence=%.2f measured=%d live_rows=%d dead_rows=%d platform=%s",
        plan.channel_id,
        getattr(plan, "cluster_public_id", ""),
        chosen["format"],
        chosen["source"],
        preference_key,
        str(preference.get("state") or ""),
        float(chosen["confidence"]),
        measured,
        live,
        dead,
        platform_id,
    )
    format_plan = as_format_plan(chosen)
    format_plan.reason = f"{format_plan.reason}; {tail}".strip("; ")
    return format_plan


#: Names the editorial plan as the owner of a format choice, in `format_reason`,
#: so an operator reading a post can tell a booked shape from a measured one.
BOOKED_FORMAT_MARK = "формат из редакторского плана"


def apply_booked_format(format_plan: Any, booked: str | None) -> Any:
    """Obey the format the editorial plan booked, when it booked one.

    The plan's `format` column is free text, so a booking S10 has no length budget
    for is refused rather than guessed at: the planner's own choice stands, the
    run continues, and the refusal is logged. A booking that matches what the
    planner already chose keeps the planner's reason, because a reason claiming an
    override that changed nothing would misreport who decided.

    Only the shape is taken from the plan — format and its length budget. The
    planner's media flags and outline stay: the plan says *what shape*, not *what
    evidence* the post ships with.
    """
    if not booked:
        return format_plan
    from tgfarm.s10_format import LENGTH_BUDGET, FormatPlan

    budget = LENGTH_BUDGET.get(booked)
    if budget is None:
        log.warning(
            "s4: editorial plan booked format %r, which S10 has no budget for — %s stands",
            booked,
            getattr(format_plan, "fmt", "news"),
        )
        return format_plan
    if getattr(format_plan, "fmt", None) == booked:
        return format_plan
    low, high = budget
    decided_by = (
        f"{BOOKED_FORMAT_MARK}: {booked} (S10 выбрала {format_plan.fmt})"
        if format_plan is not None
        else f"{BOOKED_FORMAT_MARK}: {booked} (S10 не ответила)"
    )
    base = format_plan if format_plan is not None else FormatPlan()
    return replace(base, fmt=booked, min_chars=low, max_chars=high, reason=decided_by)


def plans_for_booking(plans: list, booked_channel_id: str) -> list:
    """Keep only the destination the editorial plan booked for this topic.

    An empty `booked_channel_id` means there was no booking to obey — no plan, the
    flag off, or an entry that named no channel — and every routed plan stays,
    which is the base path unchanged. When a destination *is* named, only the plans
    that reach it are written; an empty result is a real answer and not a fallback,
    because quietly publishing a planned topic on some *other* channel is exactly
    the failure this filter exists to refuse.
    """
    if not booked_channel_id:
        return plans
    return [plan for plan in plans if str(getattr(plan, "channel_id", "")) == booked_channel_id]


async def run(cluster_public_id: str) -> list[str]:
    """Route a cluster, choose a format per channel, and generate drafts.

    The editorial plan is asked first, and only for this topic: when a `produce`
    entry booked a destination, that is the channel written for; when it booked a
    format S10 knows, that is the shape generated. A booked destination routing
    cannot reach stops the run rather than moving it elsewhere, and the topic stays
    unconsumed so a rebuilt plan can still serve it. With no booking — the flag
    off, no plan, no entry for this cluster — every step below is the base path.

    A topic the plan already ordered and whose draft still waits for a booked
    slot is refused before anything is read: the editorial queue shows such an
    open order again (that is what lets the plan schedule it), and drafting it a
    second time would spend generation budget twice on one story and give the
    topic two rows competing for one slot.
    """
    from tgfarm.editorial import awaiting_booking
    from tgfarm.plan_execution import generation_brief
    from tgfarm.s2_trends import mark_consumed
    from tgfarm.s3_router import plan_cluster
    from tgfarm.s8_analytics.formats import planner_history
    from tgfarm.s11_video import fetch_video_for

    if awaiting_booking(cluster_public_id):
        log.info("s4: %s is an open plan order — the slot is what it waits for", cluster_public_id)
        return []

    with session_scope() as session:
        cluster = session.execute(
            select(ClusterRow).where(ClusterRow.public_id == cluster_public_id)
        ).scalar_one_or_none()
        if cluster is None:
            return []
        raw_items = list(
            session.execute(
                select(RawItemRow).where(RawItemRow.cluster_id == cluster.id)
            ).scalars().all()
        )

    plans = await plan_cluster(cluster_public_id)
    if not plans:
        return []

    brief = await to_thread(generation_brief, cluster_public_id)
    plans = plans_for_booking(plans, str(getattr(brief, "channel_id", "") or ""))
    if brief is not None and not plans:
        log.warning(
            "s4: editorial plan %s booked %s for %s and routing refused it — nothing generated",
            brief.plan_public_id,
            brief.channel_id,
            cluster_public_id,
        )
        return []

    # One video lookup for the whole cluster — S11 does the licensing gate.
    # Items whose feed entry already carried video go first; probing an article
    # page costs a request, so the fallback is capped.
    video = None
    try:
        ordered = sorted(raw_items, key=lambda r: r.media_kind != "video")
        for item in ordered[:MAX_VIDEO_PROBES]:
            candidate = await fetch_video_for(item)
            if candidate is not None:
                video = candidate
                break
    except Exception as exc:  # noqa: BLE001 - video is additive
        log.warning("video discovery failed: %s", exc)

    scored_items = _to_scored(raw_items)
    created: list[str] = []
    # Load per-channel history before checking the first generated draft. The
    # separate archive lookup below covers the entire published history, not just
    # this short vector window.
    history: list = []
    seen_batch: list = []
    embedder = get_embedder()
    for channel_id in sorted({plan.channel_id for plan in plans}):
        history.extend(
            {"channel_id": channel_id, "vec": vec, "label": ""}
            for vec in await to_thread(_channel_history_vectors, channel_id)
        )

    def _too_similar(draft_text: str, channel_id: str):
        vec = embedder.embed(draft_text)
        if _too_similar_vec(vec, history + seen_batch, channel_id):
            return True, vec
        seen_batch.append(
            {"channel_id": channel_id, "vec": vec, "label": draft_text[:60]}
        )
        return False, vec

    settings = get_settings()
    if plans and getattr(settings, "dzen_requires_llm", False) and any(
        getattr(plan, "platform", "") == "dzen" for plan in plans
    ):
        from tgfarm.core.llm_health import llm_recently_working

        llm_ok = await to_thread(llm_recently_working)
        gated = plans_after_llm_gate(
            plans, requires_llm=True, llm_ok=llm_ok
        )
        if len(gated) != len(plans):
            log.warning(
                "s4: dzen drafts paused — no online model produced a draft recently"
            )
            plans = gated

    with StageTimer("s4_generate") as timer:
        kinds = _media_kinds(raw_items, video)
        history_cache: dict[str, dict[str, Any]] = {}
        preference_cache: dict[tuple[str, str], dict[str, Any]] = {}
        footage_ok = video is not None or "video" in kinds

        async def _performance_history(platform_id: str) -> dict[str, dict[str, Any]]:
            """One platform-scoped read per format, reused across this cluster's plans."""
            if platform_id not in history_cache:
                try:
                    history_cache[platform_id] = await to_thread(
                        planner_history,
                        platform_id,
                        days=settings.format_performance_window_days,
                    )
                except Exception as exc:  # noqa: BLE001 - no evidence beats a broken read
                    history_cache[platform_id] = {}
                    log.warning(
                        "s4: format performance unreadable for %s (%s) — rules only",
                        platform_id,
                        exc,
                    )
            return history_cache[platform_id]

        async def _format_preference(channel_id: str, platform_id: str) -> dict[str, Any]:
            """One durable-lesson read per destination, reused across this cluster."""
            key = (channel_id, platform_id)
            if key not in preference_cache:
                try:
                    preference_cache[key] = await to_thread(
                        durable_format_preference,
                        channel_id,
                        platform_id,
                        has_video=footage_ok,
                    )
                except Exception as exc:  # noqa: BLE001 - a lesson nobody reads is not a verdict
                    preference_cache[key] = {
                        "state": "unreadable", "platform": platform_id, "format": "",
                        "history": {}, "sample_size": 0, "detail": "урок S12 не читается",
                    }
                    log.warning(
                        "s4: durable format lesson failed for %s/%s (%s) — окно S8 решает",
                        channel_id,
                        platform_id,
                        exc,
                    )
            return preference_cache[key]

        for plan in plans:
            try:
                cluster_view = ClusterSchema(
                    id=cluster_public_id,
                    label=cluster.label or "",
                    items=scored_items,
                    niche=cluster.niche,
                    heat=cluster.heat,
                )
                platform_id = str(getattr(plan, "platform", "") or "").strip().lower()
                # The durable lesson is asked first: when it can be trusted it
                # replaces the live window, so the two are never averaged and the
                # heavier S8 read is skipped. Named apart from the similarity
                # `history` above — rebinding that name here would turn the dedupe
                # lookup into `dict + list`.
                preference = (
                    await _format_preference(str(getattr(plan, "channel_id", "") or ""), platform_id)
                    if settings.format_performance_planner
                    else None
                )
                lesson_decides = bool(preference and preference.get("state") == "trusted")
                format_history = (
                    {}
                    if not settings.format_performance_planner or lesson_decides
                    else await _performance_history(platform_id)
                )
                format_plan = format_plan_for(
                    plan,
                    cluster_view=cluster_view,
                    topic=cluster.label or "",
                    media_kinds=kinds,
                    has_video=video is not None,
                    history=format_history,
                    use_performance=settings.format_performance_planner,
                    preference=preference,
                )
                # The plan's booking outranks the measured choice when it named a
                # format at all; the row keeps both voices in its reason.
                format_plan = apply_booked_format(format_plan, getattr(brief, "fmt", None))
                draft = await generate(plan, raw_items, format_plan, video=video)
            except Exception as exc:  # noqa: BLE001 - one bad plan must not kill the cluster
                log.warning("generation failed for %s: %s", plan.channel_id, exc)
                continue
            batch_duplicate, draft_vector = _too_similar(draft.text, plan.channel_id)
            if batch_duplicate:
                log.info("s4 duplicate suppressed against in-cycle history: %s/%s", plan.channel_id, cluster_public_id)
                continue
            try:
                from tgfarm.s4_generation.memory import find_near_duplicate

                prior = await to_thread(
                    find_near_duplicate, draft.text, plan.channel_id, draft_vector
                )
            except Exception as exc:  # noqa: BLE001 - archive search is fail-open
                prior = None
                log.warning("archive duplicate check failed: %s", exc)
            if prior:
                log.info(
                    "s4 duplicate suppressed against full archive: %s/%s prior=%s similarity=%.3f",
                    plan.channel_id, cluster_public_id, prior["post_public_id"], prior["similarity"],
                )
                continue
            public_id = await to_thread(
                persist_draft,
                draft,
                plan,
                format_plan,
                video,
                plan_entry_id=getattr(brief, "entry_id", None),
                plan_slot_at=getattr(brief, "slot_at", None),
            )
            created.append(public_id)

        timer.items_in = len(plans)
        timer.items_out = len(created)

    if created:
        await to_thread(mark_consumed, cluster_public_id)
    return created


__all__ = [
    "BOOKING_INTERVAL_MINUTES",
    "PLAN_BOOKING_JOB_ID",
    "STAGE_BOOKING",
    "apply_plan_booking",
    "book_plan_slots",
    "book_plan_tick",
    "generate",
    "normalise",
    "persist_draft",
    "run",
    "verify",
]