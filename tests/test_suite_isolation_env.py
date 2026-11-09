"""The suite must compute the same numbers on every machine.

Regression guard for a class of defect, not one setting: an untracked operator
`.env` sitting next to the checkout used to be able to move any ordinary tunable
(heat threshold, interval, budget, alert window) under a test that never said so.
At 98958e4 `tests/test_trends.py::test_heat_rewards_multi_source_fresh_stories`
passed on the operator laptop with `HEAT_THRESHOLD=0.55` and failed in CI and in
fresh worktrees on the same commit with the code default 0.68 (`assert 0 == 1`,
CI run 41920465137). Nothing in the test and nothing in the run explained it.

These tests fail if that leak opens again, whoever the operator is and whatever
their `.env` says.
"""

from __future__ import annotations

import os
from pathlib import Path

from conftest import (
    DETERMINISTIC_TUNABLES,
    OFFLINE_CREDENTIALS,
    _all_credential_aliases,
    is_credential_alias,
)

from tgfarm.core.settings import Settings


def _by_alias() -> dict[str, tuple[str, object]]:
    """Map each env alias to (field name, code-declared default)."""
    return {
        (field.alias or name): (name, field.default)
        for name, field in Settings.model_fields.items()
    }


def test_pin_list_names_real_settings_fields_with_real_defaults():
    """A renamed or deleted alias must fail loudly, not quietly stop pinning."""
    by_alias = _by_alias()
    missing = [alias for alias in DETERMINISTIC_TUNABLES if alias not in by_alias]
    no_default = [alias for alias in DETERMINISTIC_TUNABLES if by_alias.get(alias, (None, None))[1] is None]
    assert not missing, f"conftest pins aliases Settings no longer declares: {missing}"
    assert not no_default, f"conftest pins aliases that have no code default to pin: {no_default}"
    # The alias that started the incident has to stay in the list.
    assert "HEAT_THRESHOLD" in DETERMINISTIC_TUNABLES


def test_pinned_tunables_are_exported_by_conftest():
    """The mechanism is 'environment wins over .env', so each alias must be set."""
    unpinned = [alias for alias in DETERMINISTIC_TUNABLES if alias not in os.environ]
    assert not unpinned, f"tunables conftest forgot to export: {unpinned}"


def test_effective_settings_equal_the_code_defaults():
    """No machine's `.env` may move a pinned tunable away from what code declares."""
    by_alias = _by_alias()
    settings = Settings()
    drifted = []
    for alias in DETERMINISTIC_TUNABLES:
        field_name, declared = by_alias[alias]
        value = getattr(settings, field_name)
        if value != declared:
            drifted.append(f"{alias}={value!r} (code declares {declared!r})")
    assert not drifted, (
        "an operator .env is overriding test tunables, so these settings — and every "
        "test that reads them — now depend on one machine's configuration: "
        + "; ".join(drifted)
    )


def test_a_local_env_file_cannot_move_a_pinned_tunable(tmp_path: Path):
    """Prove the block, with a control that proves the `.env` really is read."""
    leak = {
        "HEAT_THRESHOLD": "0.5",
        "COLLECT_INTERVAL_MIN": "1",
        "AI_POLICY": "always",
        "AI_DAILY_BUDGET": "999999",
        "QUEUE_BACKLOG_ALERT_THRESHOLD": "1",
        "DUPLICATE_SIMILARITY": "0.5",
        "SOURCE_MAX_AGE_HOURS": "1",
        "DZEN_MIN_GAP_S": "1",
        "EMBEDDING_DIM": "64",
        "IMAGE_ENABLED": "true",
        "IMAGE_PROVIDER": "pollinations",
        "TGFARM_APPROVAL_REQUIRED": "true",
        # Control: deliberately not a pinned tunable, so it must get through.
        # Without it this test would still pass if .env reading broke entirely.
        "TGFARM_API_PORT": "8123",
    }
    env_path = tmp_path / ".env"
    env_path.write_text("\n".join(f"{key}={value}" for key, value in leak.items()), encoding="utf-8")

    settings = Settings(_env_file=env_path)
    by_alias = _by_alias()
    moved = []
    for alias, value in leak.items():
        entry = by_alias.get(alias)
        if entry is None or alias not in DETERMINISTIC_TUNABLES:
            continue
        field_name, declared = entry
        got = getattr(settings, field_name)
        if got != declared:
            moved.append(f"{alias} became {got!r}, wanted {declared!r} (leaked {value})")
    assert not moved, "a local .env overrode pinned test tunables: " + "; ".join(moved)
    assert settings.api_port == 8123, "the .env was not read at all: this test would prove nothing"


