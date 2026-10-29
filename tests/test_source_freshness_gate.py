from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from tgfarm.config import Persona
from tgfarm.core.schemas import Claim, DraftPost
from tgfarm.db.models import RawItem
from tgfarm.db.session import session_scope
from tgfarm.s5_quality import gate_source_freshness

PERSONA = Persona(
    id="freshness-test",
    title="Freshness test",
    niche="tech",
    min_chars=100,
    max_chars=700,
    banned_words=[],
    max_sources=3,
    always_cite_sources=True,
    disclaimer=None,
)


def _source(url: str, published_at: datetime | None) -> None:
    suffix = uuid4().hex[:12]
    with session_scope() as session:
        session.add(
            RawItem(
                source_id=f"freshness_{suffix}",
                external_id=f"item_{suffix}",
                url=url,
                title="Test source",
                text="Test source content",
                published_at=published_at,
            )
        )


def _draft(urls: list[str], *, angle: str = "news") -> DraftPost:
    sources = [{"title": f"Source {i}", "url": url, "source": "test"} for i, url in enumerate(urls)]
    return DraftPost(
        hook="Технологическая компания обновила продукт",
        body=(
            "Изменения затрагивают несколько функций сервиса. "
            "Пользователям нужно проверить новые условия работы. "
            "Подробности опубликованы в первоисточниках."
        ),
        sources=sources,
        claims=[
            Claim(claim="продукт обновлён", source_url=urls[0], verified=True),
        ],
        angle=angle,
    )


def test_news_freshness_rejects_only_when_all_known_sources_are_stale(monkeypatch):
    import tgfarm.s5_quality as quality

    monkeypatch.setattr(
        quality,
        "get_settings",
        lambda: SimpleNamespace(source_max_age_hours=72),
    )
    old = datetime.now(UTC) - timedelta(days=10)
    urls = [f"https://freshness.invalid/{uuid4().hex}", f"https://freshness.invalid/{uuid4().hex}"]
    for url in urls:
        _source(url, old)

    outcome = gate_source_freshness(_draft(urls), PERSONA)

    assert outcome.passed is False
    assert outcome.gate == "source_freshness"
    assert outcome.scores == {"source_freshness": 0.0}
    assert "older than 72 hours" in outcome.reason


def test_news_freshness_passes_when_at_least_one_source_is_current(monkeypatch):
    import tgfarm.s5_quality as quality

    monkeypatch.setattr(
        quality,
        "get_settings",
        lambda: SimpleNamespace(source_max_age_hours=72),
    )
    fresh_url = f"https://freshness.invalid/{uuid4().hex}"
    old_url = f"https://freshness.invalid/{uuid4().hex}"
    _source(fresh_url, datetime.now(UTC) - timedelta(hours=6))
    _source(old_url, datetime.now(UTC) - timedelta(days=10))

    outcome = gate_source_freshness(_draft([fresh_url, old_url]), PERSONA)

    assert outcome.passed is True
    assert outcome.scores == {"source_freshness": 1.0}


def test_news_freshness_does_not_guess_when_publication_date_is_unknown(monkeypatch):
    import tgfarm.s5_quality as quality

    monkeypatch.setattr(
        quality,
        "get_settings",
        lambda: SimpleNamespace(source_max_age_hours=72),
    )
    url = f"https://freshness.invalid/{uuid4().hex}"
    _source(url, None)

    outcome = gate_source_freshness(_draft([url]), PERSONA)

    assert outcome.passed is True
    assert outcome.scores is None


def test_non_news_angles_can_use_older_sources(monkeypatch):
    import tgfarm.s5_quality as quality

    monkeypatch.setattr(
        quality,
        "get_settings",
        lambda: SimpleNamespace(source_max_age_hours=72),
    )
    url = f"https://freshness.invalid/{uuid4().hex}"
    _source(url, datetime.now(UTC) - timedelta(days=90))

    outcome = gate_source_freshness(_draft([url], angle="analysis"), PERSONA)

    assert outcome.passed is True
    assert outcome.scores is None


def test_zero_freshness_threshold_disables_the_gate(monkeypatch):
    import tgfarm.s5_quality as quality

    monkeypatch.setattr(
        quality,
        "get_settings",
        lambda: SimpleNamespace(source_max_age_hours=0),
    )
    url = f"https://freshness.invalid/{uuid4().hex}"
    _source(url, datetime.now(UTC) - timedelta(days=90))

    outcome = gate_source_freshness(_draft([url]), PERSONA)

    assert outcome.passed is True
