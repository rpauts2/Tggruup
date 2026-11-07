"""S10 format planner and S11 video licensing gate."""

from __future__ import annotations

import pytest

from tgfarm.config import Persona
from tgfarm.core.schemas import ItemScored
from tgfarm.s10_format import (
    LENGTH_BUDGET,
    available_text,
    describe,
    distinct_outlets,
    fit_persona,
    number_density,
    plan_format,
)
from tgfarm.s11_video import (
    MediaCandidate,
    detect_licence_marker,
    discover_from_feed,
    discover_in_html,
    evaluate,
    fits_telegram,
    guard_summary,
)

RU = Persona(id="p", title="T", niche="business", min_chars=150, max_chars=700)
LONG = Persona(id="p2", title="T", niche="science", min_chars=300, max_chars=2500)
TIGHT = Persona(id="p3", title="T", niche="tech", min_chars=150, max_chars=600)


def item(title: str, text: str = "", source: str = "a", niche: str = "business") -> ItemScored:
    return ItemScored(
        source_id=source,
        external_id=source + title[:12],
        url=f"https://example.invalid/{source}/{abs(hash(title))}",
        title=title,
        text=text,
        niche_hint=[niche],
        source_weight=0.7,
    )


def items(n: int, text_len: int = 300, **kw) -> list[ItemScored]:
    return [item(f"Заголовок новости номер {i}", "текст " * (text_len // 6), source=f"s{i}", **kw)
            for i in range(n)]


# ------------------------------------------------------------------ helpers
def test_number_density():
    assert number_density("Выросло на 40 процентов за год") > 0
    assert number_density("Никаких чисел тут нет") == 0.0
    assert number_density("") == 0.0


def test_distinct_outlets_and_text_volume():
    cluster = items(3)
    assert distinct_outlets(cluster) == 3
    assert available_text(cluster) > 0


# ------------------------------------------------------------ format choice
def test_flash_for_single_source_short_text():
    plan = plan_format(items(1, 200), RU, cluster_heat=0.8)
    assert plan.fmt == "flash"
    assert plan.max_chars <= LENGTH_BUDGET["flash"][1]


def test_stat_for_number_heavy_story():
    blob = "Инфляция ускорилась до 8,4 процента. Ставка 19 процентов. ВВП 2,1 процента."
    plan = plan_format([item("ЦБ отчитался", blob + " текст " * 20)], RU)
    assert plan.fmt == "stat"


def test_article_for_rich_multi_outlet_material():
    plan = plan_format(items(3, 2000), RU)
    assert plan.fmt == "article"
    assert plan.min_chars >= LENGTH_BUDGET["article"][0]


def test_analysis_for_several_outlets():
    plan = plan_format(items(3, 900), RU)
    assert plan.fmt == "analysis"


def test_news_for_single_source_with_text():
    plan = plan_format(items(1, 1200), RU)
    assert plan.fmt == "news"


def test_video_wins_over_everything():
    plan = plan_format(items(3, 2000), RU, has_video=True)
    assert plan.fmt == "video"
    assert plan.needs_video


def test_empty_cluster_is_safe():
    plan = plan_format([], RU)
    assert plan.fmt == "news"
    assert plan.confidence == 0.0


def test_fit_persona_demotes_article_for_tight_persona():
    plan = plan_format(items(3, 2000), TIGHT)
    assert plan.fmt == "article"
    demoted = fit_persona(plan, TIGHT)
    assert demoted.fmt == "news"
    assert demoted.max_chars <= 600
    assert "понижено" in demoted.reason


def test_fit_persona_respects_floor_and_ceiling():
    plan = plan_format(items(1, 300), LONG)
    plan = fit_persona(plan, LONG)
    assert plan.min_chars >= LONG.min_chars
    assert plan.max_chars <= LONG.max_chars


def test_plan_is_deterministic():
    cluster = items(3, 900)
    assert plan_format(cluster, RU).fmt == plan_format(cluster, RU).fmt


def test_describe_is_human_readable():
    plan = plan_format(items(2, 900), RU)
    assert "издания" in describe(plan) or plan.fmt in describe(plan)


# ------------------------------------------------------------- video guard
def test_youtube_links_are_always_refused():
    for url in (
        "https://www.youtube.com/watch?v=abc",
        "https://youtu.be/abc",
        "https://vimeo.com/12345",
        "https://www.tiktok.com/@user/video/1",
        "https://rutube.ru/video/abc",
    ):
        candidate = MediaCandidate(url=url, kind="video")
        evaluate(candidate, "https://site.invalid/article", "")
        assert not candidate.allowed, url
        assert candidate.refusal


def test_same_domain_media_is_allowed():
    candidate = MediaCandidate(url="https://site.invalid/media/clip.mp4", kind="video")
    evaluate(candidate, "https://site.invalid/article", "")
    assert candidate.allowed


def test_cc_licence_allows_cross_domain():
    candidate = MediaCandidate(url="https://cdn.other.invalid/clip.mp4", kind="video")
    evaluate(candidate, "https://site.invalid/article", "creative commons")
    assert candidate.allowed


def test_cross_domain_without_licence_is_refused():
    candidate = MediaCandidate(url="https://cdn.other.invalid/clip.mp4", kind="video")
    evaluate(candidate, "https://site.invalid/article", "")
    assert not candidate.allowed
    assert "лицензии" in candidate.refusal


def test_subdomains_match_the_registrable_domain():
    for media in ("https://cdn.site.invalid/clip.mp4", "https://files.site.invalid/v.mp4"):
        candidate = MediaCandidate(url=media, kind="video")
        evaluate(candidate, "https://www.site.invalid/article", "")
        assert candidate.allowed, media


def test_images_are_classified_separately():
    candidate = MediaCandidate(url="https://site.invalid/a.jpg", kind="image")
    evaluate(candidate, "https://site.invalid/article", "")
    assert candidate.kind == "image"


def test_unsupported_extension_refused():
    candidate = MediaCandidate(url="https://site.invalid/page.html", kind="video")
    evaluate(candidate, "https://site.invalid/article", "creative commons")
    assert not candidate.allowed
    assert candidate.refusal == "неподдерживаемый формат"


def test_licence_marker_detected_in_html():
    html = "<html><body>Материал доступен по лицензии Creative Commons BY 4.0</body></html>"
    assert detect_licence_marker(html)
    assert detect_licence_marker("<html><body>обычная статья</body></html>") == ""


def test_discover_from_feed_reads_enclosures_and_media_content():
    entry = {
        "enclosures": [{"type": "video/mp4", "href": "https://site.invalid/clip.mp4"}],
        "content": [{"value": '<video src="https://site.invalid/inline.webm"></video>'}],
        "links": [{"type": "image/jpeg", "href": "https://site.invalid/p.jpg"}],
    }
    found = {c.url for c in discover_from_feed(entry)}
    assert "https://site.invalid/clip.mp4" in found
    assert "https://site.invalid/inline.webm" in found
    assert "https://site.invalid/p.jpg" in found


def test_discover_in_html_reads_og_tags():
    html = '<meta property="og:video" content="https://site.invalid/v.mp4">'
    found = discover_in_html(html)
    assert any(c.kind == "video" for c in found)


def test_telegram_upload_limit():
    assert fits_telegram(MediaCandidate(url="u", bytes=10 * 1024 * 1024))
    assert not fits_telegram(MediaCandidate(url="u", bytes=60 * 1024 * 1024))
    assert not fits_telegram(MediaCandidate(url="u", bytes=0))


def test_guard_summary_is_operator_facing():
    summary = guard_summary()
    assert "youtube.com" in summary["refused_hosts"]
    assert summary["limits"]["telegram_bot_api_max_mb"] == 50


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=x",
        "https://vk.com/video-1_2",
        "https://dzen.ru/video/abc",
    ],
)
def test_known_platforms_are_in_the_denylist(url):
    from urllib.parse import urlparse

    from tgfarm.s11_video import REFUSED_HOSTS

    host = urlparse(url).netloc
    assert host in REFUSED_HOSTS


