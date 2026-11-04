"""S3 — routing: decide which channel gets which story, at which angle.

A single hot story can legitimately feed several channels, but only if each
takes a *different* angle — otherwise the fleet looks like one voice and readers
see the same post five times. Anti-sameness is enforced here, at routing time,
because it is much cheaper than generating two identical posts and throwing one
away in S5.
"""

from __future__ import annotations

import random
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import select

from tgfarm.config import Persona, get_config
from tgfarm.core.logging import get_logger
from tgfarm.core.run import StageTimer
from tgfarm.core.settings import get_settings
from tgfarm.core.text import content_tokens, jaccard
from tgfarm.db.models import Channel as ChannelRow
from tgfarm.db.models import Cluster as ClusterRow
from tgfarm.db.models import Post as PostRow
from tgfarm.db.models import RawItem as RawItemRow
from tgfarm.db.session import session_scope
from tgfarm.db.vector import pack, unpack
from tgfarm.llm.client import get_llm
from tgfarm.llm.embedding import get_embedder
from tgfarm.platform_capability import CAPABILITY_LABELS, CAPABILITY_NOTES, capability_of

log = get_logger(__name__)

TOPIC_COOLDOWN_HOURS = 72
RECENT_HISTORY_LIMIT = 200

#: Levels that cannot receive material at all. Planning such a destination would
#: promise a delivery S7 cannot perform, so it is dropped with a recorded reason.
UNROUTABLE_CAPABILITIES: frozenset[str] = frozenset({"unsupported", "read_only"})

#: Levels where the farm prepares the material but a human finishes delivery.
MANUAL_CAPABILITIES: frozenset[str] = frozenset({"draft_only", "manual_upload"})

#: How many clusters keep their skip report in memory (the panel reads it back).
SKIP_REGISTRY_LIMIT = 128


@dataclass
class ChannelPlan:
    cluster_public_id: str
    channel_id: str
    persona: Persona
    angle: str
    score: float
    reason: str = ""
    # Capability contract. Every field is defaulted, so existing construction
    # sites and consumers keep working: a plan for a `publish` destination looks
    # exactly as before once the defaults are applied.
    capability: str = "publish"
    capability_label: str = CAPABILITY_LABELS["publish"]
    requires_manual_action: bool = False
    manual_step: str = ""
    notes: str = ""
    # The channel's destination family. Consumers gate on it (Дзен must not get
    # an offline draft), so the router fills it instead of leaving readers to
    # guess it from `channel_id`.
    platform: str = ""

    def key(self) -> str:
        return f"{self.channel_id}:{self.cluster_public_id}"


@dataclass(frozen=True)
class SkippedDestination:
    """A destination the router refused, with the reason kept next to the plan."""

    cluster_public_id: str
    channel_id: str
    platform: str
    capability: str
    capability_label: str
    reason: str

    def note(self) -> str:
        return f"{self.channel_id} ({self.platform}): {self.reason}"


_SKIP_REGISTRY: OrderedDict[str, tuple[SkippedDestination, ...]] = OrderedDict()


def _remember_skips(cluster_public_id: str, skipped: list[SkippedDestination]) -> None:
    key = str(cluster_public_id)
    _SKIP_REGISTRY[key] = tuple(skipped)
    _SKIP_REGISTRY.move_to_end(key)
    while len(_SKIP_REGISTRY) > SKIP_REGISTRY_LIMIT:
        _SKIP_REGISTRY.popitem(last=False)


def route_skips(cluster_public_id: str) -> list[SkippedDestination]:
    """Destinations dropped from a cluster's plan and why (for API/panel)."""
    return list(_SKIP_REGISTRY.get(str(cluster_public_id), ()))


def _capability_level(platform_id: str) -> str:
    try:
        return capability_of(platform_id)
    except Exception as exc:  # noqa: BLE001 - routing must survive a broken catalogue
        log.debug("capability lookup failed for %s: %s", platform_id, exc)
        return "publish"


def _capability_gate(
    personas: list[Persona], cluster_public_id: str
) -> tuple[list[Persona], list[SkippedDestination]]:
    """Split candidates into plannable destinations and unreachable ones.

    `unsupported` and `read_only` cannot accept material, so they never enter the
    plan; the reason is kept in the skip report and in `ChannelPlan.notes`.
    """
    allowed: list[Persona] = []
    skipped: list[SkippedDestination] = []
    for persona in personas:
        level = _capability_level(persona.platform)
        if level in UNROUTABLE_CAPABILITIES:
            label = CAPABILITY_LABELS.get(level, level)
            skipped.append(
                SkippedDestination(
                    cluster_public_id=cluster_public_id,
                    channel_id=persona.id,
                    platform=persona.platform,
                    capability=level,
                    capability_label=label,
                    reason=f"{label}: {CAPABILITY_NOTES.get(level, '')}".strip(),
                )
            )
            log.debug("skip %s: platform %s is %s", persona.id, persona.platform, level)
            continue
        allowed.append(persona)
    return allowed, skipped


