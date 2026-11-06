"""W24-S: the logging layer must scrub secrets from every formatted record.

Sentinels only — never a real credential. Each test drives the production
pipeline (setup_logging -> handlers -> RedactingFormatter -> stream) so a
mutation that unwires the formatter, the registry, or the pattern chain kills
at least one of these tests.
"""

from __future__ import annotations

import io
import json
import logging
import sys

import pytest

from tgfarm.core import logging as core_logging
from tgfarm.core import redact
from tgfarm.core.errors import ProviderUnavailable

SENTINEL_VALUE = "SENTINEL-DONTLEAK-abcdef"
SENTINEL_RAW_B64ISH = "SENTINEL.DONTLEAK.abcdefghij0123456789"


@pytest.fixture()
def capture_root_logs():
    """Install a StringIO handler through the production setup_logging path."""
    saved_handlers = list(logging.root.handlers)
    saved_level = logging.root.level
    saved_configured = core_logging._CONFIGURED
    stream = io.StringIO()
    for handler in saved_handlers:
        handler.close()
    logging.root.handlers.clear()
    core_logging._CONFIGURED = False
    core_logging.setup_logging("DEBUG")
    probe = logging.StreamHandler(stream)
    probe.setLevel(logging.DEBUG)
    # reuse the formatter production just installed on the console handler, so
    # the tests measure the real wiring, not a private copy of it.
    console = [h for h in logging.root.handlers if isinstance(h, logging.StreamHandler)
               and getattr(h, "stream", None) is sys.stdout]
    assert console, "setup_logging must install a console handler"
    probe.setFormatter(console[0].formatter)
    logging.root.addHandler(probe)
    try:
        yield stream
    finally:
        for handler in logging.root.handlers:
            handler.close()
        logging.root.handlers.clear()
        logging.root.handlers.extend(saved_handlers)
        logging.root.setLevel(saved_level)
        core_logging._CONFIGURED = saved_configured


@pytest.fixture()
def clean_registry(monkeypatch):
    monkeypatch.delenv("VK_USER_TOKEN", raising=False)
    monkeypatch.delenv("TGFARM_API_TOKEN", raising=False)
    # autoload must not find the token file during unit runs
    monkeypatch.setattr(
        redact, "_discover_secret_values", lambda: set()
    )
    redact.clear_registry()
    yield
    redact.clear_registry()


def test_registered_secret_never_reaches_the_log(capture_root_logs, clean_registry):
    redact.register_secret(SENTINEL_VALUE)
    log = logging.getLogger("tgfarm.test.w24s")
    try:
        raise ProviderUnavailable(f"upload rejected: rotated credential {SENTINEL_VALUE}")
    except ProviderUnavailable:
        log.exception("stage publish failed")
    output = capture_root_logs.getvalue()
    assert SENTINEL_VALUE not in output
    assert "[REDACTED]" in output


def test_exception_classification_survives_redaction(capture_root_logs, clean_registry):
    redact.register_secret(SENTINEL_VALUE)
    log = logging.getLogger("tgfarm.test.w24s")
    try:
        raise ProviderUnavailable(f"upload rejected: rotated credential {SENTINEL_VALUE}")
    except ProviderUnavailable:
        log.exception("stage publish failed")
    output = capture_root_logs.getvalue()
    # the log stays useful: type name, message shape and traceback header remain
    assert "ProviderUnavailable" in output
    assert "upload rejected: rotated credential" in output
    assert "[REDACTED]" in output
    assert "Traceback (most recent call last)" in output


def test_pattern_chain_catches_unregistered_bearer_and_query_secrets(capture_root_logs, clean_registry):
    log = logging.getLogger("tgfarm.test.w24s.patterns")
    log.warning("provider probe failed: %s", f"401 https://api.vk.com/method?access_token={SENTINEL_RAW_B64ISH}")
    log.info("auth header %s", f"Authorization: Bearer {SENTINEL_RAW_B64ISH}")
    log.debug("session %s", f"Cookie: tgfarm_token={SENTINEL_RAW_B64ISH}")
    output = capture_root_logs.getvalue()
    assert SENTINEL_RAW_B64ISH not in output
    assert "access_token=[REDACTED]" in output
    # the credential shape is scrubbed while the call-site message shape survives;
    # "Bearer" itself may be swallowed by the broader authorization: key pattern.
    assert "Authorization: [REDACTED]" in output
    assert "provider probe failed" in output and "auth header" in output


def test_env_secret_autoloads_into_registry(capture_root_logs, monkeypatch):
    monkeypatch.setenv("VK_USER_TOKEN", SENTINEL_VALUE)
    monkeypatch.delenv("TGFARM_API_TOKEN", raising=False)
    real_discover = redact._discover_secret_values
    calls = []

    def discover():
        calls.append(1)
        return real_discover()

    monkeypatch.setattr(redact, "_discover_secret_values", discover)
    redact.clear_registry()
    log = logging.getLogger("tgfarm.test.w24s.env")
    log.error("vk call failed: %s", f"API error 5: bad token value {SENTINEL_VALUE}")
    output = capture_root_logs.getvalue()
    assert SENTINEL_VALUE not in output
    assert "vk call failed" in output
    assert calls, "autoload must run once through the real discovery path"


def test_json_file_formatter_output_is_redacted(capture_root_logs, clean_registry):
    redact.register_secret(SENTINEL_VALUE)
    json_formatter = redact.RedactingFormatter(core_logging._JsonFormatter())
    record = logging.LogRecord(
        "tgfarm.test.w24s.json", logging.ERROR, "fake.py", 1,
        "cycle crashed: admin token %s", (SENTINEL_VALUE,), None,
    )
    line = json_formatter.format(record)
    payload = json.loads(line)  # must remain valid JSON
    assert SENTINEL_VALUE not in payload["msg"]
    assert "cycle crashed: admin token" in payload["msg"]
    assert payload["level"] == "ERROR"
    assert "SENTINEL" not in line


def test_deliberate_stdout_path_is_not_redacted(capsys, clean_registry):
    """The api-token verb prints on purpose; stdout is not a log record."""
    redact.register_secret(SENTINEL_VALUE)
    print(f"токен:   {SENTINEL_VALUE}")
    captured = capsys.readouterr()
    assert SENTINEL_VALUE in captured.out


def test_non_secret_text_passes_through_unchanged(clean_registry):
    harmless = "stage s7_publish ok after 1200ms post_id=42 channel=news"
    assert redact.redact_log_text(harmless) == harmless


def test_setup_logging_wires_redaction_on_console_handler():
    saved_handlers = list(logging.root.handlers)
    saved_configured = core_logging._CONFIGURED
    logging.root.handlers.clear()
    core_logging._CONFIGURED = False
    try:
        core_logging.setup_logging("INFO", json_out=True)
        wrapped = [h for h in logging.root.handlers if isinstance(h, logging.StreamHandler)]
        assert wrapped, "setup_logging must keep installing a console handler"
        assert all(isinstance(h.formatter, redact.RedactingFormatter) for h in wrapped)
    finally:
        for handler in logging.root.handlers:
            handler.close()
        logging.root.handlers.clear()
        logging.root.handlers.extend(saved_handlers)
        core_logging._CONFIGURED = saved_configured
