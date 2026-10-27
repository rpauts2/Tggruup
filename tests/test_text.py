"""Text primitives: near-duplicate detection, similarity, language, truncation."""

from __future__ import annotations

from tgfarm.core.text import (
    SIMHASH_MAX_DISTANCE,
    char_ngrams,
    detect_lang,
    first_sentence,
    hamming,
    is_near_duplicate,
    jaccard,
    keyword_set,
    simhash,
    title_key,
    token_jaccard,
    tokens,
    truncate,
)


def test_simhash_matches_near_identical():
    a = simhash("ЦБ поднял ключевую ставку до 19 процентов")
    b = simhash("ЦБ поднял ключевую ставку до 19 процентов.")
    assert is_near_duplicate(a, b)


def test_simhash_separates_unrelated():
    a = simhash("ЦБ поднял ключевую ставку до 19 процентов")
    b = simhash("Ethereum перешёл на второй уровень, комиссия упала")
    assert not is_near_duplicate(a, b)


def test_simhash_threshold_separates_the_two_populations():
    """The threshold must sit between same-story and different-story distances."""
    same = hamming(
        simhash("ЦБ поднял ключевую ставку до 19 процентов"),
        simhash("ЦБ поднял ключевую ставку до 19 процентов."),
    )
    different = hamming(
        simhash("Ethereum перешёл на L2"),
        simhash("Ethereum перешёл на второй уровень"),
    )
    assert same <= SIMHASH_MAX_DISTANCE
    assert different > SIMHASH_MAX_DISTANCE


def test_simhash_fits_signed_64bit():
    """SQLite INTEGER is signed; an unsigned 64-bit fingerprint overflows."""
    fingerprint = simhash("some text " * 50)
    assert 0 <= fingerprint < 2**62


def test_tokens_drop_stopwords_and_short_words():
    assert "the" not in tokens("The quick brown fox")
    assert "бы" not in tokens("бы и то")
    assert "brown" in tokens("The quick brown fox")


def test_detect_lang():
    assert detect_lang("ЦБ поднял ключевую ставку") == "ru"
    assert detect_lang("The central bank raised rates") == "en"


def test_truncate_respects_limit():
    long = "слово " * 200
    out = truncate(long, 50)
    assert len(out) <= 50
    assert out.endswith("…")


def test_truncate_noop_when_short():
    assert truncate("коротко", 50) == "коротко"


def test_first_sentence():
    text = "Первое предложение. Второе предложение."
    assert first_sentence(text, 100).startswith("Первое предложение")


def test_jaccard_and_token_jaccard():
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert jaccard(set(), {"a"}) == 0.0
    assert token_jaccard("ставка выросла", "ставка снизилась") > 0.3


def test_title_key_ignores_word_order():
    assert title_key("ЦБ поднял ставку до 19%") == title_key("до 19% ЦБ поднял ставку")


def test_keyword_set_prefers_long_distinctive_tokens():
    words = keyword_set("регулирование и обучение", limit=5)
    assert words


def test_char_ngrams_handles_short_text():
    assert char_ngrams("ab") != []
    assert char_ngrams("") == []


def test_strip_html_unescapes_entities():
    """Feeds put &#8216; straight into <title>; it must not reach the cover."""
    from tgfarm.core.text import strip_html

    assert strip_html("&#8216;Pure insanity&#8217;: a &amp; a &quot;quote&quot;") == (
        "‘Pure insanity’: a & a \"quote\""
    )


def test_strip_html_removes_tags_and_collapses():
    from tgfarm.core.text import strip_html

    assert strip_html("<p>Hello   <b>world</b></p>\n<p>again</p>") == "Hello world again"