def _capability_fields(platform_id: str) -> dict:
    """Plan fields describing how this destination is actually delivered."""
    level = _capability_level(platform_id)
    manual = level in MANUAL_CAPABILITIES
    return {
        "capability": level,
        "capability_label": CAPABILITY_LABELS.get(level, level),
        "requires_manual_action": manual,
        "manual_step": CAPABILITY_NOTES.get(level, "") if manual else "",
    }


def _skips_note(skipped: list[SkippedDestination]) -> str:
    if not skipped:
        return ""
    return "Пропущены площадки: " + "; ".join(item.note() for item in skipped)


# ------------------------------------------------------------- classification
ANGLES_HINT = "news|analysis|why_it_matters|checklist|comparison|history|listicle"

_CLASSIFY_SYSTEM = f"""Ты — редактор-распределитель фермы каналов.
Тебе дают заголовок, выдержку из статьи и список каналов (id + ниша + аудитория).
Верни ТОЛЬКО JSON:
{{"routes": [{{"channel_id": "...", "score": 0.0-1.0, "angle": "{ANGLES_HINT}"}}]}}
Правила: score — насколько история подходит этому каналу. Не выдумывай channel_id.
Если история не подходит ни одному каналу — верни пустой массив routes."""

_CLASSIFY_OFFLINE = {"routes": []}


async def classify_niche(label: str, members: list, available: list[Persona]) -> list[dict]:
    """LLM route scoring with a deterministic keyword fallback."""
    if not available:
        return []

    llm = get_llm()
    channels_block = "\n".join(
        f"- {p.id} | ниша={p.niche} | аудитория={p.audience} | заголовок={p.title}"
        for p in available
    )
    sources = "\n".join(f"- {m.title} :: {m.text[:400]}" for m in members[:4])
    prompt = f"ЗАГОЛОВОК: {label}\n\nВЫДЕРЖКИ:\n{sources}\n\nКАНАЛЫ:\n{channels_block}"

    payload, response = await llm.complete_json(
        _CLASSIFY_SYSTEM,
        prompt,
        max_tokens=900,
        offline_fallback=_CLASSIFY_OFFLINE,
    )
    routes = payload.get("routes")
    if response.provider == "offline" or not isinstance(routes, list) or not routes:
        return _keyword_routes(label, members, available)
    return [r for r in routes if isinstance(r, dict) and r.get("channel_id")]


_NICHE_KEYWORDS: dict[str, set[str]] = {
    "tech": {"ai", "ии", "нейросет", "модель", "gpt", "нейросеть", "алгоритм", "чип",
             "процессор", "разработ", "программ", "софт", "приложени", "openai", "gpu"},
    "software": {"сервис", "приложени", "плагин", "интеграц", "тариф", "подписк", "инструмент",
                 "автоматиза", "api", "платформ", "релиз", "обновлени"},
    "business": {"бизнес", "экономик", "рынк", "инвест", "компани", "выручк", "инфляц",
                 "ставк", "рубл", "долл", "прибыл", "бюджет", "рынки", "клиент"},
    "crypto": {"крипт", "bitcoin", "ethereum", "блокчейн", "токен", "usdt", "defi",
               "кошел", "майнинг", "stablecoin", "ton"},
    "science": {"исследован", "учен", "наук", "теори", "эксперимент", "гипотез",
                "журнал", "cell", "геном", "космос", "физик", "хим"},
    "health": {"здоров", "медиц", "болезн", "исследован", "диет", "витамин", "вакцин",
               "терап", "симптом", "лекарств", "sleep", "мышц"},
    "local": {"город", "власт", "улиц", "пробк", "дорог", "отключен", "мероприят",
              "открыл", "застро", "транспорт", "район", "мэрия"},
}


def _stems(tokens: set[str], keywords: set[str]) -> int:
    """Count keyword hits allowing Russian inflection.

    `ставка` / `ставку` / `ставке` must all match the stem `ставк`, so a keyword
    counts when it is a prefix of a token (or vice versa). Exact set
    intersection — the obvious implementation — silently scores almost nothing
    on real Russian text and starves every non-English niche.
    """
    hits = 0
    matched: set[str] = set()
    for keyword in keywords:
        if keyword in matched:
            continue
        for token in tokens:
            if len(token) < 4 or len(keyword) < 3:
                continue
            if token.startswith(keyword) or keyword.startswith(token):
                hits += 1
                matched.add(keyword)
                break
    return hits


