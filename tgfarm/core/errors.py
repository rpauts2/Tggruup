"""Domain errors. The pipeline catches these per-stage so one bad item never
kills a run."""

from __future__ import annotations


class TgfarmError(Exception):
    """Base class."""


class ConfigError(TgfarmError):
    """Missing or invalid configuration that cannot be defaulted."""


class ProviderUnavailable(TgfarmError):
    """An external provider is not configured or refused the call."""


class ProviderRateLimited(ProviderUnavailable):
    """Provider signalled backpressure; retry after `retry_after` seconds."""

    def __init__(self, message: str, retry_after: float = 5.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class QualityRejected(TgfarmError):
    """Content failed a quality gate."""

    def __init__(self, message: str, gate: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.gate = gate
        self.details = details or {}


class PublishError(TgfarmError):
    """Telegram refused to publish; message stays queued for retry."""


class AdapterNotConfigured(TgfarmError):
    """Crossposting adapter has no credentials."""