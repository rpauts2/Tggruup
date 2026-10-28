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
import uuid

from sqlalchemy import select

from tgfarm.config import Persona
from tgfarm.core.errors import ProviderUnavailable
from tgfarm.core.logging import get_logger
from tgfarm.core.run import StageTimer, to_thread
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
from tgfarm.llm import policy
from tgfarm.llm.client import LLMResponse, compose_offline_post, get_llm
from tgfarm.llm.embedding import get_embedder
from tgfarm.s3_router import ChannelPlan

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
        mistakes_list = _lessons(persona.id, kind="mistake")
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
    holdout = random.random() < max(0.0, min(1.0, get_settings().holdout_share))
    examples = [] if holdout else await to_thread(_best_examples, plan.channel_id)
    hook_style = _pick_hook_style(plan.channel_id)
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
            cluster_public_id=plan.cluster_public_id,
        )
    else:
        decision = None
        await to_thread(
            policy.record_skip,
            "s4_draft",
            purpose=fmt,
            reason=reason,
            cluster_public_id=plan.cluster_public_id,
        )

    if decision is not None and decision.allowed:
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
        payload, response = offline, LLMResponse("", "offline", "none", 0)

    if response.provider == "offline" and not payload.get("hook"):
        raise ProviderUnavailable(
            f"offline composer has no {persona.language} source for "
            f"{plan.cluster_public_id}"
        )
    draft = normalise(payload, persona, plan.angle, items)
    draft.generation_provider = (
        response.provider if response.provider != "offline" else "offline"
    )
    draft.hook_style = classify_hook_style(draft.hook)
    draft.variant = "holdout" if holdout else "a"
    draft.post_format = fmt

    verified = await verify(draft, items, generation_provider=draft.generation_provider)
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
            cluster_public_id="",
        )
        return _fallback_claims(draft, items)

    decision = await to_thread(
        policy.reserve, "s4_verify", purpose="verify"
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
    return _dedupe_claims(claims)


# A number followed by an optional unit. Deliberately strict about the decimal
# point (`\d+(?:[.,]\d+)*`) so a sentence-ending period is never swallowed into
# the token — "GPT-5.5." must normalise to "5.5", not "5.5.".
_NUMBER_RE = re.compile(
    r"\d+(?:[.,]\d+)*\s*(?:%|млрд|млн|тыс|млрд\.|руб(?:\.|лей)?|RUB|USD|EUR|\$)?",
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
    cleaned = re.sub(r"[^\d.,]", "", cleaned)
    cleaned = cleaned.rstrip(".,")
    return cleaned.replace(",", ".")


def _normalise_haystack(text: str) -> str:
    collapsed = re.sub(r"\s+", "", text or "").lower()
    return re.sub(r"(?<=\d),(?=\d)", ".", collapsed)


def _lexically_supported(claim: str, item) -> bool:
    """Cheap corroboration: every number in the claim must appear in the source.

    Catches the dominant hallucination class (invented figures) without any API
    call. A claim with no checkable numbers passes; qualitative claims still need
    the LLM fact-checker and editorial gates.
    """
    haystack = _normalise_haystack(f"{item.title} {item.text}")
    for token in _NUMBER_RE.findall(claim or ""):
        digits = _normalise_number(token)
        if len(digits) >= 2 and digits not in haystack:
            return False
    return True


def _numeric_claim_sentences(draft: DraftPost) -> list[str]:
    """Return sentences containing meaningful numeric assertions.

    Splitting only at punctuation followed by whitespace avoids splitting decimal
    numbers such as 0.02. Very short numbers are ignored by the same grounding
    rule used elsewhere, so list labels like "1." do not become false alarms.
    """
    text = f"{draft.hook}\n{draft.body}"
    candidates = re.split(r"(?<=[.!?…])\s+|\n+", text)
    out: list[str] = []
    seen: set[str] = set()
    for sentence in candidates:
        sentence = " ".join(sentence.split()).strip()
        if not sentence:
            continue
        checkable = [
            _normalise_number(token)
            for token in _NUMBER_RE.findall(sentence)
            if len(_normalise_number(token)) >= 2
        ]
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


def _fallback_claims(draft: DraftPost, items: list[ItemScored]) -> list[Claim]:
    """Deterministic claim set for offline mode and exhausted AI budget."""
    if not items:
        return []
    base = Claim(
        claim=draft.hook[:200],
        source_url=items[0].url,
        verified=_lexically_supported(draft.hook, items[0]),
    )
    return _dedupe_claims([base, *_numeric_audit_claims(draft, items)])


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
) -> str:
    """Write a draft as a `posts` row and return its public id."""
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
            quality={},
            language=draft.language or detect_lang(draft.text),
            generation_provider=draft.generation_provider,
            status="draft",
            embedding=pack(embedder.embed(draft.text)),
            hook_style=getattr(draft, "hook_style", None),
            variant=getattr(draft, "variant", "a"),
            char_len=draft.editorial_len,
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


DUP_THRESHOLD = 0.88


def _cosine(a, b) -> float:
    import numpy as np

    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b) / denom if denom else 0.0


def _too_similar_vec(vec, pool: list[dict], channel_id: str, threshold: float = DUP_THRESHOLD) -> bool:
    """True when the draft embedding is a near-duplicate of the channel's recent output."""
    for other in pool:
        if other.get("channel_id") != channel_id:
            continue
        if _cosine(other["vec"], vec) >= threshold:
            return True
    return False


async def run(cluster_public_id: str) -> list[str]:
    """Route a cluster, choose a format per channel, and generate drafts."""
    from tgfarm.s2_trends import mark_consumed
    from tgfarm.s3_router import plan_cluster
    from tgfarm.s10_format import plan_for_cluster
    from tgfarm.s11_video import fetch_video_for

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
    # Two clusters can carry the same story under different labels; routing
    # compares labels and lets both through, and the wall ends up with the
    # same post twice in a row. The draft embedding is the honest check.
    history: list = []
    seen_batch: list = []
    embedder = get_embedder()

    def _too_similar(draft_text: str, channel_id: str) -> bool:
        vec = embedder.embed(draft_text)
        if _too_similar_vec(vec, history + seen_batch, channel_id):
            return True
        seen_batch.append(
            {"channel_id": channel_id, "vec": vec, "label": draft_text[:60]}
        )
        return False

    with StageTimer("s4_generate") as timer:
        for plan in plans:
            try:
                cluster_view = ClusterSchema(
                    id=cluster_public_id,
                    label=cluster.label or "",
                    items=scored_items,
                    niche=cluster.niche,
                    heat=cluster.heat,
                )
                format_plan = plan_for_cluster(
                    cluster_view, plan.persona, has_video=video is not None
                )
                draft = await generate(plan, raw_items, format_plan, video=video)
            except Exception as exc:  # noqa: BLE001 - one bad plan must not kill the cluster
                log.warning("generation failed for %s: %s", plan.channel_id, exc)
                continue
            if _too_similar(draft.text, plan.channel_id):
                continue
            if not history:
                history = [
                    {"channel_id": plan.channel_id, "vec": vec, "label": ""}
                    for vec in await to_thread(
                        _channel_history_vectors, plan.channel_id
                    )
                ]
            public_id = await to_thread(
                persist_draft, draft, plan, format_plan, video
            )
            created.append(public_id)

        timer.items_in = len(plans)
        timer.items_out = len(created)

    if created:
        await to_thread(mark_consumed, cluster_public_id)
    return created


__all__ = ["generate", "normalise", "persist_draft", "run", "verify"]