def _sample_angle(persona: Persona, rng, performance: dict | None = None) -> str:
    """Sample from persona weights, nudged by measured angle quality.

    The static weights remain the prior: a cold channel behaves exactly as
    configured. Once an angle has at least three measurable posts, its learned
    quality moves its weight by up to +/-25%, not enough to let a small sample
    erase editorial intent.
    """
    weights = {k: max(0.0, float(v)) for k, v in (persona.angle_weights or {"news": 1.0}).items()}
    measured = [
        float(stats.get("quality") or 0.0)
        for stats in (performance or {}).values()
        if int(stats.get("n") or 0) >= 3 and float(stats.get("quality") or 0.0) > 0
    ]
    baseline = sum(measured) / len(measured) if measured else 0.0
    if baseline > 0:
        for angle, stats in (performance or {}).items():
            if angle not in weights or int(stats.get("n") or 0) < 3:
                continue
            quality = float(stats.get("quality") or 0.0)
            if quality <= 0:
                continue
            factor = 0.75 + 0.5 * min(1.0, quality / baseline)
            weights[angle] *= factor
    options = list(weights)
    total = sum(weights.values())
    if not options or total <= 0:
        return persona.sample_angle(rng)
    return rng.choices(options, weights=[weights[k] for k in options], k=1)[0]


def _keyword_routes(label: str, members: list, available: list[Persona]) -> list[dict]:
    """Deterministic fallback: stem overlap between the story and each channel's
    niche keyword set, weighted by source authority.

    If the lexicon finds nothing, fall back to declared source affinity — a
    cluster fed only by `who_news` still belongs in a health channel even when
    the headline happens not to contain a lexicon word. Without this second
    pass a well-sourced niche can silently starve.
    """
    blob = " ".join([label] + [m.title + " " + m.text[:600] for m in members]).lower()
    tokens = set(content_tokens(blob))
    authority = max((m.source_weight for m in members), default=0.4)

    routes: list[dict] = []
    for persona in available:
        keywords = _NICHE_KEYWORDS.get(persona.niche)
        if not keywords:
            continue
        hits = _stems(tokens, keywords)
        if hits == 0:
            continue
        overlap = min(1.0, hits / 3.0)
        score = min(1.0, 0.40 * overlap + 0.45 * authority + 0.15)
        routes.append({"channel_id": persona.id, "score": round(score, 3), "angle": "news"})

    if routes:
        routes.sort(key=lambda r: r["score"], reverse=True)
        return routes

    hints: set[str] = set()
    for member in members:
        hints.update(getattr(member, "niche_hint", None) or [])
    for persona in available:
        if persona.niche in hints:
            score = round(min(1.0, 0.55 * authority + 0.35), 3)
            routes.append({"channel_id": persona.id, "score": score, "angle": "news"})
    routes.sort(key=lambda r: r["score"], reverse=True)
    return routes


# ------------------------------------------------------------------- cooldown
def channel_load(channel_id: str) -> dict:
    """Today's post count, last post time, and recent topic fingerprints."""
    now = datetime.now(UTC)
    midnight = datetime.combine(now.date(), datetime.min.time()).replace(tzinfo=UTC)

    with session_scope() as session:
        todays = session.execute(
            select(PostRow).where(
                PostRow.channel_id == channel_id,
                PostRow.created_at >= midnight,
                PostRow.status.in_(("gated", "scheduled", "publishing", "published")),
            )
        ).scalars().all()

        recent = session.execute(
            select(PostRow.hook, PostRow.body, PostRow.created_at, PostRow.embedding)
            .where(PostRow.channel_id == channel_id, PostRow.status.in_(("scheduled", "published")))
            .order_by(PostRow.created_at.desc())
            .limit(RECENT_HISTORY_LIMIT)
        ).all()

        covered = session.execute(
            select(ClusterRow.label, ClusterRow.last_seen_at, PostRow.created_at)
            .join(PostRow, PostRow.cluster_public_id == ClusterRow.public_id)
            .where(PostRow.channel_id == channel_id)
            .order_by(PostRow.created_at.desc())
            .limit(40)
        ).all()

    cutoff = now - timedelta(hours=TOPIC_COOLDOWN_HOURS)
    recent_topics = {
        label
        for label, seen, posted in covered
        if label and _aware(seen) and _aware(seen) > cutoff and _aware(posted) > cutoff
    }

    return {
        "posts_today": len(todays),
        "last_post_at": max((_aware(p.created_at) for p in todays), default=None),
        "recent_tokens": [set(content_tokens(f"{h} {b}")) for h, b, _, _ in recent],
        "recent_embeds": [v for v in (unpack(e) for _, _, _, e in recent) if v is not None],
        "covered_topics": recent_topics,
    }


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


