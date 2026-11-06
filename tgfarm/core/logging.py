"""Structured logging with one-line console format and optional file sink.

Every handler's output passes through the W24-S RedactingFormatter, so secret
values and credential-shaped text cannot reach consoles or farm_serve*.log
regardless of which call site emitted the record.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from tgfarm.core.redact import RedactingFormatter

_CONFIGURED = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("channel", "stage", "post_id", "cluster_id", "ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", json_out: bool = False, log_file: Path | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(level.upper())

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(RedactingFormatter(_JsonFormatter() if json_out else logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-22s %(message)s", "%H:%M:%S"
    )))
    root.addHandler(console)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(RedactingFormatter(_JsonFormatter()))
        root.addHandler(fh)

    # third-party noise
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default", "aiogram.event"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)