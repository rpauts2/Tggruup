"""The quality gates — the layer that decides what actually ships."""

from __future__ import annotations

import pytest

from tgfarm.config import Persona
from tgfarm.core.schemas import Claim, DraftPost
from tgfarm.s5_quality import (
    gate_copyright,
    gate_disclaimer,
    gate_facts,
    gate_language,
    gate_length,
    gate_links,
    gate_redundancy,
    gate_structure,
    gate_vocabulary,
)

BASE_PERSONA = Persona(
    id="test",
    title="Test",
    niche="tech",
    min_chars=100,
    max_chars=700,
    banned_words=["революция", "гарантированно"],
    max_sources=3,
    always_cite_sources=True,
    disclaimer=None,
)

SOURCES = [{"title": "Источник", "url": "https://t.me/example", "source": "test"}]


def draft(**kwargs) -> DraftPost:
    payload = {
        "hook": "Регулятор обновил требования к отчётности компаний.",
        "body": "Изменения вступают в силу с первого квартала следующего года. "
        "Компании должны пересмотреть внутренние процессы и подать новые формы. "
        "Эксперты ожидают дополнительных расходов на переходный период.",
        "sources": SOURCES,
        "claims": [Claim(claim="требования обновлены", source_url="https://t.me/example", verified=True)],
    }
    payload.update(kwargs)
    return DraftPost(**payload)


def test_length_gate_bounds():
    persona = BASE_PERSONA
    assert gate_length(draft(), persona).passed

    short = draft(body="Слишком коротко.")
    outcome = gate_length(short, persona)
    assert not outcome.passed and outcome.gate == "length"


def test_length_gate_ignores_source_block():
    """The length budget is editorial prose; citations are metadata.

    A 600-char budget that included three URLs would leave ~200 characters of
    actual writing, which is why char_len is not what the gate measures.
    """
    persona = BASE_PERSONA
    padded = draft(
        sources=[
            {"title": "Довольно длинный заголовок источника один", "url": "https://t.me/a", "source": "s"},
            {"title": "Довольно длинный заголовок источника два", "url": "https://t.me/b", "source": "s"},
            {"title": "Довольно длинный заголовок источника три", "url": "https://t.me/c", "source": "s"},
        ]
    )
    assert padded.editorial_len == draft().editorial_len
    assert padded.char_len > padded.editorial_len
    assert gate_length(padded, persona).passed


def test_vocabulary_gate_blocks_banned_words():
    outcome = gate_vocabulary(draft(body="Это настоящая революция в отчётности."), BASE_PERSONA)
    assert not outcome.passed and outcome.gate == "vocabulary"


def test_vocabulary_gate_allows_clean_text():
    assert gate_vocabulary(draft(), BASE_PERSONA).passed


def test_language_gate_rejects_english_on_russian_channel():
    outcome = gate_language(
        draft(
            hook="OpenAI shipped a new model.",
            body="The company said the model is faster and cheaper for developers.",
        ),
        BASE_PERSONA,
    )
    assert not outcome.passed and outcome.gate == "language"
    assert "expected ru" in outcome.reason


def test_language_gate_allows_english_on_english_channel():
    persona = Persona(**{**BASE_PERSONA.__dict__, "language": "en"})
    assert gate_language(
        draft(
            hook="OpenAI shipped a new model.",
            body="The company said the model is faster and cheaper for developers.",
        ),
        persona,
    ).passed


def test_language_gate_skips_unenforced_languages():
    persona = Persona(**{**BASE_PERSONA.__dict__, "language": "uz"})
    assert gate_language(draft(body="OpenAI yangi modelni taqdim etdi."), persona).passed


def test_links_gate_requires_sources():
    outcome = gate_links(draft(sources=[]), BASE_PERSONA)
    assert not outcome.passed and outcome.gate == "links"


def test_links_gate_enforces_persona_max_sources():
    """The schema already caps at 3; a tighter persona cap is what the gate adds."""
    tight = Persona(**{**BASE_PERSONA.__dict__, "max_sources": 2})
    three = [{"title": "t", "url": f"https://t.me/{i}", "source": "s"} for i in range(3)]
    assert not gate_links(draft(sources=three), tight).passed
    assert gate_links(draft(sources=three[:2]), tight).passed


