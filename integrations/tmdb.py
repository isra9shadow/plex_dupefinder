"""TMDb client (expected movie runtime). Stdlib HTTP, injectable fetcher, cached.

Bearer token comes from core.secrets (never hardcoded). Read-only.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from urllib.parse import urlencode

from core.errors import IntegrationError

from integrations._net import require_http_url

JsonFetcher = Callable[[str, Mapping[str, str], float], str]


def _urllib_get(
    url: str, headers: Mapping[str, str], timeout: float
) -> str:  # pragma: no cover - real IO
    request = urllib.request.Request(url, headers=dict(headers))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return str(response.read().decode("utf-8"))


def _as_dict(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


class TmdbClient:
    def __init__(
        self,
        bearer: str,
        *,
        base_url: str = "https://api.themoviedb.org/3",
        timeout: float = 10.0,
        fetcher: JsonFetcher = _urllib_get,
    ) -> None:
        self._bearer = bearer
        self._base = require_http_url(base_url).rstrip("/")
        self._timeout = timeout
        self._fetch = fetcher
        self._cache: dict[tuple[str, int | None], int | None] = {}

    def _get(self, path: str, params: Mapping[str, str]) -> object:
        url = f"{self._base}/{path}?{urlencode(dict(params))}"
        headers = {"Authorization": f"Bearer {self._bearer}", "accept": "application/json"}
        try:
            body = self._fetch(url, headers, self._timeout)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise IntegrationError(f"TMDb request failed: {path}: {exc}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise IntegrationError(f"TMDb returned invalid JSON: {path}") from exc

    def _search_movie(self, title: str, year: int | None) -> int | None:
        params = {"query": title}
        if year is not None:
            params["year"] = str(year)
        results = _as_dict(self._get("search/movie", params)).get("results")
        if isinstance(results, list) and results:
            movie_id = _as_dict(results[0]).get("id")
            if isinstance(movie_id, int):
                return movie_id
        return None

    def expected_movie_runtime(self, title: str, year: int | None = None) -> int | None:
        key = (title, year)
        if key in self._cache:
            return self._cache[key]
        runtime: int | None = None
        movie_id = self._search_movie(title, year)
        if movie_id is not None:
            value = _as_dict(self._get(f"movie/{movie_id}", {})).get("runtime")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                runtime = value
        self._cache[key] = runtime
        return runtime
