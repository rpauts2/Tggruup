"""W2-F: format choice driven by measured history and platform capability."""

from __future__ import annotations

import pytest

from tgfarm.config import Persona
from tgfarm.core.schemas import Cluster, ItemScored
from tgfarm.s10_format import (
    LENGTH_BUDGET,
    MIN_HISTORY_SAMPLE,
    FormatPlan,
    as_format_plan,
    fit_persona,
    plan_for_cluster,
    plan_format,
    plan_with_history,
)

RU = Persona(id="ru", title="Канал", niche="business", min_chars=150, max_chars=700)
TIGHT = Persona(id="tight", title="Канал", niche="tech", min_chars=150, max_chars=600)
WIDE = Persona(id="wide", title="Канал", niche="science", min_chars=150, max_chars=2500)

TOPIC = "ЦБ сохранил ставку на уровне 19%"

#: `reels` is not a format this module can size; `analysis` has too few posts.
HISTORY = {
    "news": {"published": 12, "avg_engagement": 0.031, "n": 12},
    "flash": {"published": 6, "avg_engagement": 0.068, "n": 6},
    "analysis": {"published": 2, "avg_engagement": 0.204, "n": 2},
    "reels": {"published": 8, "avg_engagement": 0.9, "n": 8},
}


def item(
    title: str = "Заголовок новости",
    text: str = "",
    source: str = "a",
    media_kind: str | None = None,
) -> ItemScored:
    return ItemScored(
        source_id=source,
        external_id=f"{source}-{abs(hash(title))}",
        url=f"https://example.invalid/{source}/{abs(hash(title))}",
        title=title,
        text=text,
        media_kind=media_kind,
    )


