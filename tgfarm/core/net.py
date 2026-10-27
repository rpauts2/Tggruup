"""Outbound URL safety for content fetched from feeds.

A feed entry controls a URL that the farm will fetch (article images, video,
og: previews). A malicious or broken feed can therefore point the fetcher at
loopback services, the cloud metadata endpoint or internal network hosts.
Everything outbound from S6/S11 goes through `assert_public_http_url`.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from tgfarm.core.logging import get_logger

log = get_logger(__name__)


class UnsafeURLError(ValueError):
    """The URL points at private, loopback or link-local infrastructure."""


_BLOCKED_NETWORKS = [
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
]


def host_is_private(host: str) -> bool:
    """True when the host resolves only into infrastructure ranges.

    Unresolvable hosts are denied by default: a feed typo is not worth an
    internal request. IP literals never touch DNS.
    """
    host = (host or "").strip().lower()
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host.rstrip("."), None)
    except OSError:
        return True
    for info in infos:
        address = str(info[4][0]).split("%")[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return True
        if any(ip in net for net in _BLOCKED_NETWORKS):
            return True
    return False


def assert_public_http_url(url: str) -> str:
    """Return the URL when it is http(s) to a public host, else raise."""
    parts = urlparse(url or "")
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError(f"scheme not allowed: {parts.scheme!r}")
    host = parts.hostname or ""
    if host_is_private(host):
        raise UnsafeURLError(f"host not allowed: {host!r}")
    return url


__all__ = ["UnsafeURLError", "assert_public_http_url", "host_is_private"]
