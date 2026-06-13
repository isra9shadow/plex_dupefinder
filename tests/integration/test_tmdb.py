"""Tests for the TMDb client (injected fetcher, cache)."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from core.errors import IntegrationError
from integrations.tmdb import TmdbClient


def _fetcher(routes: dict[str, object]):
    def fetch(url: str, headers: Mapping[str, str], timeout: float) -> str:
        assert headers["Authorization"] == "Bearer TOK"
        for needle, payload in routes.items():
            if needle in url:
                return json.dumps(payload)
        raise OSError(f"no route for {url}")

    return fetch


def test_expected_movie_runtime() -> None:
    client = TmdbClient(
        "TOK",
        fetcher=_fetcher({"search/movie": {"results": [{"id": 42}]}, "movie/42": {"runtime": 118}}),
    )
    assert client.expected_movie_runtime("Dune", 2021) == 118
    # second call is served from cache (same result, no new route needed)
    assert client.expected_movie_runtime("Dune", 2021) == 118


def test_movie_not_found() -> None:
    client = TmdbClient("TOK", fetcher=_fetcher({"search/movie": {"results": []}}))
    assert client.expected_movie_runtime("Nope") is None


def test_zero_runtime_is_none() -> None:
    client = TmdbClient(
        "TOK",
        fetcher=_fetcher({"search/movie": {"results": [{"id": 7}]}, "movie/7": {"runtime": 0}}),
    )
    assert client.expected_movie_runtime("Short") is None


def test_request_failure_raises() -> None:
    def boom(url: str, headers: Mapping[str, str], timeout: float) -> str:
        raise OSError("down")

    with pytest.raises(IntegrationError):
        TmdbClient("TOK", fetcher=boom).expected_movie_runtime("X")
