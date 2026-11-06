"""Admin API authentication.

The dashboard can force-publish and trigger cycles. Without a secret, any
process on the machine — or any web page the operator has open, since browsers
reach `127.0.0.1` happily — can drive the farm.

The token is generated once, stored outside the database, and required on every
`/api/*` call. Two details make this work without a login form:

* the token is required as a **custom header**, which a cross-origin HTML form
  cannot set, so a random page cannot forge a request;
* it is injected into the dashboard HTML, which same-origin policy keeps away
  from other pages, so the page itself is the only holder of it.

A local bind is the default. Exposing the farm to a network is a deliberate act
(`--host 0.0.0.0`) and the token still applies.
"""

from __future__ import annotations

import hmac
import os
import secrets
import stat
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from tgfarm.core.logging import get_logger
from tgfarm.core.settings import get_settings

log = get_logger(__name__)

HEADER = "X-TGFarm-Token"
COOKIE = "tgfarm_token"
TOKEN_FILENAME = "api_token"
TOKEN_BYTES = 32

# Endpoints that stay reachable without a token: the dashboard shell itself, its
# static assets, and a liveness probe for process supervisors.
PUBLIC_PATHS = (
    "/health", "/favicon.ico", "/ui/",
    "/api/platforms/mastodon/auth/callback",
    "/api/platforms/x/auth/callback",
    "/api/platforms/threads/auth/callback",
    "/api/platforms/linkedin/auth/callback",
    "/api/platforms/youtube/auth/callback",
    "/api/platforms/tiktok/auth/callback",
    "/api/platforms/pinterest/auth/callback",
    "/api/platforms/facebook/auth/callback",
    "/api/platforms/reddit/auth/callback",
    "/api/platforms/tumblr/auth/callback",
    "/api/platforms/max/webhook",
)
PUBLIC_PREFIXES = ("/ui/", "/public/instagram-media/")


def token_path() -> Path:
    return get_settings().data_root() / TOKEN_FILENAME


def token_source() -> str:
    """Where the token comes from — a state, never a value.

    `tgfarm serve` printed the token itself for years, and because the live farm is
    `python -u -m tgfarm.cli serve` with its output redirected to a file in the repo,
    that made a standing credential a plaintext line in every `farm_serve*.log`. The
    replacement is this function: an operator still learns whether the token is
    configured, already on disk, or about to be created — which is the only part of
    the old line that was ever diagnostic — and reads the value only by asking for it.
    """
    if (get_settings().api_token or "").strip():
        return "env"
    if token_path().exists():
        return "file"
    return "generated"


def _register_for_logging(token: str) -> None:
    """Hand the value to the log redactor, so a record cannot print it.

    `core/redact` discovers secrets from the environment and from the token file.
    A token that exists only in settings — supplied by a dotenv file, a test, or
    an in-memory configuration — is in neither place, and that is exactly the
    token the serve path is about to use. Telemetry must never be able to break
    authentication, so a failure here is swallowed: the token still works, it is
    simply not protected in logs until the next emit registers it.
    """
    if not token:
        return
    try:
        from tgfarm.core import redact

        redact.register_secret(token)
    except Exception:  # noqa: BLE001 - redaction is never allowed to break auth
        log.debug("could not register the admin token for log redaction")


def ensure_token() -> str:
    """Return the API token, creating it on first use."""
    configured = (get_settings().api_token or "").strip()
    if configured:
        _register_for_logging(configured)
        return configured

    path = token_path()
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            _register_for_logging(existing)
            return existing

    token = secrets.token_urlsafe(TOKEN_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        # Windows mostly ignores the mode, POSIX uses it: the file is a secret.
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        log.debug("could not restrict permissions on %s", path)
    log.info("generated admin API token at %s", path)
    _register_for_logging(token)
    return token


def rotate_token() -> str:
    """Issue a fresh token, e.g. after the old one leaked into a log."""
    path = token_path()
    path.unlink(missing_ok=True)
    return ensure_token()


def strip_token_from_query(raw_query: bytes) -> bytes:
    """Remove credentials from a query string before it reaches access logs.

    The token is read by the auth middleware first. Routes do not need it after
    authorization; leaving it in the ASGI scope risks leaking it into server
    access logs and downstream exception reports.
    """
    if not raw_query:
        return raw_query
    try:
        pairs = parse_qsl(
            raw_query.decode("utf-8", errors="replace"),
            keep_blank_values=True,
            max_num_fields=1024,
        )
    except ValueError:
        # An excessive/malformed query must not preserve a possible credential.
        return b""
    filtered = [(key, value) for key, value in pairs if key.lower() != "token"]
    if len(filtered) == len(pairs):
        return raw_query
    return urlencode(filtered, doseq=True).encode("ascii")


def presented_token(
    header_value: str | None,
    cookie_value: str | None = None,
) -> str:
    """Accept credentials only from the custom header or the HttpOnly session cookie.

    Query-string credentials are deliberately rejected because URLs can be copied,
    stored in browser history, or exposed through referrer/access telemetry.
    The dashboard uses a header for fetch(); media elements use a SameSite=Strict,
    HttpOnly cookie because an image/video request cannot set custom headers.
    """
    for candidate in (header_value, cookie_value):
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


def is_authorised(
    header_value: str | None,
    cookie_value: str | None = None,
) -> bool:
    """Constant-time comparison so the token cannot be recovered by timing."""
    settings = get_settings()
    if not settings.require_api_token:
        return True
    expected = ensure_token()
    candidate = presented_token(header_value, cookie_value)
    if not candidate:
        return False
    return hmac.compare_digest(candidate, expected)


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def read_token_from_environment() -> str:
    """The desktop app passes the token this way instead of writing it to disk."""
    return os.environ.get("TGFARM_API_TOKEN", "").strip()


__all__ = [
    "COOKIE",
    "HEADER",
    "ensure_token",
    "is_authorised",
    "is_public_path",
    "read_token_from_environment",
    "rotate_token",
    "strip_token_from_query",
    "token_path",
    "token_source",
]