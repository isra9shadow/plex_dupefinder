"""Tests for the qBittorrent client (injected transport, no network)."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from core.errors import IntegrationError
from integrations.qbittorrent import QbitClient


def _transport(*, login_ok: bool = True, torrents: list[dict[str, object]] | None = None):
    def transport(method: str, path: str, data: Mapping[str, str] | None) -> str:
        if path.endswith("/auth/login"):
            assert data is not None
            assert data["username"] and data["password"]
            return "Ok." if login_ok else "Fails."
        if path.endswith("/torrents/info"):
            return json.dumps(torrents if torrents is not None else [])
        raise OSError(f"no route for {path}")

    return transport


def test_torrents_after_login() -> None:
    client = QbitClient(
        "http://q",
        "u",
        "p",
        transport=_transport(torrents=[{"name": "X", "state": "downloading"}]),
    )
    result = client.torrents()
    assert result[0]["name"] == "X"


def test_login_failure_raises() -> None:
    client = QbitClient("http://q", "u", "p", transport=_transport(login_ok=False))
    with pytest.raises(IntegrationError):
        client.torrents()


def test_invalid_json_raises() -> None:
    def transport(method: str, path: str, data: Mapping[str, str] | None) -> str:
        return "Ok." if path.endswith("/auth/login") else "{not json"

    with pytest.raises(IntegrationError):
        QbitClient("http://q", "u", "p", transport=transport).torrents()
