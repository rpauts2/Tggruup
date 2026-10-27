"""The offline composer must not mix source languages."""

from __future__ import annotations

from tgfarm.core.schemas import ItemScored
from tgfarm.llm.client import compose_offline_post

PERSONA = {
    "language": "ru",
    "tone": ["factual"],
    "min_chars": 150,
    "max_chars": 700,
    "hashtags": ["Дзен"],
    "cta_policy": "never",
}


def item(source_id: str, url: str, title: str, text: str) -> ItemScored:
    return ItemScored(
        source_id=source_id,
        external_id=url,
        url=url,
        title=title,
        text=text,
    )


ENGLISH = item(
    "techcrunch_ai",
    "https://example.com/en",
    "Just 5 years in, interactive chatbot platform Character.AI files for bankruptcy.",
    "The startup will sell some of its assets to Disney for $20 million. "
    "The company has faced mounting scrutiny and lawsuits over chatbot safety.",
)

RUSSIAN = item(
    "vedomosti_all",
    "https://example.com/ru",
    "Венгерская MOL договорилась о выкупе российской доли в сербской NIS.",
    "Венгерская MOL подписала соглашение о выкупе российской доли в сербской NIS. "
    "Сделку подтвердил исполняющий обязанности вице-премьера Сербии. "
    "Стороны рассчитывают закрыть её до конца года.",
)


def test_offline_composer_uses_russian_source_on_russian_channel():
    payload = compose_offline_post([ENGLISH, RUSSIAN], PERSONA, "news")

    assert "MOL" in payload["hook"]
    assert "bankruptcy" not in payload["body"]
    assert payload["sources"] == [
        {
            "title": RUSSIAN.title[:160],
            "url": RUSSIAN.url,
            "source": RUSSIAN.source_id,
        }
    ]


def test_offline_composer_refuses_english_only_input_on_russian_channel():
    payload = compose_offline_post([ENGLISH], PERSONA, "news")

    assert payload["hook"] == ""
    assert payload["body"] == ""
    assert payload["sources"] == []


def test_offline_composer_keeps_english_on_english_channel():
    persona = {**PERSONA, "language": "en"}
    payload = compose_offline_post([ENGLISH], persona, "news")

    assert "Character.AI" in payload["hook"]
