"""qBittorrent Web API client: session login + torrents listing. Stdlib HTTP.

Credentials come from core.secrets (never hardcoded). Read-only listing; actions
(pause/tag/delete) are added only when a module needs them, behind opt-in config.
"""

from __future__ import annotations

import http.cookiejar
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from urllib.parse import urlencode

from core.errors import IntegrationError

from integrations._net import require_http_url

Transport = Callable[[str, str, "Mapping[str, str] | None"], str]


def _make_urllib_transport(
    base_url: str, timeout: float
) -> Transport:  # pragma: no cover - real IO
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def transport(method: str, path: str, data: Mapping[str, str] | None) -> str:
        encoded = urlencode(dict(data)).encode("utf-8") if data else None
        request = urllib.request.Request(base_url.rstrip("/") + path, data=encoded, method=method)
        with opener.open(request, timeout=timeout) as response:
            return str(response.read().decode("utf-8"))

    return transport


def _as_dict(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _as_list_of_dicts(value: object) -> list[dict[str, object]]:
    return [_as_dict(item) for item in value] if isinstance(value, list) else []


class QbitClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 30.0,
        transport: Transport | None = None,
    ) -> None:
        self._base = require_http_url(base_url).rstrip("/")
        self._user = username
        self._pass = password
        self._transport: Transport = transport or _make_urllib_transport(self._base, timeout)
        self._logged_in = False

    def _login(self) -> None:
        try:
            body = self._transport(
                "POST", "/api/v2/auth/login", {"username": self._user, "password": self._pass}
            )
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise IntegrationError(f"qBittorrent login failed: {exc}") from exc
        if body.strip() != "Ok.":
            raise IntegrationError("qBittorrent login rejected")
        self._logged_in = True

    def torrents(self) -> list[dict[str, object]]:
        if not self._logged_in:
            self._login()
        try:
            body = self._transport("GET", "/api/v2/torrents/info", None)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise IntegrationError(f"qBittorrent torrents/info failed: {exc}") from exc
        try:
            return _as_list_of_dicts(json.loads(body))
        except json.JSONDecodeError as exc:
            raise IntegrationError("qBittorrent returned invalid JSON") from exc