def item_list(n: int, text_len: int = 300) -> list[ItemScored]:
    return [item(f"Заголовок новости номер {i}", "текст " * (text_len // 6), source=f"s{i}")
            for i in range(n)]


def plan(**overrides) -> dict:
    kwargs = {"platform_id": "telegram", "media_kinds": ["image"], "topic": TOPIC}
    kwargs.update(overrides)
    return plan_with_history(RU, **kwargs)


# ------------------------------------------------ (а) no history is honest
def test_no_history_keeps_the_base_plan_and_says_not_enough_data():
    result = plan()
    base = fit_persona(plan_format([], RU), RU)
    assert result["source"] == "base"
    assert result["format"] == base.fmt
    assert result["reason"].startswith(base.reason)
    assert "мало данных" in result["reason"]
    assert result["manual"] is False
    assert result["capability"] == "publish"
    assert 0.0 <= result["confidence"] <= 1.0


def test_empty_history_mapping_behaves_like_no_history():
    assert plan(history={})["reason"] == plan()["reason"]


# ------------------------------------- (б) a real sample can move the format
def test_history_with_enough_sample_picks_the_best_format():
    result = plan(history=HISTORY)
    assert result["format"] == "flash"
    assert result["source"] == "history"
    assert "история" in result["reason"]
    assert "0.07" in result["reason"]
    assert result["confidence"] >= 0.65
    # the sample that won: n=6, not the 2-post `analysis` row
    assert "6 публикациях" in result["reason"]


def test_published_is_accepted_when_n_is_absent():
    history = {"flash": {"published": 5, "avg_engagement": 0.09}}
    result = plan(history=history)
    assert result["format"] == "flash"
    assert result["source"] == "history"


def test_history_is_deterministic():
    assert plan(history=HISTORY)["format"] == plan(history=HISTORY)["format"]


def test_enough_history_can_confirm_the_base_choice():
    history = {"news": {"published": 7, "avg_engagement": 0.05, "n": 7}}
    result = plan(history=history)
    assert result["format"] == "news"
    assert result["source"] == "history"
    assert "подтверждает" in result["reason"]


# ------------------------------------------------ (в) thin sample changes nothing
def test_small_sample_does_not_override_the_base_format():
    history = {"article": {"published": 2, "avg_engagement": 0.4, "n": 2}}
    result = plan(history=history)
    assert result["format"] == "news"
    assert result["source"] == "base"
    assert "мало данных" in result["reason"]
    # The echo now separates measured from published: a row that only carries a
    # raw count reports `measured: None` rather than implying it was measured.
    assert result["history"]["article"] == {
        "n": 2,
        "measured": None,
        "published": 2,
        "avg_engagement": 0.4,
    }


def test_video_history_is_ignored_while_no_footage_is_attached():
    history = {"video": {"published": 9, "avg_engagement": 0.9, "n": 9}}
    result = plan(history=history)
    assert result["format"] != "video"
    assert "мало данных" in result["reason"]


# ------------------------- (г) capability below publish means a human finishes
def test_manual_upload_platform_is_flagged_and_keeps_the_length_window():
    auto = plan()
    manual = plan(platform_id="vk_clips")
    assert auto["capability"] == "publish"
    assert auto["manual"] is False
    assert manual["capability"] == "manual_upload"
    assert manual["manual"] is True
    assert "пакет" in manual["manual_reason"]
    assert manual["reason"].endswith(manual["manual_reason"])
    assert (manual["min_chars"], manual["max_chars"]) == (auto["min_chars"], auto["max_chars"])


def test_draft_only_platform_already_announces_the_draft():
    result = plan(platform_id="vc_ru")
    assert result["capability"] == "draft_only"
    assert result["manual"] is True
    assert "черновик" in result["manual_reason"]


# -------------------------------- (д) the length window stays valid and sized
@pytest.mark.parametrize("persona", [RU, TIGHT, WIDE])
@pytest.mark.parametrize("platform_id", ["telegram", "vk_clips"])
def test_length_window_stays_valid_and_matches_the_format(persona, platform_id):
    result = plan_with_history(
        persona, platform_id=platform_id, media_kinds=["image"], topic=TOPIC
    )
    low, high = LENGTH_BUDGET[result["format"]]
    assert result["min_chars"] > 0
    assert result["max_chars"] >= result["min_chars"]
    assert low <= result["min_chars"] <= high
    assert low <= result["max_chars"] <= high


def test_history_cannot_push_a_format_past_the_persona_cap():
    history = {"article": {"published": 10, "avg_engagement": 0.5, "n": 10}}
    result = plan(history=history)
    assert result["format"] != "article"
    assert result["max_chars"] <= RU.max_chars
    assert result["source"] == "history"
    assert "понижено" in result["reason"]


# -------------------------------------------------- reuse of the base planner
def test_cluster_base_plan_is_taken_from_the_existing_planner():
    cluster = Cluster(id="cl-1", label="Ставка", items=item_list(3, 900), heat=0.1)
    result = plan_with_history(
        RU, platform_id="telegram", media_kinds=[], topic=TOPIC, cluster=cluster
    )
    base = plan_for_cluster(cluster, RU)
    assert result["format"] == base.fmt
    assert (result["min_chars"], result["max_chars"]) == (base.min_chars, base.max_chars)


def test_cluster_video_field_feeds_the_base_video_rule():
    cluster = Cluster(
        id="cl-2",
        label="Видео",
        items=[item("Заголовок с видео", "текст " * 40, source="s1", media_kind="video")],
        heat=0.1,
    )
    result = plan_with_history(
        RU, platform_id="telegram", media_kinds=[], topic=TOPIC, cluster=cluster
    )
    assert result["format"] == "video"
    assert result["needs_video"] is True


# ------------------------------------------- S4 compatibility of the returned plan
def test_as_format_plan_matches_the_object_s4_consumes():
    result = plan(history=HISTORY)
    adapted = as_format_plan(result)
    assert isinstance(adapted, FormatPlan)
    assert adapted.fmt == result["format"]
    assert (adapted.min_chars, adapted.max_chars) == (result["min_chars"], result["max_chars"])
    assert adapted.reason == result["reason"]


def test_min_history_sample_is_the_documented_three():
    assert MIN_HISTORY_SAMPLE == 3