# ---------------------------------------------------------------- anti-sameness
def sameness(text: str, history: dict) -> float:
    """Max similarity of `text` against a channel's recent output.

    Blends token Jaccard (interpretable) with embedding cosine (semantic). The
    weights renormalise over whichever signals are actually available, so a
    history with no stored vectors still produces a usable 0..1 score instead of
    being capped at the token weight.
    """
    if not text.strip():
        return 1.0
    candidate = set(content_tokens(text))
    token_signals = history.get("recent_tokens") or []
    vectors = [v for v in (history.get("recent_embeds") or []) if v is not None]

    if not token_signals and not vectors:
        return 0.0

    jac = max((jaccard(candidate, h) for h in token_signals), default=0.0)

    cosine = 0.0
    if vectors:
        embedder = get_embedder()
        stack = np.vstack(vectors)
        cosine = float((stack @ embedder.embed(text)).max())

    if token_signals and vectors:
        return round(0.45 * jac + 0.55 * cosine, 4)
    if token_signals:
        return round(jac, 4)
    return round(cosine, 4)


def _load_history_vectors(channel_id: str, limit: int = 120) -> list[np.ndarray]:
    from tgfarm.db.models import Post

    with session_scope() as session:
        rows = session.execute(
            select(Post.embedding)
            .where(Post.channel_id == channel_id, Post.embedding.isnot(None))
            .order_by(Post.created_at.desc())
            .limit(limit)
        ).scalars().all()
    return [v for v in (unpack(b) for b in rows) if v is not None]


