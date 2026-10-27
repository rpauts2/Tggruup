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
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Literal

from tgfarm.config import Persona
from tgfarm.core.logging import get_logger
from tgfarm.core.schemas import Cluster, ItemScored

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


def describe(plan: FormatPlan) -> str:
    return f"{plan.fmt} ({FORMAT_LABEL.get(plan.fmt, plan.fmt)}) — {plan.reason}"


__all__ = [
    "FORMAT_LABEL",
    "LENGTH_BUDGET",
    "Format",
    "FormatPlan",
    "available_text",
    "describe",
    "distinct_outlets",
    "fit_persona",
    "number_density",
    "plan_for_cluster",
    "plan_format",
]