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

from conftest import DETERMINISTIC_TUNABLES, OFFLINE_CREDENTIALS

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
        "CLUSTER_SIMILARITY": "0.5",
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
    """The half of the isolation the new pin must not undo."""
    settings = Settings()
    assert os.environ.get("TGFARM_MODE", "off") == "off"
    assert "offline" in settings.provider_chain
    leaked = [name for name in OFFLINE_CREDENTIALS if os.environ.get(name)]
    assert not leaked, f"credentials present in the test environment: {leaked}"
    assert settings.image_enabled is False
    assert settings.video_enabled is False


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