def test_draft_post_schema_caps_sources():
    many = [{"title": str(i), "url": f"https://t.me/{i}", "source": "s"} for i in range(9)]
    assert len(draft(sources=many).sources) == 3


def test_facts_gate_requires_verified_ratio():
    claims = [
        Claim(claim="a", source_url="https://t.me/example", verified=True),
        Claim(claim="b", source_url="https://t.me/example", verified=False),
        Claim(claim="c", source_url="https://t.me/example", verified=False),
    ]
    outcome = gate_facts(draft(claims=claims), BASE_PERSONA)
    assert not outcome.passed
    assert "grounded" in outcome.reason


def test_facts_gate_passes_at_the_boundary():
    claims = [
        Claim(claim="a", source_url="https://t.me/example", verified=True),
        Claim(claim="b", source_url="https://t.me/example", verified=True),
        Claim(claim="c", source_url="https://t.me/example", verified=False),
    ]
    assert gate_facts(draft(claims=claims), BASE_PERSONA).passed



def test_facts_gate_rejects_verified_claim_from_unattached_source():
    claim = Claim(
        claim="Требования подтверждены",
        source_url="https://unattached.example/story",
        verified=True,
    )
    outcome = gate_facts(draft(claims=[claim]), BASE_PERSONA)
    assert not outcome.passed
    assert outcome.gate == "facts"
    assert "not attached" in outcome.reason


def test_facts_gate_accepts_same_attached_source_with_fragment_or_trailing_slash():
    claim = Claim(
        claim="Требования подтверждены",
        source_url="https://t.me/example/#headline",
        verified=True,
    )
    outcome = gate_facts(draft(claims=[claim]), BASE_PERSONA)
    assert outcome.passed

def test_links_gate_blocks_blacklisted_domain(db):
    from tgfarm.db.models import BlocklistEntry
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        session.add(BlocklistEntry(scope="domain", value="spam.example", reason="test"))

    outcome = gate_links(
        draft(sources=[{"title": "t", "url": "https://spam.example/a", "source": "s"}]),
        BASE_PERSONA,
    )
    assert not outcome.passed
    assert "blocked domain" in outcome.reason


def test_facts_gate_requires_sources():
    outcome = gate_facts(draft(sources=[], claims=[]), BASE_PERSONA)
    assert not outcome.passed and outcome.gate == "facts"



def test_facts_gate_passes_fully_grounded():
    assert gate_facts(draft(), BASE_PERSONA).passed


def test_disclaimer_gate():
    persona = Persona(**{**BASE_PERSONA.__dict__, "disclaimer": "Не является рекомендацией."})
    body = draft().body + " Не является рекомендацией."
    assert gate_disclaimer(draft(body=body), persona).passed
    assert not gate_disclaimer(draft(), persona).passed


def test_structure_gate_requires_sentence_hook():
    outcome = gate_structure(draft(hook="Просто заголовок без точки"), BASE_PERSONA)
    assert not outcome.passed and outcome.gate == "structure"


def test_structure_gate_limits_emoji():
    outcome = gate_structure(draft(hook="Регулятор обновил требования 🔥🚀 к отчётности."), BASE_PERSONA)
    assert not outcome.passed


def test_structure_gate_rejects_too_short_hook():
    assert not gate_structure(draft(hook="Коротко."), BASE_PERSONA).passed


def test_redundancy_gate_allows_subject_repetition():
    """A news post that names its subject in both hook and body is normal."""
    ok = draft(
        hook="Регулятор обновил требования к отчётности.",
        body="Регулятор обновил требования. Отчётность меняется с первого квартала. "
        "Компании пересматривают процессы.",
    )
    assert gate_redundancy(ok, BASE_PERSONA).passed


def test_redundancy_gate_catches_verbatim_restatement():
    bad = draft(
        hook="Регулятор обновил требования к отчётности компаний сегодня",
        body="Регулятор обновил требования к отчётности компаний сегодня. "
        "Далее следуют подробности.",
    )
    outcome = gate_redundancy(bad, BASE_PERSONA)
    assert not outcome.passed and outcome.gate == "redundancy"


