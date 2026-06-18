"""Tests for the Radarr/Sonarr clients (injected fetcher, no network)."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from core.errors import IntegrationError
from integrations.radarr import RadarrClient
from integrations.sonarr import SonarrClient


def _fetcher(routes: dict[str, object]):
    def fetch(url: str, headers: Mapping[str, str], timeout: float) -> str:
        assert headers["X-Api-Key"] == "KEY"
        for needle, payload in routes.items():
            if needle in url:
                return json.dumps(payload)
        raise OSError(f"no route for {url}")

    return fetch


def test_radarr_movies() -> None:
    client = RadarrClient(
        "http://r/",
        "KEY",
        fetcher=_fetcher({"movie": [{"path": "/movies/A"}, {"path": "/movies/B"}]}),
    )
    assert [m["path"] for m in client.movies()] == ["/movies/A", "/movies/B"]


def test_sonarr_series() -> None:
    client = SonarrClient("http://s", "KEY", fetcher=_fetcher({"series": [{"path": "/tv/Show"}]}))
    assert client.series()[0]["path"] == "/tv/Show"


def test_rootfolders_extracts_paths() -> None:
    client = RadarrClient(
        "http://r", "KEY", fetcher=_fetcher({"rootfolder": [{"path": "/movies"}, {"nope": 1}]})
    )
    assert client.rootfolders() == ["/movies"]


def test_queue_records_envelope() -> None:
    client = SonarrClient(
        "http://s", "KEY", fetcher=_fetcher({"queue": {"records": [{"id": 1}, {"id": 2}]}})
    )
    assert len(client.queue()) == 2


def test_queue_plain_list() -> None:
    client = RadarrClient("http://r", "KEY", fetcher=_fetcher({"queue": [{"id": 9}]}))
    assert client.queue()[0]["id"] == 9


def test_system_status() -> None:
    client = RadarrClient(
        "http://r", "KEY", fetcher=_fetcher({"system/status": {"version": "5.0"}})
    )
    assert client.system_status()["version"] == "5.0"


def test_request_failure_raises_integration_error() -> None:
    def boom(url: str, headers: Mapping[str, str], timeout: float) -> str:
        raise OSError("connection refused")

    with pytest.raises(IntegrationError):
        RadarrClient("http://r", "KEY", fetcher=boom).movies()


def test_invalid_json_raises_integration_error() -> None:
    def bad(url: str, headers: Mapping[str, str], timeout: float) -> str:
        return "{not json"

    with pytest.raises(IntegrationError):
        RadarrClient("http://r", "KEY", fetcher=bad).movies()
