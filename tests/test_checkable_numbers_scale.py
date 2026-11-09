"""The numeric audit must stay linear in the text length.

`_checkable_numbers` scans every number in a draft and used to locate its line
with `str.rfind`/`str.find` per match, i.e. two full-text passes inside a loop.
On text without line breaks — which is what an LLM rewrite of a news item is —
that made the guardrail and the numeric-claim audit quadratic: 0.09 s at 80 KB,
0.34 s at 160 KB, growing ~4x per doubling. The measurement is the assertion
here, because the defect was invisible in output: every number was found
correctly, only the time grew.
"""

from __future__ import annotations

import time

from tgfarm.s4_generation import _checkable_numbers


def _one_line(length: int) -> str:
    return (("ставка 12.5% и рост 3,4 млрд " * 40) * (length // 900 + 1))[:length]


def test_number_scanning_does_not_grow_quadratically():
    small, large = 40_000, 160_000

    started = time.perf_counter()
    small_values = _checkable_numbers(_one_line(small))
    small_seconds = time.perf_counter() - started

    started = time.perf_counter()
    large_values = _checkable_numbers(_one_line(large))
    large_seconds = time.perf_counter() - started

    assert len(large_values) > len(small_values) > 0
    # Linear would be 4x for a 4x longer text; quadratic was ~13x and climbing.
    assert large_seconds < small_seconds * 8, (
        f"scanning grew {large_seconds / small_seconds:.1f}x for a 4x longer text "
        f"({small_seconds:.3f}s -> {large_seconds:.3f}s)"
    )
    # An absolute ceiling keeps the guard meaningful on a slow machine too.
    assert large_seconds < 0.5, f"{large_seconds:.3f}s for {large} chars"


def test_list_markers_are_still_not_numeric_claims():
    """The optimisation must not change what counts as a number."""
    assert _checkable_numbers("1. Первый пункт\n2) второй\nитог 42%") == ["42"]
