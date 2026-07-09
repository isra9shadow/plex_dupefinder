"""Shared base client for the *arr v3 API (Radarr/Sonarr).

Uses stdlib HTTP (no extra dependency) and accepts an injectable ``fetcher`` so
tests run without a network. Never leaks the API key into logs.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping

from core.errors import IntegrationError

from integrations._net import require_http_url

JsonFetcher = Callable[[str, Mapping[str, str], float], str]
# (method, url, headers, body, timeout) -> response text. Injected so tests never
# touch the network; the write path is separate from the read ``fetcher``.
JsonWriter = Callable[[str, str, Mapping[str, str], "bytes | None", float], str]


def _urllib_get(
    url: str, headers: Mapping[str, str], timeout: float
) -> str:  # pragma: no cover - real IO
    request = urllib.request.Request(url, headers=dict(headers))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return str(response.read().decode("utf-8"))


def _urllib_write(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
) -> str:  # pragma: no cover - real IO
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return str(response.read().decode("utf-8"))


def _as_dict(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _as_list_of_dicts(value: object) -> list[dict[str, object]]:
    return [_as_dict(item) for item in value] if isinstance(value, list) else []


class ArrClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        fetcher: JsonFetcher = _urllib_get,
        writer: JsonWriter = _urllib_write,
    ) -> None:
        self._base = require_http_url(base_url).rstrip("/")
        self._key = api_key
        self._timeout = timeout
        self._fetch = fetcher
        self._write = writer

    def _get(self, path: str) -> object:
        url = f"{self._base}/api/v3/{path.lstrip('/')}"
        try:
            body = self._fetch(url, {"X-Api-Key": self._key}, self._timeout)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise IntegrationError(f"ARR request failed: {url}: {exc}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise IntegrationError(f"ARR returned invalid JSON from {url}") from exc

    def _send(self, method: str, path: str, payload: object) -> object:
        """POST/PUT a JSON body to the *arr API (write path). Empty body → {}."""
        url = f"{self._base}/api/v3/{path.lstrip('/')}"
        headers = {"X-Api-Key": self._key, "Content-Type": "application/json"}
        body = json.dumps(payload).encode("utf-8")
        try:
            text = self._write(method, url, headers, body, self._timeout)
        except urllib.error.HTTPError as exc:  # include Radarr's validation detail
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300].strip()
            except Exception:  # best effort; the body may be unreadable
                detail = ""
            raise IntegrationError(
                f"ARR write failed: {method} {url}: HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise IntegrationError(f"ARR write failed: {method} {url}: {exc}") from exc
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise IntegrationError(f"ARR returned invalid JSON from {url}") from exc

    def _list(self, path: str) -> list[dict[str, object]]:
        return _as_list_of_dicts(self._get(path))

    # --- tags (shared by Radarr/Sonarr) ---------------------------------------
    def tags(self) -> list[dict[str, object]]:
        """All tags: ``[{id, label}, …]`` (GET /tag)."""
        return self._list("tag")

    def create_tag(self, label: str) -> dict[str, object]:
        """Create a tag and return ``{id, label}`` (POST /tag)."""
        return _as_dict(self._send("POST", "tag", {"label": label}))

    def system_status(self) -> dict[str, object]:
        return _as_dict(self._get("system/status"))

    def rootfolders(self) -> list[str]:
        out: list[str] = []
        for folder in self._list("rootfolder"):
            path = folder.get("path")
            if isinstance(path, str):
                out.append(path)
        return out

    def queue(self) -> list[dict[str, object]]:
        raw = self._get("queue?page=1&pageSize=1000")
        if isinstance(raw, dict) and "records" in raw:
            return _as_list_of_dicts(raw.get("records"))
        return _as_list_of_dicts(raw)
