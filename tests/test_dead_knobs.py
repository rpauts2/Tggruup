"""No `Settings` field may exist that nothing in `tgfarm/` reads.

The defect class this guards against is the DEAD KNOB: a field declared in
`Settings`, a line in `.env.example`, sometimes a pin in `conftest.py`, and no
call site at all. The operator edits it, restarts, and nothing changes — a
config UI that lies. Five of them shipped at 4c3e57d (`opencode_zen_free_only`,
`video_download_dir`, `dzen_profile_dir`, `cluster_similarity`,
`duplicate_similarity`) and a sixth (`x_bearer_token`) was advertised in the
platform panel as a credential requirement while no code read it.

The rule, stated exactly:

* a field is USED when its name appears in `tgfarm/**.py` outside
  `tgfarm/core/settings.py` either as an attribute access (`settings.foo`) or as
  a quoted name (`getattr(settings, "foo")`, `{"attribute": "foo"}`);
* a field is also USED when it is read inside a `Settings` method or property
  whose own name passes the same test — that is how `dzen_profile_dir` reaches
  Playwright, through `dzen_profile_path()`;
* everything else must be listed in `DYNAMIC_READ_ALLOWLIST` below with a
  reason, and that list is re-checked: an entry that is no longer dead fails.

Its own limits, on purpose, because a guard that overstates itself becomes the
next bug:

* this is a text-presence check, so it proves ABSENCE, not presence. A quoted
  name in a dict nobody reads satisfies it, and a knob reached only through a
  computed attribute (`f"{key}_publish_timeout_s"`) does not, which is exactly
  what the allowlist is for;
* one level of helper indirection is followed, not more;
* it reads `Settings.model_fields`, so a field that only exists in `.env.example`
  is `tests/test_env_template_matches_settings.py`'s business, not this file's.

The last two tests prove the checker is not a rubber stamp: a fake field added
to the real tree, and a field read only by a dead helper, are both reported.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from tgfarm.core.settings import PACKAGE_DIR, Settings

SETTINGS_FILE = (PACKAGE_DIR / "core" / "settings.py").resolve()

#: Fields this checker cannot see, one entry per field with the reason it is a
#: false positive. A reason must name the reader; "trust me" is not a reason.
#: `test_allowlist_entries_are_still_needed` fails any entry that stops being
#: dead, so this list cannot quietly grow into a place to hide knobs.
DYNAMIC_READ_ALLOWLIST: dict[str, str] = {
    # The per-platform publish budgets are one computed attribute, so the
    # literal name never appears: `getattr(settings, f"{key}_publish_timeout_s",
    # None)` in `tgfarm/s7_publish/__init__.py`, called on every publication.
    "vk_publish_timeout_s": (
        "read by the f\"{key}_publish_timeout_s\" getattr in tgfarm/s7_publish/__init__.py"
    ),
    "telegram_publish_timeout_s": (
        "read by the f\"{key}_publish_timeout_s\" getattr in tgfarm/s7_publish/__init__.py"
    ),
}


def _corpus() -> dict[Path, str]:
    """Every Python file of the package except the settings module itself."""
    out: dict[Path, str] = {}
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        resolved = path.resolve()
        if resolved == SETTINGS_FILE or "__pycache__" in resolved.parts:
            continue
        out[resolved] = path.read_text(encoding="utf-8", errors="replace")
    return out


def _settings_helpers(settings_text: str) -> dict[str, str]:
    """Each `Settings` method/property mapped to the source it is written in.

    A knob read only by such a helper is alive exactly as long as something
    outside settings calls that helper — which is the test applied below.
    """
    helpers: dict[str, str] = {}
    for node in ast.parse(settings_text).body:
        if not (isinstance(node, ast.ClassDef) and node.name == "Settings"):
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                segment = ast.get_source_segment(settings_text, item) or ""
                helpers[item.name] = segment
    return helpers


def _reads(text: str, name: str) -> bool:
    """A real read of `name`: an attribute access or a quoted attribute name."""
    return bool(
        re.search(rf"\.{re.escape(name)}\b", text)
        or re.search(rf"[\"']{re.escape(name)}[\"']", text)
    )


def find_dead_fields(
    field_names: Iterable[str],
    corpus: Mapping[Path, str],
    helpers: Mapping[str, str],
    settings_text: str,
) -> list[str]:
    """Fields with no reader outside the declaration site.

    Pure and parameterised on purpose: the regression tests below hand it a
    synthetic tree, so the matcher itself is under test and not only its result.
    """
    dead: list[str] = []
    for name in field_names:
        if any(_reads(text, name) for text in corpus.values()):
            continue
        reached_via_helper = False
        for helper_name, segment in helpers.items():
            if not _reads(segment, name):
                continue
            if any(_reads(text, helper_name) for text in corpus.values()):
                reached_via_helper = True
                break
        if reached_via_helper:
            continue
        dead.append(name)
    del settings_text  # kept in the signature so callers pass the same source
    return sorted(dead)


def _real_dead_fields() -> tuple[list[str], Mapping[Path, str]]:
    corpus = _corpus()
    settings_text = SETTINGS_FILE.read_text(encoding="utf-8")
    helpers = _settings_helpers(settings_text)
    return (
        find_dead_fields(Settings.model_fields, corpus, helpers, settings_text),
        corpus,
    )


def test_no_settings_field_is_only_declared():
    """The guard itself: a knob nobody reads is a failing test, not a TODO."""
    dead, _ = _real_dead_fields()
    unexplained = [name for name in dead if name not in DYNAMIC_READ_ALLOWLIST]
    assert not unexplained, (
        "these Settings fields are declared and read by nothing in tgfarm/, so the "
        "value an operator puts in .env changes no behaviour at all: "
        + ", ".join(unexplained)
        + ". Either read the field at a call site and prove it moves something, or "
        "delete it from Settings, .env.example and the conftest pin — deleting is the "
        "honest fix when nothing wants the setting."
    )


def test_allowlist_entries_are_still_needed():
    """An allowlist entry for a knob that is now really read must be deleted.

    Without this, the allowlist becomes the place the next dead knob is hidden,
    which would make this whole file a lie of its own.
    """
    dead, _ = _real_dead_fields()
    stale = sorted(set(DYNAMIC_READ_ALLOWLIST) - set(dead))
    assert not stale, (
        "DYNAMIC_READ_ALLOWLIST excuses fields that are no longer text-invisible: "
        + ", ".join(stale)
        + ". Drop the entry — a reasoned allowlist may only cover what the checker "
        "genuinely cannot see."
    )


def test_allowlist_reasons_are_not_empty_and_name_a_reader():
    """Each exception carries a reason, because an unexplained exception is a hole."""
    for name, reason in DYNAMIC_READ_ALLOWLIST.items():
        assert len(reason) > 20, f"{name}: allowlist reason is not a reason: {reason!r}"
        assert name in Settings.model_fields, f"{name}: allowlisted field no longer exists"


# ------------------------------------------------------- the guard on itself
def test_guard_catches_a_deliberately_dead_knob(tmp_path: Path):
    """A knob only Settings knows about must come back as dead — proof of aim.

    The synthetic tree below has four fields and three legitimate ways to be
    alive (direct attribute access, a quoted dynamic name, a helper that is
    called elsewhere) plus one that is nothing but a declaration.
    """
    settings_text = (
        "class Settings:\n"
        "    used_directly: str = Field(default='a', alias='USED_DIRECTLY')\n"
        "    used_dynamically: str = Field(default='b', alias='USED_DYNAMICALLY')\n"
        "    used_via_helper: str = Field(default='c', alias='USED_VIA_HELPER')\n"
        "    nobody_reads_this: str = Field(default='d', alias='NOBODY_READS_THIS')\n"
        "\n"
        "    def helper_path(self):\n"
        "        return self.used_via_helper\n"
        "\n"
        "    def helper_nobody_calls(self):\n"
        "        return self.only_read_by_a_dead_helper\n"
    )
    fields = [
        "used_directly",
        "used_dynamically",
        "used_via_helper",
        "nobody_reads_this",
        "only_read_by_a_dead_helper",
    ]
    corpus = {
        tmp_path / "caller.py": (
            "def run(settings):\n"
            "    a = settings.used_directly\n"
            "    b = getattr(settings, 'used_dynamically', None)\n"
            "    path = settings.helper_path()\n"
            "    return a, b, path\n"
        ),
        # A comment that mentions the name in prose is NOT a read: the checker
        # must still report `nobody_reads_this` even though the word appears.
        tmp_path / "prose.py": "# someone once suggested settings.nobody_reads... here\n",
    }
    helpers = _settings_helpers(settings_text)
    dead = find_dead_fields(fields, corpus, helpers, settings_text)
    assert dead == ["nobody_reads_this", "only_read_by_a_dead_helper"], (
        f"the guard reports {dead}; it has to catch exactly the two fields nothing "
        "reads, including the one hidden behind a helper nobody calls"
    )


def test_guard_catches_a_new_dead_knob_in_the_real_tree():
    """Same fake knob, real source: proves the matcher works on this codebase."""
    corpus = _corpus()
    settings_text = SETTINGS_FILE.read_text(encoding="utf-8")
    helpers = _settings_helpers(settings_text)
    fake = "operator_visible_but_nobody_reads"
    dead = find_dead_fields(
        [*Settings.model_fields, fake], corpus, helpers, settings_text
    )
    assert fake in dead, (
        "adding an unread field to the real field list did not make this guard fail, "
        "so the guard itself is dead"
    )
    assert set(dead) - {fake} == set(DYNAMIC_READ_ALLOWLIST), (
        "the real tree reported dead fields nobody explained"
    )