def test_redundancy_gate_allows_shared_subject_phrase():
    """A shared subject phrase is not repetition — only a long verbatim run is."""
    ok = draft(
        hook="ВОЗ обновила рекомендации по физической активности.",
        body="ВОЗ обновила рекомендации по физической активности: 150 минут в неделю "
        "вместо 300. Сидячий времени сокращают до шести часов в сутки.",
    )
    assert gate_redundancy(ok, BASE_PERSONA).passed


def test_redundancy_gate_skips_short_hooks():
    assert gate_redundancy(draft(hook="Коротко.", body="Коротко. " * 30), BASE_PERSONA).passed


def test_copyright_gate_rejects_reproduced_headline():
    """A body that is just the headline, with nothing added, is a scraped repost."""
    title = "регулятор обновил требования к отчётности компаний"
    sources = [{"title": title, "url": "https://t.me/x", "source": "s"}]
    outcome = gate_copyright(
        draft(body=f"{title}.", sources=sources),
        BASE_PERSONA,
    )
    assert not outcome.passed and outcome.gate == "copyright"


def test_copyright_gate_allows_headline_then_development():
    """Restating the headline at the top of a news post is normal writing."""
    title = "регулятор обновил требования к отчётности компаний"
    sources = [{"title": title, "url": "https://t.me/x", "source": "s"}]
    outcome = gate_copyright(
        draft(
            body=(
                f"{title}. Изменения вступают в силу с первого квартала. "
                "Формы обновлены, переходный период продлён на полгода, "
                "штрафы за несвоевременную отчётность смягчены для малого бизнеса."
            ),
            sources=sources,
        ),
        BASE_PERSONA,
    )
    assert outcome.passed


def test_copyright_gate_allows_original_paraphrase():
    outcome = gate_copyright(
        draft(
            body="Требования к отчётности изменились. Компаниям нужно пересмотреть процессы.",
            sources=[{"title": "совсем другой заголовок источника", "url": "https://t.me/x", "source": "s"}],
        ),
        BASE_PERSONA,
    )
    assert outcome.passed




@pytest.mark.parametrize("angle", ["news", "analysis", "why_it_matters", "checklist"])
def test_draft_post_text_assembles(angle):
    d = draft(angle=angle)
    text = d.text
    assert d.hook in text
    assert "https://t.me/example" in text


def test_draft_post_caps_sources_and_hashtags():
    d = draft(
        sources=[{"title": str(i), "url": f"https://t.me/{i}", "source": "s"} for i in range(9)],
        hashtags=["#a", "b", "c", "d", "e", "f", "g"],
    )
    assert len(d.sources) == 3
    assert len(d.hashtags) == 6
    assert all(not h.startswith("#") for h in d.hashtags)


# ------------------------------------------- the text that actually ships (D3-D5)
def test_vocabulary_gate_checks_cta():
    """A banned word in CTA must not ship just because it is not in hook/body."""
    d = draft(cta="Не пропустите революция в отчётности!")
    assert not gate_vocabulary(d, BASE_PERSONA).passed


def test_cliches_gate_checks_cta():
    """A stock phrase in CTA is part of the published text."""
    from tgfarm.s5_quality import gate_cliches

    d = draft(cta="На сегодняшний день это главное событие.")
    assert not gate_cliches(d, BASE_PERSONA).passed


def test_vocabulary_gate_checks_hashtags():
    d = draft(hashtags=["революция"])
    assert not gate_vocabulary(d, BASE_PERSONA).passed


def test_links_gate_checks_cta():
    """A link in the CTA is a link we publish and must be checked."""
    from tgfarm.db.models import BlocklistEntry
    from tgfarm.db.session import session_scope

    with session_scope() as session:
        session.add(BlocklistEntry(scope="domain", value="spam.example", reason="test"))
    d = draft(cta="Подробности: https://spam.example/a")
    assert not gate_links(d, BASE_PERSONA).passed


def test_emoji_budget_applies_to_personas_above_one():
    """A non-max_1 persona still has a nine-emoji cap; the branch used to be dead."""
    persona = Persona(**{**BASE_PERSONA.__dict__, "emoji_policy": "max_9"})
    ten = draft(hook="Регулятор 🔥🚀💥🎯📈📉💰🎉🏆😀 обновил требования к отчётности.")
    nine = draft(hook="Регулятор 🔥🚀💥🎯📈📉💰🎉🏆 обновил требования к отчётности.")
    assert not gate_structure(ten, persona).passed
    assert gate_structure(nine, persona).passed
