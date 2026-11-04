"""S10 — format planner.

Decides *how* to present a story before a single word is written. Telegram readers
skim, and the four formats have measurably different performance profiles:

| format          | when                                              | target length |
|-----------------|---------------------------------------------------|---------------|
| `flash`         | one fact, high velocity, visual subject           | 150–320       |
| `stat`          | the story is a number                             | 200–420       |
| `news`          | a clean, well-sourced event                       | 400–700       |
| `analysis`      | the story needs interpretation, several outlets   | 600–1100      |
| `article`       | rich source material, evergreen topic             | 1200–2500     |
| `video`         | licensed footage is attached to the story         | 200–600       |

The decision is rule-based over signals the farm already has (outlet count,
source text volume, number density, niche, persona length budget, media
attached). An LLM override is available but optional — the rules are auditable,
which is what matters when the output is published under the channel's name.

`plan_with_history` answers the neighbouring question: which format has actually
worked on *this* destination. It keeps the rule-based plan as the base, lets
measured per-format engagement override it only when the sample is large enough,
and never claims automatic delivery for a destination whose capability is below
`publish`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from tgfarm.config import Persona
from tgfarm.core.logging import get_logger
from tgfarm.core.schemas import Cluster, ItemScored
from tgfarm.platform_capability import CAPABILITY_LABELS, CAPABILITY_NOTES, capability_of

log = get_logger(__name__)

Format = Literal["flash", "stat", "news", "analysis", "article", "video"]

LENGTH_BUDGET: dict[Format, tuple[int, int]] = {
    "flash": (150, 320),
    "stat": (200, 420),
    "news": (400, 700),
    "analysis": (600, 1100),
    "article": (1200, 2500),
    "video": (200, 600),
}

FORMAT_LABEL: dict[str, str] = {
    "flash": "Картинка + короткий текст",
    "stat": "Цифра крупно + комментарий",
    "news": "Новость",
    "analysis": "Разбор",
    "article": "Статья",
    "video": "Видео + короткий текст",
}

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*\s*(?:%|млрд|млн|тыс|руб|USD|EUR|\$)?", re.I)
_PERSONA = {"ru", "uz", "en"}


@dataclass
class FormatPlan:
    fmt: str = "news"
    reason: str = ""
    confidence: float = 0.5
    min_chars: int = 400
    max_chars: int = 700
    needs_art: bool = True
    needs_video: bool = False
    outline: list[str] = field(default_factory=list)
    signals: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _plan(fmt: str, reason: str, confidence: float, outline: list[str] | None = None,
          needs_video: bool = False, **signals) -> FormatPlan:
    low, high = LENGTH_BUDGET[fmt]
    return FormatPlan(
        fmt=fmt,
        reason=reason,
        confidence=round(confidence, 2),
        min_chars=low,
        max_chars=high,
        needs_art=True,
        needs_video=needs_video,
        outline=outline or [],
        signals={k: round(float(v), 3) for k, v in signals.items()},
    )


def available_text(items: list[ItemScored]) -> int:
    """Characters of source body actually available to write from."""
    return sum(len(item.text or "") for item in items)


def number_density(text: str) -> float:
    """Numeric tokens per 100 characters."""
    if not text:
        return 0.0
    return len(_NUMBER_RE.findall(text)) * 100.0 / max(1, len(text))


def distinct_outlets(items: list[ItemScored]) -> int:
    return len({item.source_id for item in items})


def plan_format(
    items: list[ItemScored],
    persona: Persona,
    *,
    has_video: bool = False,
    cluster_heat: float = 0.0,
) -> FormatPlan:
    """Choose the format. Deterministic and inspectable."""
    if not items:
        return _plan("news", "empty cluster", 0.0)

    blob = " ".join(f"{i.title} {i.text}" for i in items)
    text_chars = available_text(items)
    outlets = distinct_outlets(items)
    density = number_density(blob)
    media_kinds = {getattr(i, "media_kind", None) for i in items}

    signals = {
        "outlets": outlets,
        "text_chars": text_chars,
        "number_density": density,
        "heat": cluster_heat,
        "persona_max": persona.max_chars,
        "persona_min": persona.min_chars,
        "has_video": 1.0 if has_video else 0.0,
        "has_image": 1.0 if "image" in media_kinds else 0.0,
    }

    # 1. licensed video attached wins outright
    if has_video:
        return _plan(
            "video",
            "к источнику прикреплено видео",
            0.8,
            outline=[
                "Одним предложением: что случилось.",
                "2–4 предложения контекста, без пересказа видео.",
                "Ссылка на первоисточник.",
            ],
            needs_video=True,
            **signals,
        )

    # 2. one outlet, one fact, lots of heat -> flash
    if outlets <= 1 and text_chars < 900 and cluster_heat >= 0.6:
        return _plan(
            "flash",
            "один источник, короткий текст, высокий heat",
            0.7,
            outline=["Факт в первом предложении.", "Одна деталь. Ссылка."],
            **signals,
        )

    # 3. number-dominated story -> stat card
    if density >= 1.1 and text_chars < 3500:
        return _plan(
            "stat",
            f"плотность чисел {density:.1f} на 100 символов",
            0.75,
            outline=["Цифра крупно.", "Что она значит в двух предложениях.", "Источник."],
            **signals,
        )

    # 4. rich material + several outlets -> article
    if text_chars >= 4500 and outlets >= 2:
        return _plan(
            "article",
            f"{text_chars} символов источников, {outlets} издания",
            0.72,
            outline=[
                "Лид: зачем читать.",
                "Что произошло — 3–4 абзаца.",
                "Почему это важно — 2–3 абзаца.",
                "Что дальше / что делать.",
                "Источники.",
            ],
            **signals,
        )

    # 5. several outlets, decent text -> analysis
    if outlets >= 2 and text_chars >= 2000:
        return _plan(
            "analysis",
            f"{outlets} издания, {text_chars} символов",
            0.7,
            outline=[
                "Что произошло.",
                "Три причины / фактора.",
                "К кому это относится и что с этим делать.",
                "Источники.",
            ],
            **signals,
        )

    # 6. single outlet with usable text -> news
    if text_chars >= 800:
        return _plan(
            "news",
            f"один источник, {text_chars} символов",
            0.65,
            outline=["Лид.", "Суть в 3 абзацах.", "Источник."],
            **signals,
        )

    # 7. headline-only feed -> flash is the honest format, gates will judge length
    return _plan(
        "flash",
        f"источник дал только заголовок ({text_chars} символов)",
        0.55,
        outline=["Факт из заголовка + контекст из кластера.", "Ссылка."],
        **signals,
    )


# A demoted format is only worth it if the persona can actually write the shorter
# format, not merely scrape its floor. Requiring headroom keeps a 600-character
# channel on `news` (400–700) instead of an `analysis` pinned to exactly 600.
HEADROOM = 100


def fit_persona(plan: FormatPlan, persona: Persona) -> FormatPlan:
    """Clamp the plan to what the persona will accept.

    A persona that caps at 600 characters cannot publish an `article`; better to
    demote the format than to fail the length gate on every such post.
    """
    if plan.fmt in ("article", "analysis") and persona.max_chars < LENGTH_BUDGET[plan.fmt][0]:
        for fallback in ("analysis", "news", "stat", "flash"):
            floor, ceiling = LENGTH_BUDGET[fallback]
            if persona.max_chars >= floor + HEADROOM:
                plan.fmt = fallback
                # reset the budget: the clamp below must use the *fallback*
                # format's range, not the long format's
                plan.min_chars, plan.max_chars = floor, ceiling
                plan.needs_video = fallback == "video"
                plan.reason += (
                    f"; понижено до «{FORMAT_LABEL[fallback].lower()}»: "
                    f"лимит персоны {persona.max_chars} симв."
                )
                break

    # A channel with a high floor cannot publish a `flash` entry: clamping the
    # ceiling down to the floor would leave a 350-350 "flash" that is neither
    # short nor an article. Promote to the smallest format that fits the floor.
    if persona.min_chars > LENGTH_BUDGET[plan.fmt][1]:
        for fallback in ("news", "analysis", "article"):
            floor, ceiling = LENGTH_BUDGET[fallback]
            if persona.min_chars <= ceiling and persona.max_chars >= floor:
                plan.fmt = fallback
                plan.min_chars, plan.max_chars = floor, ceiling
                plan.needs_video = False
                plan.reason += (
                    f"; повышено до «{FORMAT_LABEL[fallback].lower()}»: "
                    f"минимум персоны {persona.min_chars} симв."
                )
                break

    low = max(plan.min_chars, persona.min_chars)
    high = min(plan.max_chars, persona.max_chars)
    if low > high:
        high = low

    plan.min_chars = low
    plan.max_chars = high
    return plan


def plan_for_cluster(
    cluster: Cluster,
    persona: Persona,
    *,
    has_video: bool = False,
) -> FormatPlan:
    items = cluster.items
    plan = plan_format(items, persona, has_video=has_video, cluster_heat=cluster.heat)
    return fit_persona(plan, persona)


# --------------------------------------------------------- history-aware plan
#: Published posts a format needs before its average engagement may move the
#: decision. Below this the sample cannot outvote the rules, and saying so is
#: more useful than pretending the data is conclusive.
MIN_HISTORY_SAMPLE = 3

#: What the last mile looks like when the farm itself cannot press "publish".
_MANUAL_REASONS: dict[str, str] = {
    "manual_upload": "готовим пакет для ручной загрузки — публикует человек",
    "draft_only": "готовим черновик в редакторе площадки — финальную кнопку нажимает человек",
    "read_only": "площадка только для чтения — материал остаётся черновиком",
    "unsupported": "у площадки нет рабочего сценария — материал остаётся черновиком",
}


def _normalise_media_kinds(media_kinds: Iterable[str] | str | None) -> set[str]:
    """Media kinds as a lowercase set; `None` and blanks carry no information."""
    if media_kinds is None:
        return set()
    if isinstance(media_kinds, str):
        media_kinds = [media_kinds]
    return {str(kind).strip().lower() for kind in media_kinds if str(kind).strip()}


def _as_sample_size(row: Mapping[str, Any]) -> int:
    """Posts behind a history row: `measured` first, then `n`, then `published`.

    Measured outranks the other keys on purpose, and a measured count of zero
    counts as zero rather than as «no such key»: twelve posts nobody ever
    measured is no evidence at all. Callers that only know a raw count still get
    the old behaviour through `n`/`published`.
    """
    for key in ("measured", "n", "published"):
        if key not in row:
            continue
        value = row.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            size = int(float(value))
        except (TypeError, ValueError):
            continue
        return max(0, size)
    return 0


def _as_count(row: Mapping[str, Any], key: str) -> int | None:
    """A positive integer under `key`, or None when the row does not carry it."""
    value = row.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        size = int(float(value))
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


def _as_engagement(row: Mapping[str, Any]) -> float | None:
    """Measured average engagement, or `None` when the row cannot be trusted."""
    value = row.get("avg_engagement")
    if value is None or isinstance(value, bool):
        return None
    try:
        engagement = float(value)
    except (TypeError, ValueError):
        return None
    if engagement != engagement or engagement < 0:  # NaN or negative = unusable
        return None
    return engagement


def _history_winner(
    history: Mapping[str, Mapping[str, Any]] | None,
    *,
    allow_video: bool,
) -> dict[str, Any] | None:
    """Best-performing format with enough evidence behind it, else `None`.

    A row is usable only when it names a format this module can size, carries at
    least `MIN_HISTORY_SAMPLE` posts and a real non-negative average engagement.
    `video` is skipped unless licensed footage exists: a format the farm cannot
    produce is not a format it may choose. Ties break on the larger sample, then
    on the documented format order, so one history always yields one winner.
    """
    if not history:
        return None
    order = list(LENGTH_BUDGET)
    rows: list[tuple[float, int, str]] = []
    for raw_format, row in history.items():
        fmt = str(raw_format or "").strip().lower()
        if fmt not in LENGTH_BUDGET or not isinstance(row, Mapping):
            continue
        if fmt == "video" and not allow_video:
            continue
        size = _as_sample_size(row)
        engagement = _as_engagement(row)
        if engagement is None or size < MIN_HISTORY_SAMPLE:
            continue
        rows.append((engagement, size, fmt))
    if not rows:
        return None
    engagement, size, fmt = max(rows, key=lambda row_: (row_[0], row_[1], -order.index(row_[2])))
    return {"format": fmt, "n": size, "avg_engagement": round(engagement, 4)}


def _history_overview(
    history: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, dict[str, float | int | None]]:
    """Echo of the parsed history, so a panel can show what the decision saw.

    `measured` and `published` stay separate: a panel that shows only one number
    cannot tell a well-measured format from a rarely-measured one.
    """
    overview: dict[str, dict[str, float | int | None]] = {}
    for raw_format, row in (history or {}).items():
        if not isinstance(row, Mapping):
            continue
        fmt = str(raw_format or "").strip().lower()
        overview[fmt] = {
            "n": _as_sample_size(row),
            "measured": _as_count(row, "measured"),
            "published": _as_count(row, "published"),
            "avg_engagement": _as_engagement(row),
        }
    return overview


def _history_confidence(size: int) -> float:
    """Confidence grows with the sample but never pretends to be near-certain."""
    return round(min(0.9, 0.5 + 0.05 * min(int(size), 8)), 2)


def _capability_level(platform_id: str) -> str:
    """Honest level, defaulting to the lowest one: an unknown status is not `publish`."""
    try:
        return capability_of(platform_id)
    except Exception as exc:  # noqa: BLE001 - a broken catalogue must not become a promise
        log.debug("capability lookup failed for %s: %s", platform_id, exc)
        return "unsupported"


def _manual_reason(capability: str, platform_id: str) -> str:
    hint = _MANUAL_REASONS.get(
        capability, "автопубликации нет — материал готовит ферма, отправляет человек"
    )
    return f"{platform_id or 'площадка'}: {hint}"


def _retarget(plan: FormatPlan, fmt: str) -> FormatPlan:
    """Move a fitted plan to another format, taking that format's length window.

    The outline belongs to the old format, so it is dropped rather than reused:
    a `flash` outline under an `analysis` heading is worse than no outline.
    """
    low, high = LENGTH_BUDGET[fmt]
    if plan.fmt != fmt:
        plan.outline = []
    plan.fmt = fmt
    plan.min_chars, plan.max_chars = low, high
    plan.needs_video = fmt == "video"
    return plan


def plan_with_history(
    persona: Persona,
    *,
    platform_id: str,
    media_kinds: Iterable[str] | str | None,
    topic: str,
    history: Mapping[str, Mapping[str, Any]] | None = None,
    cluster: Cluster | None = None,
) -> dict[str, Any]:
    """Plan a format from the rules, measured history and the platform contract.

    `plan_for_cluster` answers "what does this story look like"; this answers
    "what should we write *here*". The base is not re-derived:

    * format, length and outline come from `plan_format` + `fit_persona`, or
      straight from `plan_for_cluster` when a cluster is supplied;
    * `history` (`{format: {published, measured, avg_engagement}}`) may override
      the format only when a row carries at least `MIN_HISTORY_SAMPLE` *measured*
      posts — published-but-never-measured posts are activity, not evidence.
      `n` is still read as the sample size for callers without a measured count,
      and `published` is never preferred over it. Below the threshold the base
      choice stands and the reason says «мало данных»;
    * a destination whose capability is not `publish` keeps the same length
      window and is flagged `manual=True`, because a human finishes delivery;
    * nothing is invented: no history means the base plan plus an honest reason,
      and every returned format carries its `LENGTH_BUDGET` window.

    Without a cluster there are no sources, so the rules fall back to the
    documented `news`/confidence-0 plan; `media_kinds` still feeds `has_video`
    and is echoed, but it cannot make the planner claim a video it has no story
    for. `topic` is echoed and measured, never used as a fabricated signal.

    Keys: `format`, `min_chars`, `max_chars`, `reason`, `source`
    (`base`|`history`), `confidence`, plus `manual`, `manual_reason`,
    `capability`, `capability_label`, `capability_note`, `format_label`,
    `needs_art`, `needs_video`, `outline`, `signals`, `topic`, `media_kinds`,
    `history`. `source` names the evidence that settled the returned format:
    `history` when a sufficient sample existed (whether it replaced or merely
    confirmed the base choice), `base` when only the rules could speak.
    """
    kinds = _normalise_media_kinds(media_kinds)
    topic_text = str(topic or "").strip()
    has_video = "video" in kinds or any(
        getattr(item, "media_kind", None) == "video" for item in (cluster.items if cluster else [])
    )

    if cluster is not None:
        plan = plan_for_cluster(cluster, persona, has_video=has_video)
    else:
        plan = fit_persona(plan_format([], persona, has_video=has_video), persona)

    winner = _history_winner(history, allow_video=has_video)
    if winner is None:
        tail = (
            f"мало данных: ни у одного формата нет {MIN_HISTORY_SAMPLE}+ публикаций"
            if history
            else "мало данных: истории по форматам нет"
        )
        plan.reason = f"{plan.reason}; {tail}".strip("; ")
    else:
        label = FORMAT_LABEL.get(winner["format"], winner["format"]).lower()
        stats = f"engagement {winner['avg_engagement']:.2f} на {winner['n']} публикациях"
        if winner["format"] != plan.fmt:
            plan = _retarget(plan, winner["format"])
            # set the reason first: fit_persona appends its own demotion note to it
            plan.reason = f"история: «{label}» — лучший {stats}"
            plan = fit_persona(plan, persona)
            if plan.fmt == winner["format"]:
                plan.confidence = _history_confidence(winner["n"])
            else:
                plan.confidence = round(_history_confidence(winner["n"]) * 0.6, 2)
        else:
            plan.reason = f"{plan.reason}; история подтверждает «{label}» ({stats})"

    source = "history" if winner is not None else "base"
    confidence = round(float(plan.confidence), 2)

    capability = _capability_level(platform_id)
    platform_key = str(platform_id or "").strip().lower()
    manual = capability != "publish"
    manual_reason = _manual_reason(capability, platform_key) if manual else ""
    if manual_reason:
        plan.reason = f"{plan.reason}; {manual_reason}"

    # The capability flag never touches the length window: a manual package is
    # written to the same budget as an automatic post.
    if plan.min_chars > plan.max_chars:
        plan.max_chars = plan.min_chars
    plan.min_chars = max(1, int(plan.min_chars))
    plan.max_chars = max(plan.min_chars, int(plan.max_chars))

    signals = dict(plan.signals)
    signals.update(
        {
            "has_video": 1.0 if has_video else 0.0,
            "has_image": 1.0 if "image" in kinds else 0.0,
            "topic_chars": float(len(topic_text)),
        }
    )

    return {
        "format": plan.fmt,
        "min_chars": plan.min_chars,
        "max_chars": plan.max_chars,
        "reason": plan.reason,
        "source": source,
        "confidence": confidence,
        "manual": manual,
        "manual_reason": manual_reason,
        "capability": capability,
        "capability_label": CAPABILITY_LABELS.get(capability, capability),
        "capability_note": CAPABILITY_NOTES.get(capability, ""),
        "platform_id": platform_key,
        "format_label": FORMAT_LABEL.get(plan.fmt, plan.fmt),
        "needs_art": plan.needs_art,
        "needs_video": plan.needs_video,
        "outline": list(plan.outline),
        "signals": signals,
        "topic": topic_text,
        "media_kinds": sorted(kinds),
        "history": _history_overview(history),
    }


def as_format_plan(plan: Mapping[str, Any]) -> FormatPlan:
    """Adapt the planner dict to the `FormatPlan` object S4 already consumes."""
    return FormatPlan(
        fmt=str(plan.get("format") or "news"),
        reason=str(plan.get("reason") or ""),
        confidence=float(plan.get("confidence") or 0.0),
        min_chars=int(plan.get("min_chars") or 0),
        max_chars=int(plan.get("max_chars") or 0),
        needs_art=bool(plan.get("needs_art", True)),
        needs_video=bool(plan.get("needs_video", False)),
        outline=list(plan.get("outline") or []),
        signals=dict(plan.get("signals") or {}),
    )


def describe(plan: FormatPlan) -> str:
    return f"{plan.fmt} ({FORMAT_LABEL.get(plan.fmt, plan.fmt)}) — {plan.reason}"


__all__ = [
    "FORMAT_LABEL",
    "LENGTH_BUDGET",
    "MIN_HISTORY_SAMPLE",
    "Format",
    "FormatPlan",
    "as_format_plan",
    "available_text",
    "describe",
    "distinct_outlets",
    "fit_persona",
    "number_density",
    "plan_for_cluster",
    "plan_format",
    "plan_with_history",
]