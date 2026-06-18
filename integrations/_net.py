"""Shared HTTP guards for integration clients (defense-in-depth)."""

from __future__ import annotations

from urllib.parse import urlsplit

from core.errors import IntegrationError


def require_http_url(url: str) -> str:
    """Validate a configured base URL: only ``http``/``https`` with a host.

    Fails closed against ``file://``/``ftp://``/``data:`` and malformed config
    (a typo'd URL is rejected loudly instead of being silently opened). The URL
    still comes from the operator's own config, so this is hardening, not an
    untrusted-input boundary.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise IntegrationError(f"unsupported or malformed URL (need http(s)://host): {url!r}")
    return url