def test_credential_and_mode_isolation_survives_the_pin():
    """The half of the isolation the new pin must not undo.

    Checked against the blanking value, not against truthiness: an `int` expiry
    or a `bool` switch has to be blanked at a value its own type parses (`0`,
    `false`), and those are truthy strings for `os.environ.get`.
    """
    settings = Settings()
    assert os.environ.get("TGFARM_MODE", "off") == "off"
    assert "offline" in settings.provider_chain
    expected = _all_credential_aliases()
    leaked = [
        f"{name}={os.environ.get(name)!r}"
        for name, blank in expected.items()
        if os.environ.get(name) != blank
    ]
    assert not leaked, f"credentials present in the test environment: {leaked}"
    assert settings.image_enabled is False
    assert settings.video_enabled is False


def test_every_credential_field_is_blanked_not_just_the_listed_ones():
    """The hand-kept list covered 45 of 82 credential fields; the rest leaked.

    `Settings` is the source of truth, so a credential field that neither appears
    in `OFFLINE_CREDENTIALS` nor matches a credential name fragment must not
    exist, and no credential may be readable through the model in a test run.
    """
    by_alias = _by_alias()
    expected = _all_credential_aliases()
    uncovered = sorted(alias for alias in by_alias if alias not in expected and is_credential_alias(alias))
    assert not uncovered, f"credential fields conftest never blanks: {uncovered}"

    settings = Settings()
    readable = []
    for alias in expected:
        entry = by_alias.get(alias)
        if entry is None:
            continue
        value = getattr(settings, entry[0])
        if value not in (None, "", 0, False):
            readable.append(f"{alias}={value!r}")
    assert not readable, f"credentials still readable through Settings: {readable}"


def test_a_test_run_never_writes_into_the_operator_data_tree():
    """`data_root()` and `media_root()` must point outside the checkout.

    The suite shares one database and one media tree, and until this was fixed
    they were the operator's: `DATA_DIR`/`MEDIA_DIR` were module constants and
    the `TGFARM_MEDIA_DIR` conftest set was read by nothing in `tgfarm/`. A run
    left 1281 files in `data/media` and rewrote `data/platform_auth_checks.json`,
    and — worse — `core/apiauth.token_path()` read the live admin token from
    `data/api_token`, so every API test authenticated with a real secret.
    """
    from tgfarm.core.settings import ROOT

    settings = Settings()
    live = ROOT / "data"
    for name, root in (("data_root", settings.data_root()), ("media_root", settings.media_root())):
        assert live not in root.parents and root != live, (
            f"{name}() is {root}, inside the operator's live tree: a test run would "
            "read the real admin token and write media the farm later publishes"
        )
        assert root.is_dir(), f"{name}() returned {root}, which was not created"


def test_the_admin_token_is_not_the_one_on_disk():
    """A test must not authenticate with the token the running farm uses."""
    from tgfarm.core import apiauth
    from tgfarm.core.settings import ROOT

    live_path = ROOT / "data" / apiauth.TOKEN_FILENAME
    token_path = apiauth.token_path()
    assert token_path != live_path, f"the test run points at the live token {live_path}"
    if live_path.exists():
        live_token = live_path.read_text(encoding="utf-8").strip()
        assert apiauth.ensure_token() != live_token, (
            "the suite generated or read the live admin token; a test that leaks it "
            "leaks a working credential for the running farm"
        )


def test_heat_promotion_uses_the_pinned_default():
    """Behaviour, not bookkeeping: the trend gate a test asserts on is the code default.

    `tests/test_trends.py` builds a four-source story that scores about 0.91 and a
    weak single-source item that scores about 0.16 (measured by bisecting the
    promotion boundary). The default must keep separating them, otherwise those
    two tests are only green on the machine whose .env happens to put the
    threshold between them.
    """
    threshold = Settings().heat_threshold
    assert threshold == _by_alias()["HEAT_THRESHOLD"][1]
    assert 0.16 < threshold < 0.91, "the pinned heat threshold no longer separates the trend fixtures"
