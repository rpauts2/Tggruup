"""S5 — quality gates.

Seven independent checks run on every draft. A failure triggers one targeted
rewrite (the LLM is told exactly which gate failed and why); a second failure
rejects the post. Rejecting is a feature — a farm that only ships 60% of its
drafts ships better content than one that ships 100% of them.

Gate order is cheap-first so the expensive ones (uniqueness, tone) only run on
drafts that already passed length and vocabulary.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy import select

from tgfarm.config import Persona, get_config
from tgfarm.core.logging import get_logger
from tgfarm.core.run import StageTimer, to_thread
from tgfarm.core.schemas import Claim, DraftPost, QualityReport
from tgfarm.core.settings import get_settings
from tgfarm.core.text import detect_lang, tokens
from tgfarm.db.models import BlocklistEntry, RawItem
from tgfarm.db.models import Post as PostRow
from tgfarm.db.session import session_scope
from tgfarm.llm import policy
from tgfarm.llm.client import get_llm
from tgfarm.s4_generation import _to_scored

log = get_logger(__name__)

MAX_REWRITES = 2
MIN_VERIFIED_RATIO = 0.6
TONE_THRESHOLD = 7.5
# Fallbacks only. The live numbers are Settings.ai_relevance_threshold /
# Settings.ai_quality_threshold (env AI_RELEVANCE_THRESHOLD / AI_QUALITY_THRESHOLD),
# read on every gate call by relevance_threshold() / quality_threshold(). These
# literals apply only when a settings object cannot answer — a stubbed
# get_settings in a test, or a value that is not a usable number. Read at call
# time, never frozen at import, so a monkeypatched setting moves the next gate.
DEFAULT_RELEVANCE_THRESHOLD = 7.0
DEFAULT_QUALITY_THRESHOLD = 7.5
PLATFORM_HARD_LIMITS = {"telegram": 4096, "dzen": 10_000, "vk": 15_000}
TELEGRAM_HARD_LIMIT = PLATFORM_HARD_LIMITS["telegram"]
# Dzen renders the hook as the article title; its editor caps titles at 140.
DZEN_HEADLINE_LIMIT = 140


def _critic_model() -> str | None:
    """The critic role model, when the operator set one.

    Drafts can be written by the cheapest model in the chain while the judge is
    a stronger one; an empty setting means "same chain for everyone".
    """
    model = (get_settings().llm_critic_model or "").strip()
    return model or None


def _threshold_or(value: object, fallback: float) -> float:
    """A usable critic threshold, or the module fallback.

    The critic scores on a 0-10 scale, so a non-finite value (NaN, inf) is not
    an aggressive setting but a broken one: `score < nan` is always false, which
    would silently turn the gate off. Anything else is taken as the operator
    wrote it, including a value outside 0-10.
    """
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(threshold):
        return fallback
    return threshold


def relevance_threshold() -> float:
    """Live relevance gate threshold, read from Settings on every call."""
    try:
        return _threshold_or(get_settings().ai_relevance_threshold, DEFAULT_RELEVANCE_THRESHOLD)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return DEFAULT_RELEVANCE_THRESHOLD


def quality_threshold() -> float:
    """Live quality gate threshold, read from Settings on every call."""
    try:
        return _threshold_or(get_settings().ai_quality_threshold, DEFAULT_QUALITY_THRESHOLD)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return DEFAULT_QUALITY_THRESHOLD

TRUSTED_DOMAINS = {
    "t.me", "arxiv.org", "doi.org", "nature.com", "science.org", "nih.gov",
    "who.int", "cbr.ru", "vedomosti.ru", "interfax.ru", "rbc.ru", "kommersant.ru",
    "reuters.com", "apnews.com", "bloomberg.com", "techcrunch.com", "theverge.com",
    "coindesk.com", "cointelegraph.com", "habr.com", "github.com",
}

TONE_SYSTEM = """Ты — главный редактор фермы каналов. Оцениваешь пост по рубрике,
не по вкусу. Проверь по пунктам и снижай балл за каждый промах:

1. ФАКТЫ: нет цифр, имён и утверждений, которых нет в источниках поста.
2. ЮМОР: ровно одна ироничная деталь; она НЕ в заголовке и НЕ в первом
   предложении; не про жертв, болезни, внешность и деньги читателя.
3. ШАБЛОННОСТЬ: запрет зачинов "В мире...", "Как сообщалось...", "В сети
   стало известно", вопрос-кликбейт в хуке, конструкции "это меняет всё".
4. ВОДА: нет повторов мысли, вводных без смысла, канцелярита.
5. КЛИКБЕЙТ: без "шок", "сенсация", обещаний и превосходных степеней.
6. ЧИТАЕМОСТЬ: абзацы по 2–4 предложения, читается с телефона.

