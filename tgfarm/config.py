"""Loads config/*.yaml once and exposes typed accessors.

Everything the operator can tune lives in YAML, not in code, so re-tuning the
farm never means a redeploy of business logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from tgfarm.core.logging import get_logger
from tgfarm.core.settings import config_dir as _config_dir

log = get_logger(__name__)


def _read_yaml(name: str) -> dict:
    path: Path = _config_dir() / name
    if not path.exists():
        log.warning("config file missing: %s", path)
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


@dataclass(frozen=True)
class Persona:
    id: str
    title: str
    niche: str
    platform: str = "telegram"
    language: str = "ru"
    timezone: str = "Europe/Moscow"
    posts_per_day: int = 3
    enabled: bool = True
    min_chars: int = 150
    max_chars: int = 600
    emoji_policy: str = "max_1"
    cta_policy: str = "never"
    always_cite_sources: bool = True
    max_sources: int = 3
    audience: str = ""
    tone: list[str] = field(default_factory=list)
    style_notes: list[str] = field(default_factory=list)
    banned_words: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    palette: dict[str, str] = field(default_factory=dict)
    disclaimer: str | None = None
    cta: str | None = None
    city: str | None = None
    angle_weights: dict[str, float] = field(default_factory=dict)
    # Optional explicit link to the Telegram channel. Without it the sync matches
    # by title, which is a guess; with it the mapping is unambiguous.
    tg_username: str | None = None
    # A catch-all persona receives every cluster, regardless of niche. Used by
    # the Dzen channel, which is deliberately broader than the Telegram fleet.
    catch_all: bool = False

    @classmethod
    def from_dict(cls, raw: dict) -> Persona:
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001
        data = {k: v for k, v in raw.items() if k in known}
        data.pop("defaults", None)
        data["platform"] = str(data.get("platform") or "telegram").lower()
        return cls(**data)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "niche": self.niche,
            "platform": self.platform,
            "language": self.language,
            "timezone": self.timezone,
            "posts_per_day": self.posts_per_day,
            "enabled": self.enabled,
            "min_chars": self.min_chars,
            "max_chars": self.max_chars,
            "emoji_policy": self.emoji_policy,
            "cta_policy": self.cta_policy,
            "always_cite_sources": self.always_cite_sources,
            "max_sources": self.max_sources,
            "audience": self.audience,
            "tone": self.tone,
            "style_notes": self.style_notes,
            "banned_words": self.banned_words,
            "hashtags": self.hashtags,
            "palette": self.palette,
            "disclaimer": self.disclaimer,
            "cta": self.cta,
            "city": self.city,
            "angle_weights": self.angle_weights,
            "tg_username": self.tg_username,
            "catch_all": self.catch_all,
        }

    def sample_angle(self, rng) -> str:
        weights = self.angle_weights or {"news": 1.0}
        options = list(weights.keys())
        probs = [max(0.0, float(weights[k])) for k in options]
        total = sum(probs)
        if total <= 0:
            return options[0]
        return rng.choices(options, weights=probs, k=1)[0]


@dataclass(frozen=True)
class SourceSpec:
    id: str
    name: str = ""
    kind: str = "rss"
    url: str = ""
    niches: list[str] = field(default_factory=list)
    weight: float = 0.5
    lang: str = "ru"
    enabled: bool = True
    min_interval_s: int = 600
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict) -> SourceSpec:
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001
        return cls(**{k: v for k, v in raw.items() if k in known})

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "url": self.url,
            "niches": self.niches,
            "weight": self.weight,
            "lang": self.lang,
            "enabled": self.enabled,
            "min_interval_s": self.min_interval_s,
        }


class Schedule:
    def __init__(self, raw: dict) -> None:
        self.timezone: str = raw.get("timezone", "Europe/Moscow")
        self.hour_weights: dict[int, float] = {
            int(k): float(v) for k, v in (raw.get("hour_weights") or {}).items()
        }
        self.day_weights: dict[str, float] = {
            k.lower(): float(v) for k, v in (raw.get("day_weights") or {}).items()
        }
        jitter = raw.get("jitter") or {}
        self.jitter_min = int(jitter.get("min", 8))
        self.jitter_max = int(jitter.get("max", 40))
        self.fleet_posts_per_hour = int(raw.get("fleet_posts_per_hour", 6))
        self.cadence = raw.get("cadence") or {}
        self.crosspost = raw.get("crosspost") or {}

    def hours(self) -> list[int]:
        return sorted(self.hour_weights)

    def peak_hours(self, top: int = 3) -> list[int]:
        """The hours carrying the most weight — used for assertions and reports."""
        return [h for h, _ in sorted(self.hour_weights.items(), key=lambda kv: -kv[1])[:top]]

    def hour_probability(self, hour: int) -> float:
        return self.hour_weights.get(hour, 0.0)

    def day_probability(self, weekday: int) -> float:
        names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        if 0 <= weekday < len(names):
            return self.day_weights.get(names[weekday], 0.14)
        return 0.14


@dataclass(frozen=True)
class FarmConfig:
    personas: list[Persona]
    sources: list[SourceSpec]
    schedule: Schedule

    def persona(self, channel_id: str) -> Persona | None:
        for p in self.personas:
            if p.id == channel_id:
                return p
        return None

    def by_niche(self, niche: str, *, platform: str | None = None) -> list[Persona]:
        return [
            p
            for p in self.personas
            if p.niche == niche
            and p.enabled
            and (platform is None or p.platform == platform)
        ]

    def by_platform(self, platform: str) -> list[Persona]:
        return [p for p in self.personas if p.platform == platform and p.enabled]

    @property
    def niches(self) -> set[str]:
        return {p.niche for p in self.personas}

    @property
    def enabled_personas(self) -> list[Persona]:
        return [p for p in self.personas if p.enabled]

    @property
    def catch_all_personas(self) -> list[Persona]:
        return [p for p in self.personas if p.enabled and p.catch_all]


def _source_key(spec: SourceSpec) -> tuple:
    """Identity of a collector: kind, URL and its query parameters.

    The registry grew by copy-paste: the same Google News query is listed several
    times under different ids. Each copy is fetched and stored as a separate
    source, so one story enters a cluster many times and inflates `heat`.
    """
    params = tuple(sorted((str(k), str(v)) for k, v in (spec.params or {}).items()))
    return (spec.kind, spec.url, params)


def _dedupe_sources(sources: list[SourceSpec]) -> list[SourceSpec]:
    """Keep the first definition of each collector, merging its niches."""
    merged: dict[tuple, SourceSpec] = {}
    for spec in sources:
        key = _source_key(spec)
        existing = merged.get(key)
        if existing is None:
            merged[key] = spec
            continue
        niches = list(dict.fromkeys([*(existing.niches or []), *(spec.niches or [])]))
        if niches != existing.niches:
            import dataclasses

            merged[key] = dataclasses.replace(existing, niches=niches)
    if len(merged) != len(sources):
        log.info(
            "sources: %d duplicate definitions collapsed (%d -> %d)",
            len(sources) - len(merged),
            len(sources),
            len(merged),
        )
    return list(merged.values())


@lru_cache(maxsize=1)
def get_config() -> FarmConfig:
    personas_raw = _read_yaml("personas.yaml")
    sources_raw = _read_yaml("sources.yaml")
    slots_raw = _read_yaml("slots.yaml")

    personas = [Persona.from_dict(p) for p in (personas_raw.get("channels") or [])]
    sources = _dedupe_sources(
        [SourceSpec.from_dict(s) for s in (sources_raw.get("sources") or [])]
    )
    schedule = Schedule(slots_raw)

    if not personas:
        log.warning("no personas configured — farm has no channels")
    cfg = FarmConfig(personas=personas, sources=sources, schedule=schedule)
    log.info(
        "config loaded: %d channels, %d sources, %d slot hours",
        len(personas),
        len(sources),
        len(schedule.hours()),
    )
    return cfg


def reload_config() -> FarmConfig:
    get_config.cache_clear()
    return get_config()