def test_lookalike_label_is_not_the_same_domain():
    from tgfarm.s11_video import _same_domain

    assert not _same_domain("evil-samedomain.com", "samedomain.com")
    assert not _same_domain("samedomain.com.evil.net", "samedomain.com")
    assert not _same_domain("notexample.com", "example.com")


def test_unrelated_hosts_under_a_public_suffix_are_not_the_same_domain():
    from tgfarm.s11_video import _same_domain

    assert not _same_domain("evil.co.uk", "good.co.uk")
    assert not _same_domain("evil.com.au", "good.com.au")
    assert _same_domain("co.uk", "co.uk")


def test_subdomains_of_one_owner_under_a_public_suffix_still_match():
    from tgfarm.s11_video import _same_domain

    assert _same_domain("cdn.example.co.uk", "www.example.co.uk")
    assert _same_domain("example.co.uk", "cdn.example.co.uk")
    assert _same_domain("cdn.example.com.au", "example.com.au")


def test_sibling_subdomains_and_case_normalisation():
    from tgfarm.s11_video import _same_domain

    assert _same_domain("cdn.site.invalid", "www.site.invalid")
    assert _same_domain("CDN.Example.COM.", "example.com")
    assert not _same_domain("", "example.com")
    assert _same_domain("localhost", "localhost")
