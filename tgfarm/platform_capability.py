"""Honest five-level capability status of every catalogue destination.

The mission's platform contract distinguishes five levels instead of a boolean
"supported" flag::

    unsupported < read_only < draft_only < manual_upload < publish

A destination may only be labelled ``publish`` when the farm itself delivers the
material without a human: a crosspost adapter with a real ``publish()`` call, or
one of the primary publishers wired in the pipeline (Telegram Bot API, VK wall,
Dzen browser editor).  Everything else is reported as what it truly is — a filled
draft, a manual package, a read-only integration, or nothing at all.

The levels are derived from code facts (adapter registry, manual-package
registry), never from documentation, so the panel cannot show a green
"publishing" state for a platform that only produces a package.
"""

from __future__ import annotations

from typing import Iterable, Mapping

LEVELS: tuple[str, ...] = (
    "unsupported",
    "read_only",
    "draft_only",
    "manual_upload",
    "publish",
)

#: Level index for ordering/UX ("at least draft_only").
LEVEL_RANK: dict[str, int] = {name: index for index, name in enumerate(LEVELS)}

CAPABILITY_LABELS: dict[str, str] = {
    "publish": "Публикует автоматически",
    "manual_upload": "Ручная загрузка пакета",
    "draft_only": "Заполняет черновик, публикует человек",
    "read_only": "Только чтение",
    "unsupported": "Не поддерживается",
}

CAPABILITY_NOTES: dict[str, str] = {
    "publish": "Материал доставляется адаптером площадки без участия человека.",
    "manual_upload": "TgFarm готовит текст, заголовок, теги и медиа; загрузку выполняет человек.",
    "draft_only": "TgFarm заполняет редактор площадки в сохранённом профиле, финальную кнопку нажимает человек.",
    "read_only": "TgFarm только читает данные площадки и не отправляет материалы.",
    "unsupported": "Рабочего сценария нет: ни адаптера, ни ручного пакета.",
}

#: Primary publishers that live in the pipeline, not in the crosspost registry.
PRIMARY_PUBLISHERS: frozenset[str] = frozenset({"telegram", "vk", "dzen"})

#: Destinations where the farm fills the site editor but never presses "publish".
DRAFT_ONLY: frozenset[str] = frozenset({"vc_ru"})

#: Destinations with a read-only integration only.  None today: metrics readers
#: (Reddit, Telegraph) belong to destinations that also publish.
READ_ONLY: frozenset[str] = frozenset()


def _adapter_ids() -> frozenset[str]:
    """Platform ids with a registered crosspost adapter (lazy: heavy imports)."""
    try:
        from tgfarm.s9_growth.adapters import ADAPTERS

        return frozenset(str(key).lower() for key in ADAPTERS)
    except Exception:  # noqa: BLE001 - a broken adapter import must not hide the matrix
        return frozenset()


def _manual_ids() -> frozenset[str]:
    try:
        from tgfarm.manual_drafts import MANUAL_DESTINATIONS

        return frozenset(str(key).lower() for key in MANUAL_DESTINATIONS)
    except Exception:  # noqa: BLE001 - same reasoning as above
        return frozenset()


def publish_ids(
    *,
    adapters: Iterable[str] | None = None,
    primary: Iterable[str] = PRIMARY_PUBLISHERS,
) -> frozenset[str]:
    """Every destination the farm can deliver without a human."""
    base = _adapter_ids() if adapters is None else frozenset(str(x).lower() for x in adapters)
    return base | frozenset(str(x).lower() for x in primary)


def capability_of(
    platform_id: str,
    *,
    adapters: Iterable[str] | None = None,
    manual: Iterable[str] | None = None,
    draft_only: Iterable[str] = DRAFT_ONLY,
    read_only: Iterable[str] = READ_ONLY,
) -> str:
    """Return the honest capability level of one destination."""
    pid = str(platform_id or "").strip().lower()
    if not pid:
        return "unsupported"
    if pid in publish_ids(adapters=adapters):
        return "publish"
    draft = {str(x).lower() for x in draft_only}
    if pid in draft:
        return "draft_only"
    manual_set = _manual_ids() if manual is None else {str(x).lower() for x in manual}
    if pid in manual_set:
        return "manual_upload"
    if pid in {str(x).lower() for x in read_only}:
        return "read_only"
    return "unsupported"


def describe(platform_id: str, **kwargs) -> dict[str, str | bool]:
    """Contract row for API/UI: level, label, note and the auto-publish flag."""
    level = capability_of(platform_id, **kwargs)
    return {
        "capability": level,
        "capability_label": CAPABILITY_LABELS[level],
        "capability_note": CAPABILITY_NOTES[level],
        "auto_publish": level == "publish",
        "supports_draft": LEVEL_RANK[level] >= LEVEL_RANK["draft_only"],
        "supports_manual_package": LEVEL_RANK[level] >= LEVEL_RANK["manual_upload"],
    }


def capability_matrix() -> dict[str, dict[str, str | bool]]:
    """Capability row of every destination in the catalogue."""
    from tgfarm.platforms import CATALOG

    return {str(item["id"]): describe(str(item["id"])) for item in CATALOG}


def capability_counts() -> dict[str, int]:
    """How many catalogue destinations sit on each level (dashboard summary)."""
    counts = {level: 0 for level in LEVELS}
    for row in capability_matrix().values():
        counts[str(row["capability"])] += 1
    return counts


def is_at_least(platform_id: str, level: str, **kwargs) -> bool:
    """True when the destination supports at least ``level``."""
    if level not in LEVEL_RANK:
        raise ValueError(f"unknown capability level: {level}")
    return LEVEL_RANK[capability_of(platform_id, **kwargs)] >= LEVEL_RANK[level]


__all__ = [
    "LEVELS",
    "LEVEL_RANK",
    "CAPABILITY_LABELS",
    "CAPABILITY_NOTES",
    "PRIMARY_PUBLISHERS",
    "DRAFT_ONLY",
    "READ_ONLY",
    "capability_of",
    "describe",
    "capability_matrix",
    "capability_counts",
    "is_at_least",
    "publish_ids",
]
