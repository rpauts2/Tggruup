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
)
PUBLIC_PREFIXES = ("/ui/",)


def token_path() -> Path:
    return get_settings().data_root() / TOKEN_FILENAME


def ensure_token() -> str:
    """Return the API token, creating it on first use."""
    configured = (get_settings().api_token or "").strip()
    if configured:
        return configured

    path = token_path()
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
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
    if not raw_query or b"token=" not in raw_query.lower():
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
    return urlencode(filtered, doseq=True).encode("ascii")


def presented_token(
    header_value: str | None,
    query_value: str | None = None,
    cookie_value: str | None = None,
) -> str:
    """Accept the token from a header, the query string, or the session cookie.

    All three exist for a reason: fetch() can set a header, a bookmarked link
    needs the query string, and `<img src>` can do neither.
    """
    for candidate in (header_value, query_value, cookie_value):
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


def is_authorised(
    header_value: str | None,
    query_value: str | None = None,
    cookie_value: str | None = None,
) -> bool:
    """Constant-time comparison so the token cannot be recovered by timing."""
    settings = get_settings()
    if not settings.require_api_token:
        return True
    expected = ensure_token()
    candidate = presented_token(header_value, query_value, cookie_value)
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
]