# ------------------------------------------------------------------ routing
async def plan_cluster(cluster_public_id: str, *, rng: random.Random | None = None) -> list[ChannelPlan]:
    """Decide which channels take this cluster and at which angle.

    Destinations whose honest capability is `unsupported`/`read_only` are not
    planned at all; `draft_only`/`manual_upload` are planned with
    `requires_manual_action` set, because the last step belongs to a human.
    """
    rng = rng or random.Random(uuid.uuid4().hex)
    get_settings()
    config = get_config()
    # A report from a previous route of the same cluster must not be mistaken
    # for the current decision.
    _remember_skips(cluster_public_id, [])

    with session_scope() as session:
        cluster = session.execute(
            select(ClusterRow).where(ClusterRow.public_id == cluster_public_id)
        ).scalar_one_or_none()
        if cluster is None:
            log.warning("cluster %s not found", cluster_public_id)
            return []
        members = session.execute(
            select(RawItemRow).where(RawItemRow.cluster_id == cluster.id)
            .order_by(RawItemRow.source_weight.desc())
        ).scalars().all()
        members = list(members)
        label = cluster.label or (members[0].title if members else "")

    if not members:
        return []

    niche = cluster.niche
    telegram_available = [
        p for p in config.by_platform("telegram") if not p.catch_all
    ]
    if niche:
        telegram_available = [p for p in telegram_available if p.niche == niche]
    catch_all_personas = list(config.catch_all_personas)

    # Capability gate: a destination that cannot accept material is not a
    # candidate, and it does not reach the classifier prompt either — that would
    # burn tokens scoring a channel which can never publish.
    telegram_available, telegram_skipped = _capability_gate(telegram_available, cluster_public_id)
    catch_all_personas, catch_all_skipped = _capability_gate(catch_all_personas, cluster_public_id)
    skipped = telegram_skipped + catch_all_skipped
    _remember_skips(cluster_public_id, skipped)
    notes = _skips_note(skipped)

    if not telegram_available and not catch_all_personas:
        log.debug(
            "cluster %s niche=%s has no capable destination (skipped=%s)",
            cluster_public_id,
            niche,
            [item.channel_id for item in skipped],
        )
        return []

    routes = (
        await classify_niche(label, members, telegram_available)
        if telegram_available
        else []
    )
    try:
        from tgfarm.s8_analytics import angle_performance

        learned_angles = angle_performance(14)
    except Exception as exc:  # noqa: BLE001 - routing must survive analytics failure
        log.debug("angle feedback unavailable: %s", exc)
        learned_angles = {}
    try:
        from tgfarm.s12_learning import best_angle_weights as _best_angle_weights
    except Exception:  # noqa: BLE001
        _best_angle_weights = lambda _cid: None
    plans: list[ChannelPlan] = []

    with StageTimer("s3_route") as timer:
        for route in routes:
            persona = next(
                (p for p in telegram_available if p.id == route.get("channel_id")), None
            )
            if persona is None:
                continue
            score = float(route.get("score", 0.0) or 0.0)
            if score < 0.30:
                continue

            load = channel_load(persona.id)
            if load["posts_today"] >= persona.posts_per_day:
                log.debug("%s already at daily quota", persona.id)
                continue
            if load["covered_topics"] and label and any(
                label[:40].lower() in topic.lower() for topic in load["covered_topics"]
            ):
                log.debug("%s already covered this topic", persona.id)
                continue

            learned_persona = persona
            lw = _best_angle_weights(persona.id)
            if lw:
                import dataclasses
                learned_persona = dataclasses.replace(persona, angle_weights=lw)

            angle = route.get("angle")
            if angle not in (learned_persona.angle_weights or {"news": 1.0}):
                angle = _sample_angle(
                    learned_persona,
                    rng,
                    {k: v for k, v in learned_angles.items() if k == angle}
                    or learned_angles,
                )

            plans.append(
                ChannelPlan(
                    cluster_public_id=cluster_public_id,
                    channel_id=persona.id,
                    persona=learned_persona,
                    angle=angle,
                    score=score,
                    reason=f"niche={niche} score={score:.2f}",
                    notes=notes,
                    platform=persona.platform,
                    **_capability_fields(persona.platform),
                )
            )

        # Catch-all channels are deliberately outside niche classification: they
        # take every story, so a deterministic route is both correct and cheaper
        # than asking the model whether a general-interest channel wants it.
        for persona in catch_all_personas:
            load = channel_load(persona.id)
            if load["posts_today"] >= persona.posts_per_day:
                log.debug("%s already at daily quota", persona.id)
                continue
            if load["covered_topics"] and label and any(
                label[:40].lower() in topic.lower() for topic in load["covered_topics"]
            ):
                log.debug("%s already covered this topic", persona.id)
                continue

            learned_persona = persona
            lw = _best_angle_weights(persona.id)
            if lw:
                import dataclasses
                learned_persona = dataclasses.replace(persona, angle_weights=lw)

            # Semantic anti-repeat: label-level checks miss paraphrases, the
            # embedding does not. 0.85 against the channel's own last posts.
            try:
                repeat = sameness(label, load)
            except Exception:  # noqa: BLE001 - routing survives analytics
                repeat = 0.0
            if repeat >= 0.85:
                log.debug("%s: topic too close to recent output (%.2f)", persona.id, repeat)
                continue

            plans.append(
                ChannelPlan(
                    cluster_public_id=cluster_public_id,
                    channel_id=persona.id,
                    persona=learned_persona,
                    angle=_sample_angle(learned_persona, rng, learned_angles),
                    score=0.55,
                    reason="catch_all",
                    notes=notes,
                    platform=persona.platform,
                    **_capability_fields(persona.platform),
                )
            )

        plans.sort(key=lambda p: p.score, reverse=True)
        timer.items_in = len(routes) + len(catch_all_personas)
        timer.items_out = len(plans)

    # per-channel: if several clusters would land on the same channel today,
    # keep only the best ones up to the daily quota
    counts: dict[str, int] = {}
    trimmed: list[ChannelPlan] = []
    for plan in plans:
        used = counts.get(plan.channel_id, 0)
        if used >= max(1, plan.persona.posts_per_day // 2):
            continue
        counts[plan.channel_id] = used + 1
        trimmed.append(plan)
    return trimmed


def save_post_embedding(post_public_id: str) -> None:
    embedder = get_embedder()
    with session_scope() as session:
        row = session.execute(
            select(PostRow).where(PostRow.public_id == post_public_id)
        ).scalar_one_or_none()
        if row is None:
            return
        pack(embedder.embed(f"{row.hook}\n{row.body}"))


def today_posts_by_channel() -> dict[str, int]:
    today = datetime.now(UTC).date()
    with session_scope() as session:
        rows = session.execute(
            select(ChannelRow.id, PostRow.created_at)
            .join(PostRow, PostRow.channel_id == ChannelRow.id)
        ).all()
    counts: dict[str, int] = {}
    for channel_id, created in rows:
        if _aware(created).date() == today:
            counts[channel_id] = counts.get(channel_id, 0) + 1
    return counts


__all__ = [
    "ChannelPlan",
    "SkippedDestination",
    "classify_niche",
    "plan_cluster",
    "route_skips",
    "sameness",
    "save_post_embedding",
    "today_posts_by_channel",
]

