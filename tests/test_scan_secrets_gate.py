"""The checkout's own secret gate.

`tools/scan_secrets.py` exists because a test run once carried real credentials
into a live VK wall. A gate that is red on a clean checkout is worse than no
gate: every operator learns to ignore its exit code, which is exactly the state
a real leak needs. The planted fixture in `tests/test_w24b_buildinfo.py` was a
credential-shaped literal, so the scanner flagged the repository itself.
"""

from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_tool():
    spec = spec_from_file_location("scan_secrets_under_test", REPO_ROOT / "tools" / "scan_secrets.py")
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_shipped_checkout_has_no_secret_named_where_it_may_not_be() -> None:
    scanner = _load_tool()

    assert scanner.scan(REPO_ROOT) == []


def test_a_planted_literal_is_still_reported(tmp_path: Path) -> None:
    """The gate must stay strict: silencing the fixture must not silence a leak."""
    scanner = _load_tool()
    (tmp_path / "leak.py").write_text('KEY = "sk-' + 'live-' + 'qwertyuiopasdfghjkl"\n', encoding="utf-8")

    hits = scanner.scan(tmp_path)

    assert len(hits) == 1
    assert hits[0].startswith("leak.py:1:")


def test_a_credential_assembled_from_parts_is_not_a_literal(tmp_path: Path) -> None:
    """How the fixture keeps its shape without writing a key into the tree."""
    scanner = _load_tool()
    (tmp_path / "fixture.py").write_text('KEY = "sk-" + "live-" + "qwertyuiopasdfghjkl"\n', encoding="utf-8")

    assert scanner.scan(tmp_path) == []


def test_skipped_directories_are_not_read(tmp_path: Path) -> None:
    scanner = _load_tool()
    cache = tmp_path / ".venv"
    cache.mkdir()
    (cache / "odd.py").write_text(
        'KEY = "sk-' + 'live-' + 'qwertyuiopasdfghjkl"\n', encoding="utf-8"
    )

    assert scanner.scan(tmp_path) == []
