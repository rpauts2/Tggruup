from __future__ import annotations

from types import SimpleNamespace

from tgfarm.config import Persona
from tgfarm.core.schemas import Claim, DraftPost
from tgfarm.s4_generation import _date_audit_claims, _explicit_dates
from tgfarm.s5_quality import gate_facts

PERSONA = Persona(
    id="date-audit-test",
    title="Date audit test",
    niche="tech",
    min_chars=80,
    max_chars=700,
    banned_words=[],
    max_sources=3,
    always_cite_sources=True,
    disclaimer=None,
)


def _item(text: str, url: str = "https://dates.invalid/source") -> SimpleNamespace:
    return SimpleNamespace(title="Source headline", text=text, url=url)


def _draft(body: str, url: str = "https://dates.invalid/source") -> DraftPost:
    return DraftPost(
        hook="Проверяем календарную дату",
        body=body,
        sources=[{"title": "Source headline", "url": url, "source": "test"}],
        claims=[Claim(claim="headline", source_url=url, verified=True)],
        angle="news",
    )


def test_explicit_dates_normalize_numeric_russian_and_english_formats():
    from tgfarm.s4_generation import _explicit_dates

    assert _explicit_dates("2026-10-29") == _explicit_dates("29.10.2026")
    assert _explicit_dates("29 октября 2026 года") == _explicit_dates("29.10.2026")
    assert _explicit_dates("October 29, 2026") == _explicit_dates("29.10.2026")
    assert _explicit_dates("29 October 2026") == _explicit_dates("2026-10-29")


def test_date_audit_accepts_equivalent_date_format_from_source():
    url = "https://dates.invalid/equivalent"
    draft = _draft("Событие произошло 29.10.2026.", url)
    audits = _date_audit_claims(draft, [_item("Событие назначено на 29 октября 2026 года.", url)])

    assert len(audits) == 1
    assert audits[0].verified is True
    assert audits[0].note == "date_audit"
    assert audits[0].source_url == url


def test_date_audit_rejects_calendar_date_missing_from_all_sources():
    url = "https://dates.invalid/missing"
    draft = _draft("Событие произошло 29.10.2026.", url)
    audits = _date_audit_claims(draft, [_item("Источник упоминает только 2026 год.", url)])

    assert len(audits) == 1
    assert audits[0].verified is False
    assert audits[0].note == "date_audit"


def test_date_audit_requires_one_source_to_support_all_dates_in_sentence():
    url1 = "https://dates.invalid/one"
    url2 = "https://dates.invalid/two"
    draft = _draft("События произошли 29.10.2026 и 30.10.2026.", url1)
    audits = _date_audit_claims(
        draft,
        [
            _item("Первое событие произошло 29 октября 2026 года.", url1),
            _item("Второе событие произошло 30 октября 2026 года.", url2),
        ],
    )

    assert len(audits) == 1
    assert audits[0].verified is False


def test_quality_facts_gate_blocks_unverified_date_audit():
    url = "https://dates.invalid/gate"
    draft = _draft("Событие произошло 29.10.2026.", url)
    draft.claims.extend(
        _date_audit_claims(draft, [_item("Источник не содержит полной даты.", url)])
    )

    outcome = gate_facts(draft, PERSONA)

    assert outcome.passed is False
    assert outcome.gate == "facts"
    assert "explicit date claim(s) not grounded" in outcome.reason


def test_date_audit_ignores_text_without_explicit_calendar_date():
    draft = _draft("В течение года продукт получил несколько обновлений.")
    assert _date_audit_claims(draft, [_item("Продукт получил обновления в 2026 году.")]) == []
