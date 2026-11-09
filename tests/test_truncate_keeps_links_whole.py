"""A cut must not publish half a link.

`truncate` is the single choke point every platform path goes through
(`distribution`, adapters, manual packages). Cutting inside `https://…` ships a
dead target; cutting inside `[label](url)` ships a dangling bracket. Both were
reachable: `truncate("[текст](https://ex.com/a/b/c)", 25)` produced
`[текст](https://e…`.
"""

from __future__ import annotations

import pytest

from tgfarm.core.text import truncate


def test_a_markdown_link_that_straddles_the_cut_keeps_its_label():
    result = truncate("Смотрите [полный отчёт](https://example.com/report/2026) дальше", 30)
    assert result == "Смотрите полный отчёт дальше…"
    assert "](https" not in result


def test_a_bare_url_that_straddles_the_cut_is_not_half_written():
    result = truncate("Читайте подробнее: https://example.com/very/long/path", 40)
    assert "https://example.co…" not in result
    assert "https://example.com/very/long/path" not in result
    assert result.endswith("…")


def test_a_url_that_fits_is_kept_verbatim():
    assert truncate("https://example.com/a", 40) == "https://example.com/a"


def test_zero_limit_returns_nothing_instead_of_an_ellipsis():
    """`max(0, limit - 1)` used to leave an empty cut with an ellipsis glued on."""
    assert truncate("word word", 0) == ""
    assert truncate("word word", -5) == ""


@pytest.mark.parametrize("limit", [1, 2, 3, 5, 10, 20, 40, 100])
def test_the_result_never_exceeds_the_limit(limit):
    text = "Длинный текст [ссылка](https://example.com/x) и ещё слова для обрезки"
    assert len(truncate(text, limit)) <= limit


def test_plain_text_still_cuts_on_a_word_boundary():
    assert truncate("Слово " * 20, 30) == "Слово Слово Слово Слово…"


def test_text_that_fits_is_returned_unchanged():
    assert truncate("Короткий текст", 100) == "Короткий текст"
