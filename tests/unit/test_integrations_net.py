"""Tests for integrations._net.require_http_url (A3-04 URL hardening)."""

from __future__ import annotations

import pytest
from core.errors import IntegrationError
from integrations._net import require_http_url
from integrations.radarr import RadarrClient


def test_accepts_http_and_https() -> None:
    assert require_http_url("http://host:7878") == "http://host:7878"
    assert require_http_url("https://api.themoviedb.org/3") == "https://api.themoviedb.org/3"


@pytest.mark.parametrize(
    "bad", ["file:///etc/passwd", "ftp://host", "data:text/plain,x", "radarr", "", "http://"]
)
def test_rejects_non_http_or_malformed(bad: str) -> None:
    with pytest.raises(IntegrationError):
        require_http_url(bad)


def test_client_construction_rejects_bad_url() -> None:
    with pytest.raises(IntegrationError):
        RadarrClient("file:///x", "KEY")