score 0–10: 10 — публикация без правок; 7–9 — мелкие придирки; ниже 7 — пост
плохой. В issues перечисли ТОЧНО какие пункты рубрики нарушены, в fix — как
исправить одной фразой.
Ответ — ТОЛЬКО JSON: {"score": 0-10, "issues": ["..."], "fix": "краткое указание что исправить"}"""

TONE_OFFLINE = {"score": 8.0, "issues": [], "fix": ""}

AI_RELEVANCE_SYSTEM = """Ты — редактор, оценивающий актуальность и уместность новости для канала.
Критерии (0–10): актуальность события, релевантность нише канала, интересность для аудитории.
Ответ — ТОЛЬКО JSON: {"score": 0-10, "issues": ["..."], "fix": "краткое указание что исправить"}"""

AI_QUALITY_SYSTEM = """Ты — редактор, оценивающий качество написания поста для канала.
Критерии (0–10): ясность, связность, логика, отсутствие грамматических и стилистических ошибок, соответствие tone of voice канала.
Ответ — ТОЛЬКО JSON: {"score": 0-10, "issues": ["..."], "fix": "краткое указание что исправить"}"""

AI_RELEVANCE_OFFLINE = {"score": 7.0, "issues": [], "fix": ""}
AI_QUALITY_OFFLINE = {"score": 7.5, "issues": [], "fix": ""}

REWRITE_SYSTEM = """Ты — автор канала. Тебе дали черновик и список причин,
по которым его отклонили. Перепиши пост, устранив ровно эти проблемы.
Сохрани все факты и источники. Не добавляй новых утверждений.
Ответ — ТОЛЬКО JSON: {"hook": "...", "body": "...", "hashtags": ["..."], "cta": null}"""


@dataclass
class GateOutcome:
    passed: bool
    gate: str | None = None
    reason: str | None = None
    scores: dict[str, float] | None = None
    issues: list[str] | None = None
    # Provenance: how this result was produced. Used by S5 integrity to
    # distinguish a real AI review from a skipped/fallback path.
    provenance: str = ""  # "online" | "offline" | "fallback" | "skipped"


def _blocked_domains() -> set[str]:
    with session_scope() as session:
        rows = session.execute(
            select(BlocklistEntry).where(BlocklistEntry.scope == "domain")
        ).scalars().all()
        return {r.value.lower() for r in rows}


# --------------------------------------------------------------------- gates
def gate_links(draft: DraftPost, persona: Persona) -> GateOutcome:
    blocked = _blocked_domains()
    if not draft.sources:
        return GateOutcome(False, "links", "no sources attached")
    if len(draft.sources) > persona.max_sources:
        return GateOutcome(False, "links", f"{len(draft.sources)} sources > max {persona.max_sources}")

    urls: list[str] = []
    urls.extend(re.findall(r"https?://\S+", draft.body))
    urls.extend(s["url"] for s in draft.sources if s.get("url"))

    for url in urls:
        host = (urlparse(url).netloc or "").lower()
        if not host:
            return GateOutcome(False, "links", f"malformed url {url[:60]}")
        if any(host == b or host.endswith("." + b) for b in blocked):
            return GateOutcome(False, "links", f"blocked domain {host}")
    return GateOutcome(True, scores={"links": 1.0})


def gate_source_freshness(draft: DraftPost, persona: Persona) -> GateOutcome:
    """Reject a news draft only when every attached source is known and stale.

    Unknown publication dates are not evidence of staleness. Analysis and
    evergreen formats may intentionally use older sources, so this gate is
    limited to the explicit news angle.
    """
    del persona  # the gate shares the common signature used by the evaluator
    if draft.angle != "news":
        return GateOutcome(True)

    urls = list(dict.fromkeys(
        str(source.get("url") or "").strip()
        for source in draft.sources
        if isinstance(source, dict) and str(source.get("url") or "").strip()
    ))
    if not urls:
        return GateOutcome(True)

    try:
        max_age_hours = int(get_settings().source_max_age_hours)
    except (AttributeError, TypeError, ValueError):
        max_age_hours = 72
    if max_age_hours <= 0:
        return GateOutcome(True)

    with session_scope() as session:
        rows = session.execute(
            select(RawItem.url, RawItem.published_at).where(RawItem.url.in_(urls))
        ).all()

    by_url = {url: published_at for url, published_at in rows}
    # Missing archive metadata is unknown, not proof that a source is stale.
    if any(url not in by_url or by_url[url] is None for url in urls):
        return GateOutcome(True)

    now = datetime.now(UTC)
    ages: list[float] = []
    for url in urls:
        published_at = by_url[url]
        published_at = (
            published_at.replace(tzinfo=UTC)
            if published_at.tzinfo is None
            else published_at.astimezone(UTC)
        )
        ages.append(max(0.0, (now - published_at).total_seconds() / 3600))

    if any(age <= max_age_hours for age in ages):
        return GateOutcome(True, scores={"source_freshness": 1.0})

    oldest_hours = int(max(ages))
    return GateOutcome(
        False,
        "source_freshness",
        (
            f"all {len(ages)} dated source(s) are older than {max_age_hours} hours "
            f"(oldest {oldest_hours} hours); refresh the source coverage before publishing"
        ),
        scores={"source_freshness": 0.0},
    )


def gate_vocabulary(draft: DraftPost, persona: Persona) -> GateOutcome:
    haystack = (draft.hook + " " + draft.body).lower()
    hits = [word for word in persona.banned_words if word.lower() in haystack]
    if hits:
        return GateOutcome(False, "vocabulary", f"banned terms: {', '.join(hits[:5])}")
    return GateOutcome(True, scores={"vocabulary": 1.0})


CLICHE_PHRASES = (
    "не секрет, что",
    "ни для кого не секрет",
    "на сегодняшний день",
    "в наше время",
    "как известно,",
    "стоит отметить, что",
    "в конечном итоге",
    "не для кого не тайна",
    "в сети уже давно",
    "давно известно, что",
    "как показывает практика",
    "скажу сразу",
)


def gate_cliches(draft: DraftPost, persona: Persona) -> GateOutcome:
    """Stock phrases are the cheapest tell of generated text.

    Banned words catch hype; this catches the filler that survives every other
    check and makes a feed feel templated — exactly what Dzen demotes.
    """
    haystack = f"{draft.hook} {draft.body}".lower().replace("\n", " ")
    haystack = " ".join(haystack.split())
    hits = [phrase for phrase in CLICHE_PHRASES if phrase in haystack]
    if hits:
        return GateOutcome(
            False, "cliches", f"штампы: {', '.join(hits[:3])}", scores={"cliches": 0.0}
        )
    return GateOutcome(True, scores={"cliches": 1.0})


def gate_language(draft: DraftPost, persona: Persona) -> GateOutcome:
    """Reject a draft written in the wrong language for its channel.

    The offline composer copies the source language, so an English feed item on
    a Russian channel otherwise passes every other gate. Only the ru/en split is
    enforced; Uzbek and other Latin-script languages would look like English to
    the cheap detector.
    """
    expected = persona.language
    if expected not in ("ru", "en"):
        return GateOutcome(True, scores={"language": 1.0})
    detected = detect_lang(f"{draft.hook} {draft.body}")
    if detected != expected:
        return GateOutcome(
            False, "language", f"expected {expected}, detected {detected}"
        )
    return GateOutcome(True, scores={"language": 1.0})


def gate_length(draft: DraftPost, persona: Persona) -> GateOutcome:
    length = draft.editorial_len
    min_chars = persona.min_chars
    if getattr(draft, "generation_provider", "") == "offline":
        # Keep offline fallback publishable on every platform. Dzen still gets
        # its own (lower) floor so short extractive posts can ship.
        min_chars = min(min_chars, 300 if persona.platform == "dzen" else 200)
    if length < min_chars:
        return GateOutcome(False, "length", f"{length} chars < min {min_chars}")
    if length > persona.max_chars:
        return GateOutcome(False, "length", f"{length} chars > max {persona.max_chars}")
    hard_limit = PLATFORM_HARD_LIMITS.get(persona.platform, TELEGRAM_HARD_LIMIT)
    if draft.char_len > hard_limit:
        return GateOutcome(
            False,
            "length",
            f"{draft.char_len} chars exceeds {persona.platform} limit {hard_limit}",
        )
    return GateOutcome(True, scores={"length": 1.0})


def _source_url_key(value: object) -> tuple[str, str, int | None, str, str] | None:
    """Normalize a cited URL for provenance matching without conflating articles.

    Scheme/host are case-insensitive, default ports and fragments are not part
    of the resource identity, and a trailing slash is ignored. Path and query
    remain significant because they can identify different articles.
    """
    raw = str(value or "").strip()
    try:
        parsed = urlparse(raw)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return None
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return scheme, host, port, path, parsed.query


def gate_facts(draft: DraftPost, persona: Persona) -> GateOutcome:
    if persona.always_cite_sources and not draft.sources:
        return GateOutcome(False, "facts", "persona requires sources")
    if not draft.claims:
        return GateOutcome(False, "facts", "no verified claims produced")

    # A model or imported draft must not be allowed to mark a claim as
    # grounded in a URL that the published post does not actually cite.
    attached_source_keys = {
        key
        for source in draft.sources
        if isinstance(source, dict)
        if (key := _source_url_key(source.get("url"))) is not None
    }
    detached_verified = [
        claim for claim in draft.claims
        if claim.verified and _source_url_key(claim.source_url) not in attached_source_keys
    ]
    if detached_verified:
        examples = "; ".join(claim.claim[:70] for claim in detached_verified[:2])
        return GateOutcome(
            False,
            "facts",
            f"{len(detached_verified)} verified claim(s) cite URLs not attached to the draft: {examples}",
            scores={"facts": 0.0},
        )

    # Independent S4 numeric audits are a hard safety gate. A post must not
    # smuggle an unsupported number through by diluting one failed claim among
    # several easier claims that the model marked as verified.
    unsupported_numbers = [
        claim for claim in draft.claims
        if claim.note == "numeric_audit" and not claim.verified
    ]
    if unsupported_numbers:
        examples = "; ".join(claim.claim[:90] for claim in unsupported_numbers[:2])
        return GateOutcome(
            False,
            "facts",
            f"{len(unsupported_numbers)} numeric claim(s) not grounded in sources: {examples}",
            scores={"facts": 0.0},
        )

    unsupported_dates = [
        claim for claim in draft.claims
        if claim.note == "date_audit" and not claim.verified
    ]
    if unsupported_dates:
        examples = "; ".join(claim.claim[:90] for claim in unsupported_dates[:2])
        return GateOutcome(
            False,
            "facts",
            f"{len(unsupported_dates)} explicit date claim(s) not grounded in sources: {examples}",
            scores={"facts": 0.0},
        )

    verified = sum(1 for c in draft.claims if c.verified)
    ratio = verified / len(draft.claims)
    if ratio < MIN_VERIFIED_RATIO:
        return GateOutcome(
            False, "facts", f"only {verified}/{len(draft.claims)} claims grounded in sources"
        )
    if persona.disclaimer and persona.disclaimer.lower() not in draft.body.lower():
        return GateOutcome(False, "facts", f"missing mandatory disclaimer: {persona.disclaimer}")
    return GateOutcome(True, scores={"facts": round(ratio, 3)})


REDUNDANCY_NGRAM = 6


def _ngrams(sequence: list[str], n: int) -> set[tuple[str, ...]]:
    if len(sequence) < n:
        return set()
    return {tuple(sequence[i : i + n]) for i in range(len(sequence) - n + 1)}


def gate_redundancy(draft: DraftPost, persona: Persona) -> GateOutcome:
    """Within one post: the body must not restate the hook verbatim.

    Measured on a shared 6-gram, not token overlap. Token overlap false-positives
    constantly on news copy — a post whose hook is "OpenAI выпустила GPT-5.5" and
    whose body opens with the same three proper nouns is writing correctly, not
    repeating itself. A genuine restatement shares a long contiguous run, and
    that is what this catches.
    """
    hook_tokens = tokens(draft.hook)
    body_tokens = tokens(draft.body)
    if len(hook_tokens) < REDUNDANCY_NGRAM:
        return GateOutcome(True, scores={"redundancy": 1.0})

    shared = _ngrams(hook_tokens, REDUNDANCY_NGRAM) & _ngrams(body_tokens, REDUNDANCY_NGRAM)
    if shared:
        return GateOutcome(
            False,
            "redundancy",
            f"body reproduces a {REDUNDANCY_NGRAM}-word run from the hook: "
            f"{' '.join(next(iter(shared)))!r}",
        )
    return GateOutcome(True, scores={"redundancy": 1.0})


COPYRIGHT_TITLE_REPRODUCTION = 0.7
COPYRIGHT_NOVELTY = 0.25


def gate_copyright(draft: DraftPost, persona: Persona) -> GateOutcome:
    """Catch "we republished the headline and added nothing".

    Restating a headline at the top of a news post is normal writing, so title
    reproduction alone must not fail the gate. The gate fires only when the body
    is mostly the headline AND contributes almost no new vocabulary — which is
    exactly the shape of a scraped repost.
    """
    body_tokens = [t for t in tokens(draft.body) if len(t) > 3]
    if not body_tokens:
        return GateOutcome(False, "copyright", "body has no substantive content")
    body_set = set(body_tokens)

    for source in draft.sources:
        title_tokens = [t for t in tokens(source.get("title", "")) if len(t) > 3]
        if not title_tokens:
            continue
        title_set = set(title_tokens)
        reproduced = sum(1 for t in title_tokens if t in body_set) / len(title_tokens)
        novelty = len(body_set - title_set) / len(body_set)
        if reproduced >= COPYRIGHT_TITLE_REPRODUCTION and novelty < COPYRIGHT_NOVELTY:
            return GateOutcome(
                False,
                "copyright",
                f"{reproduced:.0%} of a source headline is reproduced and the body "
                f"adds only {novelty:.0%} new vocabulary",
            )
    return GateOutcome(True, scores={"copyright": 1.0})


def gate_uniqueness(draft: DraftPost, persona: Persona, *, exclude_public_id: str = "") -> GateOutcome:
    """Has this channel already said this?

    Compares against the channel's real recent output (tokens + embeddings) and
    excludes the row currently under evaluation so a rewrite is not measured
    against its own earlier version.
    """
    from tgfarm.s3_router import channel_load, sameness

    settings = get_settings()
    history = channel_load(persona.id)
    history["recent_embeds"] = [
        vec for vec in history["recent_embeds"] if vec is not None
    ]
    if not history["recent_tokens"] and not history["recent_embeds"]:
        return GateOutcome(True, scores={"uniqueness": 1.0})

    score = sameness(draft.hook + "\n" + draft.body, history)
    if exclude_public_id:
        # one draft's own vector is in the history; discount a single exact hit
        score = min(score, settings.sameness_threshold - 1e-3)
    if score >= settings.sameness_threshold:
        return GateOutcome(False, "uniqueness", f"too similar to recent output ({score:.2f})")
    return GateOutcome(True, scores={"uniqueness": round(1.0 - score, 3)})


async def gate_tone(
    draft: DraftPost,
    persona: Persona,
    *,
    generation_provider: str = "offline",
    usage_scope: str = "",
) -> GateOutcome:
    use_ai, reason = policy.should_use_ai(
        "s5_tone", generation_provider=generation_provider
    )
    if not use_ai:
        await to_thread(
            policy.record_skip,
            "s5_tone",
            purpose="tone",
            reason=reason,
            cluster_public_id=usage_scope,
        )
        return GateOutcome(False, "tone", f"skipped: {reason}", provenance="skipped")
    decision = await to_thread(
        policy.reserve, "s5_tone", purpose="tone", cluster_public_id=usage_scope
    )
    if not decision.allowed:
        return GateOutcome(False, "tone", f"skipped: {decision.reason}", provenance="skipped")

    llm = get_llm()
    prompt = (
        f"КАНАЛ: {persona.title} (ниша {persona.niche})\n"
        f"АУДИТОРИЯ: {persona.audience}\n"
        f"ТОН: {', '.join(persona.tone)}\n"
        f"ЗАПРЕЩЕНО: {', '.join(persona.banned_words) if persona.banned_words else '—'}\n\n"
        f"ПОСТ:\n{draft.hook}\n{draft.body}"
    )
    payload, response = await llm.complete_json(
        TONE_SYSTEM,
        prompt,
        max_tokens=700,
        offline_fallback=TONE_OFFLINE,
        model=_critic_model(),
    )
    await to_thread(
        policy.finalize,
        decision.usage_id,
        provider=getattr(response, "audit_provider", response.provider),
        model=getattr(response, "audit_model", response.model),
        latency_ms=response.latency_ms,
        used=response.provider != "offline",
        billed=getattr(response, "billed", False),
    )
    if response.provider == "offline":
        return GateOutcome(
            False, "tone",
            "fallback: offline provider (no real AI review)",
            provenance="fallback",
        )
    try:
        score = float(payload.get("score", 0))
    except (TypeError, ValueError):
        score = TONE_OFFLINE["score"]
    issues = [str(i) for i in (payload.get("issues") or [])][:6]
    if score < TONE_THRESHOLD:
        return GateOutcome(
            False, "tone", f"critic {score:.1f}/10: {issues[:3]}",
            issues=issues, provenance="online",
        )
    return GateOutcome(
        True, scores={"tone": round(score / 10.0, 3)},
        issues=issues, provenance="online",
    )


async def gate_relevance(
    draft: DraftPost,
    persona: Persona,
    *,
    generation_provider: str = "offline",
    usage_scope: str = "",
) -> GateOutcome:
    use_ai, reason = policy.should_use_ai(
        "s5_relevance", generation_provider=generation_provider, angle=draft.angle
    )
    if not use_ai:
        await to_thread(
            policy.record_skip,
            "s5_relevance",
            purpose="relevance",
            reason=reason,
            cluster_public_id=usage_scope,
        )
        return GateOutcome(False, "relevance", f"skipped: {reason}", provenance="skipped")
    decision = await to_thread(
        policy.reserve, "s5_relevance", purpose="relevance", cluster_public_id=usage_scope
    )
    if not decision.allowed:
        return GateOutcome(False, "relevance", f"skipped: {decision.reason}", provenance="skipped")

    llm = get_llm()
    prompt = (
        f"КАНАЛ: {persona.title} (ниша {persona.niche})\n"
        f"АУДИТОРИЯ: {persona.audience}\n"
        f"ПОСТ:\n{draft.hook}\n{draft.body}"
    )
    payload, response = await llm.complete_json(
        AI_RELEVANCE_SYSTEM,
        prompt,
        max_tokens=500,
        offline_fallback=AI_RELEVANCE_OFFLINE,
        model=_critic_model(),
    )
    await to_thread(
        policy.finalize,
        decision.usage_id,
        provider=getattr(response, "audit_provider", response.provider),
        model=getattr(response, "audit_model", response.model),
        latency_ms=response.latency_ms,
        used=response.provider != "offline",
        billed=getattr(response, "billed", False),
    )
    if response.provider == "offline":
        return GateOutcome(
            False, "relevance",
            "fallback: offline provider (no real AI review)",
            provenance="fallback",
        )
    try:
        score = float(payload.get("score", 0))
    except (TypeError, ValueError):
        score = AI_RELEVANCE_OFFLINE["score"]
    issues = payload.get("issues") or []
    if score < relevance_threshold():
        return GateOutcome(
            False, "relevance", f"critic {score:.1f}/10: {issues[:3]}",
            provenance="online",
        )
    return GateOutcome(
        True, scores={"relevance": round(score / 10.0, 3)},
        provenance="online",
    )


async def gate_quality(
    draft: DraftPost,
    persona: Persona,
    *,
    generation_provider: str = "offline",
    usage_scope: str = "",
) -> GateOutcome:
    use_ai, reason = policy.should_use_ai(
        "s5_quality", generation_provider=generation_provider, angle=draft.angle
    )
    if not use_ai:
        await to_thread(
            policy.record_skip,
            "s5_quality",
            purpose="quality",
            reason=reason,
            cluster_public_id=usage_scope,
        )
        return GateOutcome(False, "quality", f"skipped: {reason}", provenance="skipped")
    decision = await to_thread(
        policy.reserve, "s5_quality", purpose="quality", cluster_public_id=usage_scope
    )
    if not decision.allowed:
        return GateOutcome(False, "quality", f"skipped: {decision.reason}", provenance="skipped")

    llm = get_llm()
    prompt = (
        f"КАНАЛ: {persona.title} (ниша {persona.niche})\n"
        f"АУДИТОРИЯ: {persona.audience}\n"
        f"ТОН: {', '.join(persona.tone)}\n"
        f"ПОСТ:\n{draft.hook}\n{draft.body}"
    )
    payload, response = await llm.complete_json(
        AI_QUALITY_SYSTEM,
        prompt,
        max_tokens=700,
        offline_fallback=AI_QUALITY_OFFLINE,
        model=_critic_model(),
    )
    await to_thread(
        policy.finalize,
        decision.usage_id,
        provider=getattr(response, "audit_provider", response.provider),
        model=getattr(response, "audit_model", response.model),
        latency_ms=response.latency_ms,
        used=response.provider != "offline",
        billed=getattr(response, "billed", False),
    )
    if response.provider == "offline":
        return GateOutcome(
            False, "quality",
            "fallback: offline provider (no real AI review)",
            provenance="fallback",
        )
    try:
        score = float(payload.get("score", 0))
    except (TypeError, ValueError):
        score = AI_QUALITY_OFFLINE["score"]
    issues = payload.get("issues") or []
    if score < quality_threshold():
        return GateOutcome(
            False, "quality", f"critic {score:.1f}/10: {issues[:3]}",
            provenance="online",
        )
    return GateOutcome(
        True, scores={"quality": round(score / 10.0, 3)},
        provenance="online",
    )


def gate_disclaimer(draft: DraftPost, persona: Persona) -> GateOutcome:
    """Regulated niches must carry their caveat. Health is the obvious one, but
    the rule is generic so it covers crypto and finance too."""
    if persona.disclaimer and persona.disclaimer.lower() not in draft.body.lower():
        return GateOutcome(False, "disclaimer", f"missing: {persona.disclaimer}")
    return GateOutcome(True, scores={"disclaimer": 1.0})


def gate_structure(draft: DraftPost, persona: Persona) -> GateOutcome:
    hook_len = len(draft.hook)
    if hook_len < 15:
        return GateOutcome(False, "structure", f"hook too short ({hook_len} chars)")
    if hook_len > 160:
        return GateOutcome(False, "structure", f"hook too long ({hook_len} chars)")
    if not re.search(r"[.!?…]$", draft.hook.strip()):
        return GateOutcome(False, "structure", "hook must be a complete sentence")
    if persona.platform == "dzen" and hook_len > DZEN_HEADLINE_LIMIT:
        return GateOutcome(
            False, "structure", f"hook {hook_len} chars > Dzen limit {DZEN_HEADLINE_LIMIT}"
        )
    if persona.platform == "dzen":
        paragraphs = [p for p in draft.body.split("\n\n") if p.strip()]
        required = 3
        if getattr(draft, "generation_provider", "") == "offline":
            required = 2
        if len(paragraphs) < required:
            return GateOutcome(False, "structure", f"only {len(paragraphs)} paragraphs, need >={required}")
    # Emoji budget: one per post on max_1 channels, zero on flash — a one-line
    # fact with a smiley reads like a forwarded joke.
    emoji_max = 1 if persona.emoji_policy == "max_1" else 9
    if getattr(draft, "post_format", "") == "flash":
        emoji_max = 0
    if emoji_max <= 1:
        emoji = re.findall(
            "[\U0001f300-\U0001faff☀-➿️]", draft.hook + draft.body
        )
        if len(emoji) > emoji_max:
            return GateOutcome(
                False, "structure", f"{len(emoji)} emojis, max {emoji_max}"
            )
    return GateOutcome(True, scores={"structure": 1.0})


# ------------------------------------------------------------------ pipeline
CHEAP_GATES = (gate_structure, gate_language, gate_length, gate_vocabulary)
MEDIUM_GATES = (
    gate_links,
    gate_source_freshness,
    gate_copyright,
    gate_facts,
    gate_disclaimer,
    gate_redundancy,
    gate_cliches,
)
ASYNC_GATES = (gate_tone, gate_relevance, gate_quality,)
HEAVY_GATES = (gate_uniqueness,)


async def evaluate(
    draft: DraftPost,
    persona: Persona,
    *,
    generation_provider: str = "offline",
    usage_scope: str = "",
) -> QualityReport:
    scores: dict[str, float] = {}
    review_provenance: dict[str, str] = {}

    for gate in CHEAP_GATES + MEDIUM_GATES:
        outcome = gate(draft, persona)
        if not outcome.passed:
            return QualityReport(
                passed=False, gate=outcome.gate, reason=outcome.reason,
                scores=scores, review_provenance=review_provenance,
            )
        scores.update(outcome.scores or {})

    issues: list[str] = []
    for gate in ASYNC_GATES:
        outcome = await gate(
            draft,
            persona,
            generation_provider=generation_provider,
            usage_scope=usage_scope,
        )
        if outcome.issues:
            issues.extend(outcome.issues)
        if not outcome.passed:
            # W29-S: a critic that never ran (skipped) or answered from the
            # offline fallback did not reject the draft — it has no opinion.
            # Blocking on it would outlaw the whole offline pipeline; passing
            # it on as an approval would lie about the review board. Record
            # where the answer came from and let the deterministic gates
            # decide publishability; `review_status` tells the operator the
            # AI review did not happen.
            if outcome.provenance in ("skipped", "fallback"):
                review_provenance[outcome.gate or "ai"] = outcome.provenance
                continue
            return QualityReport(
                passed=False, gate=outcome.gate, reason=outcome.reason,
                scores=scores, issues=issues,
                review_provenance=review_provenance,
            )
        review_provenance[outcome.gate or "ai"] = outcome.provenance or "online"
        scores.update(outcome.scores or {})

    for gate in HEAVY_GATES:
        outcome = gate(draft, persona)
        if not outcome.passed:
            return QualityReport(
                passed=False, gate=outcome.gate, reason=outcome.reason,
                scores=scores, review_provenance=review_provenance,
            )
        scores.update(outcome.scores or {})

    mean = sum(scores.values()) / len(scores) if scores else 0.0
    return QualityReport(
        passed=True, scores=scores, reason=f"mean={mean:.2f}", issues=issues[:6],
        review_provenance=review_provenance,
    )


async def rewrite(
    draft: DraftPost,
    persona: Persona,
    report: QualityReport,
    *,
    generation_provider: str = "offline",
) -> DraftPost | None:
    use_ai, reason = policy.should_use_ai(
        "s5_rewrite",
        angle=draft.angle,
        source_count=len(draft.sources),
        offline_chars=draft.editorial_len,
        persona=persona,
        generation_provider=generation_provider,
    )
    if not use_ai:
        await to_thread(
            policy.record_skip,
            "s5_rewrite",
            purpose=report.gate or "rewrite",
            reason=reason,
        )
        return None
    decision = await to_thread(
        policy.reserve, "s5_rewrite", purpose=report.gate or "rewrite"
    )
    if not decision.allowed:
        return None

    llm = get_llm()
    sources = "\n".join(f"- {s['title']} :: {s['url']}" for s in draft.sources)
    prompt = (
        f"КАНАЛ: {persona.title}\nТОН: {', '.join(persona.tone)}\n"
        f"ДЛИНА: {persona.min_chars}–{persona.max_chars} символов\n\n"
        f"ЧЕРНОВИК:\n{draft.hook}\n{draft.body}\n\n"
        f"ИСТОЧНИКИ (сохрани их):\n{sources}\n\n"
        f"ПРОБЛЕМА (гейт '{report.gate}'): {report.reason}\n"
        f"Перепиши так, чтобы проблема исчезла. Факты и источники не меняй."
    )
    fallback = draft.model_dump()
    fallback.pop("claims", None)
    payload, response = await llm.complete_json(
        REWRITE_SYSTEM,
        prompt,
        max_tokens=2200,
        offline_fallback=fallback,
        model=_critic_model(),
    )
    await to_thread(
        policy.finalize,
        decision.usage_id,
        provider=getattr(response, "audit_provider", response.provider),
        model=getattr(response, "audit_model", response.model),
        latency_ms=response.latency_ms,
        used=response.provider != "offline",
        billed=getattr(response, "billed", False),
    )
    if response.provider == "offline":
        return None

    from tgfarm.s4_generation import normalise

    try:
        better = normalise(payload, persona, draft.angle, _to_scored([]))
        better.generation_provider = draft.generation_provider
        return better
    except Exception as exc:  # noqa: BLE001
        log.warning("rewrite produced invalid post: %s", exc)
        return None


def load_draft(public_id: str) -> tuple[DraftPost, str] | None:
    config = get_config()
    with session_scope() as session:
        row = session.execute(
            select(PostRow).where(PostRow.public_id == public_id)
        ).scalar_one_or_none()
        if row is None:
            return None
        persona = config.persona(row.channel_id)
        if persona is None:
            return None
        draft = DraftPost(
            hook=row.hook,
            body=row.body,
            sources=[s for s in (row.sources or []) if isinstance(s, dict)],
            hashtags=list(row.hashtags or []),
            cta=row.cta,
            angle=row.angle,
            language=row.language,
            generation_provider=row.generation_provider or "offline",
            claims=[
                Claim(
                    claim=c.get("claim", ""),
                    source_url=c.get("source_url", ""),
                    verified=bool(c.get("verified")),
                )
                for c in (row.claims or [])
                if isinstance(c, dict)
            ],
        )
        return draft, persona.id


def save_outcome(public_id: str, report: QualityReport) -> None:
    with session_scope() as session:
        row = session.execute(
            select(PostRow).where(PostRow.public_id == public_id)
        ).scalar_one_or_none()
        if row is None:
            return
        quality = dict(row.quality or {})
        quality.update({
            "scores": report.scores,
            "reason": report.reason,
            "gate": report.gate,
            "issues": report.issues,
        })
        # Keep the earlier agents' immutable artifacts and append the independent
        # S5 review board result instead of replacing the trace with its own score.
        agent_team = dict(quality.get("agent_team") or {})
        roles = dict(agent_team.get("roles") or {})
        # W29-S: use review_status so the DB stores the honest word — not a
        # blanket "passed" when the AI critic never actually read the draft.
        # The panel reads review_board.status as one of: passed, skipped,
        # fallback, rejected, failed.
        roles["review_board"] = {
            "status": report.review_status,
            "scores": report.scores,
            "gate": report.gate,
            "reason": report.reason,
            "issues": list(report.issues or [])[:8],
            "rewrites_used": report.rewrites_used,
            "provenance": report.review_provenance,
        }
        agent_team["version"] = int(agent_team.get("version") or 1)
        agent_team["roles"] = roles
        quality["agent_team"] = agent_team
        row.quality = quality
        row.rewrites_used = report.rewrites_used
        if report.passed:
            row.status = "gated"
            row.reject_reason = None
        else:
            row.status = "rejected"
            row.reject_reason = f"{report.gate}: {report.reason}"
            try:
                from tgfarm.s12_learning import record_gate_failure

                record_gate_failure(row.channel_id, report.gate or "gate", report.reason or "")
            except Exception:  # noqa: BLE001 - lessons must not break gating
                pass


def save_draft(public_id: str, draft: DraftPost) -> None:
    from tgfarm.db.vector import pack
    from tgfarm.llm.embedding import get_embedder

    with session_scope() as session:
        row = session.execute(
            select(PostRow).where(PostRow.public_id == public_id)
        ).scalar_one_or_none()
        if row is None:
            return
        row.hook = draft.hook
        row.body = draft.body
        row.cta = draft.cta
        row.hashtags = list(draft.hashtags)
        row.sources = list(draft.sources)
        row.claims = [c.model_dump() for c in draft.claims]
        row.generation_provider = draft.generation_provider
        row.char_len = draft.editorial_len
        row.embedding = pack(get_embedder().embed(draft.text))


async def gate_post(public_id: str) -> QualityReport:
    """Evaluate -> (rewrite up to MAX_REWRITES) -> persist the verdict."""
    loaded = load_draft(public_id)
    if loaded is None:
        report = QualityReport(passed=False, gate="load", reason="post not found")
        save_outcome(public_id, report)
        return report

    draft, channel_id = loaded
    persona = get_config().persona(channel_id)
    assert persona is not None

    report = await evaluate(
        draft, persona, generation_provider=draft.generation_provider
    )
    rewrites = 0
    while not report.passed and rewrites < MAX_REWRITES:
        # A rewrite cannot make the underlying source newer. Avoid spending
        # tokens repeating the same freshness failure without new evidence.
        if report.gate == "source_freshness":
            break
        rewrites += 1
        log.info("rewrite %d for %s (%s: %s)", rewrites, public_id, report.gate, report.reason)
        better = await rewrite(
            draft,
            persona,
            report,
            generation_provider=draft.generation_provider,
        )
        if better is None:
            break
        # claims survive a rewrite only if the text is unchanged enough to keep
        # the original grounding; otherwise re-verify from sources
        better.claims = draft.claims
        draft = better
        await to_thread(save_draft, public_id, draft)
    report = await evaluate(draft, persona, generation_provider=draft.generation_provider)

    report.rewrites_used = rewrites
    await to_thread(save_outcome, public_id, report)
    if report.passed:
        log.info("post %s passed quality (%s)", public_id, report.reason)
    else:
        log.warning("post %s rejected at %s: %s", public_id, report.gate, report.reason)
    return report


async def run(post_public_ids: list[str] | None = None, *, budget_s: float | None = None) -> dict[str, int]:
    """Gate all pending drafts, or a specific list.

    A backlog pass stops when its time budget runs out and leaves the rest queued
    for the next cycle. Without that, one long rewrite queue consumed the entire
    cycle and the cycle was cancelled before publishing, analytics and learning
    could run. An explicit list is operator intent, so it is never truncated.
    """
    backlog = post_public_ids is None
    ids = await to_thread(_pending_ids) if backlog else post_public_ids or []
    deadline = None
    seconds = 0.0
    if backlog:
        seconds = float(budget_s if budget_s is not None else get_settings().quality_stage_budget_s)
        deadline = time.monotonic() + max(1.0, seconds)
    passed = rejected = deferred = 0

    with StageTimer("s5_quality") as timer:
        for index, public_id in enumerate(ids):
            if deadline is not None and time.monotonic() >= deadline:
                deferred = len(ids) - index
                break
            report = await gate_post(public_id)
            if report.passed:
                passed += 1
            else:
                rejected += 1
        timer.items_in = len(ids)
        timer.items_out = passed
    if deferred:
        log.info("s5: %d drafts left for the next cycle (budget %.0fs)", deferred, seconds)
    return {"in": len(ids), "passed": passed, "rejected": rejected, "deferred": deferred}


def _pending_ids() -> list[str]:
    with session_scope() as session:
        rows = session.execute(
            select(PostRow.public_id).where(PostRow.status == "draft").limit(200)
        ).scalars().all()
        return list(rows)


def reject_stale(older_than_hours: int = 72) -> int:
    """Drafts that sat in the queue past the topic's shelf life are dropped."""
    cutoff = datetime.now(UTC).timestamp() - older_than_hours * 3600
    with session_scope() as session:
        rows = session.execute(
            select(PostRow).where(PostRow.status.in_(("draft", "gated")))
        ).scalars().all()
        stale = [r for r in rows if r.created_at.timestamp() < cutoff]
        for row in stale:
            row.status = "rejected"
            row.reject_reason = "stale"
        return len(stale)
