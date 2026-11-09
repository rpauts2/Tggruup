"""`simhash` must be linear in the text, and must not change its answer.

The fingerprint runs on every collected item and on every legacy row a reprint
backfill re-checks, so its cost is a per-item tax on the whole pipeline. The
implementation voted bit by bit over *every* character n-gram: 62 Python
iterations per gram, 77k grams in a 77 KB article, 0.94 s for one call. It also
grew linearly with text length but with a 62x constant nobody would defend.

The fix counts each distinct gram once and weights its vote by multiplicity,
which is the same number by construction — a gram that occurs w times casts the
same bit votes w times. So this file pins two things: the speed, and the fact
that the fingerprint of a given text did not move. A faster fingerprint that
answers differently would silently re-partition the duplicate net.
"""

from __future__ import annotations

import hashlib
import random
import time

from tgfarm.core.text import char_ngrams, simhash


def _reference_simhash(text: str, bits: int = 62) -> int:
    """The implementation before the change, kept as the oracle."""
    vector = [0] * bits
    grams = char_ngrams(text, 4)
    if not grams:
        return 0
    for gram in grams:
        h = int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=8).digest(), "big")
        for i in range(bits):
            vector[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if vector[i] > 0:
            out |= 1 << i
    return out


def _prose(length: int) -> str:
    """Many distinct sentences: the shape that made the old loop expensive."""
    rng = random.Random(7)
    words = (
        "компания рынок выручка отчёт квартал аналитик прогноз рост падение "
        "регулятор требования закон поправка сервис платформа пользователь "
        "данные исследование учёный эксперимент результат"
    ).split()
    sentences = []
    while sum(len(s) + 1 for s in sentences) < length:
        sentences.append(
            " ".join(rng.choice(words) for _ in range(rng.randint(6, 14))).capitalize() + "."
        )
    return " ".join(sentences)[:length]


def test_the_fingerprint_of_the_same_text_is_unchanged():
    """Speed must not move the answer: same text, same bits, every shape."""
    rng = random.Random(11)
    words = "компания рынок выручка отчёт квартал аналитик прогноз рост".split()
    cases = [
        "",
        "a",
        "аб",
        "Компания сообщила о росте выручки в третьем квартале",
        "Привет мир. Привет мир.",
        "Одинаковое предложение здесь повторяется. " * 300,
        _prose(5_000),
        _prose(40_000),
    ]
    for _ in range(6):
        cases.append(" ".join(rng.choice(words) for _ in range(rng.randint(5, 400))))

    drifted = [text[:40] for text in cases if simhash(text) != _reference_simhash(text)]
    assert not drifted, f"the fingerprint moved for: {drifted}"


def test_fingerprinting_a_long_article_is_not_a_per_item_tax():
    """90 KB is a long article; the old code spent 0.94 s on it."""
    text = _prose(90_000)
    assert len(text) == 90_000

    started = time.perf_counter()
    simhash(text)
    seconds = time.perf_counter() - started

    assert seconds < 0.2, f"{seconds:.3f}s for 90 KB, which the pipeline pays per item"


def test_fingerprinting_does_not_grow_with_repetition():
    """A repeated body is cheap: distinct grams, not occurrences, cost money.

    The fingerprints are not equal — the boundary grams of a repeated sentence
    differ from those of one copy, and those decide a few bits — so the assertion
    is the cost, not the value.
    """
    sentence = "Компания сообщила о росте выручки в третьем квартале этого года. "
    text = sentence * 2000
    assert len(text) > 120_000

    started = time.perf_counter()
    simhash(text)
    seconds = time.perf_counter() - started

    assert seconds < 0.2, f"{seconds:.3f}s for {len(text)} chars of repetition"
