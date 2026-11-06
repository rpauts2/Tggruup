"""The config cache must not survive a test-file boundary, and must survive inside one.

`tgfarm.config.get_config` is `@lru_cache(maxsize=1)`: the first caller in a pytest
run decides what every later file reads, unless something clears it. `_reset_caches()`
in `tests/conftest.py` resets settings, engine, LLM and embedder — and did not reset
this one. That is not hypothetical: a whole-suite run on 2026-11-06 lost eight
`first_slot` cells of `tests/test_publishing_state_machine.py` because a file that had
run earlier had cached a cadence the matrix never asked for. The fix lives in
`tests/conftest.py::pytest_runtest_setup`; these tests pin both halves of it, because
a hook that clears on every call is as wrong as one that never clears — it would make
the cache useless inside a file and hide the ordering bug it exists to prevent.

Both directions are asserted through the hook itself, not through suite order, so this
file is deterministic on its own and in any position.
"""

from __future__ import annotations

from pathlib import Path

from tgfarm.config import get_config


class _Item:
    """The one attribute the hook reads: the path of the test being set up."""

    def __init__(self, path: str) -> None:
        self.path = path


def test_a_new_file_boundary_forgets_the_previous_files_config() -> None:
    """Warm the cache, cross a file boundary, and the cache must be empty."""
    first = str(Path(__file__).with_name("some_other_file.py"))
    second = str(Path(__file__).with_name("this_file.py"))

    get_config()
    assert get_config.cache_info().currsize == 1, "the cache was not warmed at all"

    _boundary(first)
    _boundary(second)

    assert get_config.cache_info().currsize == 0, (
        "a config warmed by one test file reached the next one; the cadence a test "
        "runs in is then decided by suite order, not by the test"
    )


def test_within_one_file_the_cache_is_not_thrashed() -> None:
    """Same file, two setups: no clear, so the cache keeps the object it loaded."""
    path = str(Path(__file__))

    _boundary(path)
    info_after_first = get_config.cache_info()
    assert info_after_first.currsize <= 1

    get_config()
    _boundary(path)

    assert get_config.cache_info().currsize == 1, (
        "the boundary hook fired inside a single file; it must clear only when the "
        "file changes, otherwise it is a per-test YAML re-read wearing a hygiene costume"
    )


def test_the_hook_is_the_conftest_one_and_is_registered() -> None:
    """Guard against a hook that exists but is not the one pytest calls."""
    import conftest

    assert callable(conftest.pytest_runtest_setup)
    source = Path(conftest.__file__).read_text(encoding="utf-8")
    assert "get_config.cache_clear()" in source, (
        "`pytest_runtest_setup` no longer clears the config cache; the ordering "
        "defect this file pins is back"
    )


def _boundary(path: str) -> None:
    """Run the real hook for a test living at `path`."""
    from conftest import pytest_runtest_setup

    pytest_runtest_setup(_Item(path))  # type: ignore[arg-type]
