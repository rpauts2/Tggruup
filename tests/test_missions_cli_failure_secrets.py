"""CLI error boundaries must not leak even unrecognizable credentials."""
from __future__ import annotations

import json
import logging

import pytest

from tgfarm import cli
from tgfarm.core import apiauth

SENTINEL = "TEST_SECRET_SENTINEL_7f9c2e"


@pytest.mark.parametrize("command", [["serve", "--no-scheduler"], ["api-token"], ["api-token", "--rotate"]])
def test_token_initialization_failure_is_safe(command, monkeypatch, capsys, caplog):
    def fail():
        try:
            raise ValueError(f"Authorization: Bearer {SENTINEL}")
        except ValueError as cause:
            raise RuntimeError(f"bare credential {SENTINEL}") from cause

    # create_app also initializes the module-level app. Import before injecting
    # failures so this test cannot leave a poisoned closure in sys.modules.
    from tgfarm import api

    assert callable(api.create_app)
    monkeypatch.setattr(cli, "setup_logging", lambda *_: None)
    monkeypatch.setattr(apiauth, "ensure_token", fail)
    monkeypatch.setattr(apiauth, "rotate_token", fail)
    with caplog.at_level(logging.ERROR):
        assert cli.main(command) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"] == "RuntimeError"
    assert SENTINEL not in captured.out + captured.err + caplog.text
    assert caplog.records
    for record in caplog.records:
        assert SENTINEL not in record.getMessage()
        assert record.exc_info is None
        assert record.exc_text is None


def test_server_run_failure_is_safe(monkeypatch, capsys, caplog):
    import uvicorn

    def fail(*_args, **_kwargs):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(cli, "setup_logging", lambda *_: None)
    monkeypatch.setattr(apiauth, "ensure_token", lambda: SENTINEL)
    monkeypatch.setattr(uvicorn, "run", fail)
    assert cli.main(["serve", "--no-scheduler"]) == 1
    captured = capsys.readouterr()
    assert "http://" in captured.out
    assert SENTINEL not in captured.out + captured.err + caplog.text
