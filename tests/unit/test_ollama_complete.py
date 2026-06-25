"""Tests for OllamaClient.complete (free-form text — injected poster, no network)."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from core.errors import IntegrationError
from integrations.ollama import OllamaClient


def _envelope(text: str) -> str:
    """Wrap free-form text the way Ollama's /api/generate does (response str)."""
    return json.dumps({"response": text})


def _client(response: str) -> OllamaClient:
    return OllamaClient(poster=lambda u, b, h, t: response)


def test_complete_returns_text() -> None:
    assert _client(_envelope("Resumen claro.")).complete("hola") == "Resumen claro."


def test_complete_strips_think_block() -> None:
    raw = _envelope("<think>razonando en silencio</think>\nResumen final.")
    assert _client(raw).complete("hola") == "Resumen final."


def test_complete_does_not_request_json_mode() -> None:
    captured: list[bytes] = []

    def poster(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        captured.append(body)
        return _envelope("ok")

    OllamaClient(poster=poster).complete("hola")
    sent = json.loads(captured[0])
    assert "format" not in sent  # free-form prose, not format=json
    assert sent["stream"] is False


def test_complete_honours_explicit_timeout() -> None:
    captured: list[float] = []

    def poster(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        captured.append(timeout)
        return _envelope("ok")

    OllamaClient(poster=poster, timeout=120.0).complete("hola", timeout=5.0)
    assert captured == [5.0]  # explicit timeout overrides the client default


def test_complete_bad_envelope_raises() -> None:
    with pytest.raises(IntegrationError):
        _client("not json").complete("hola")


def test_complete_empty_after_think_raises() -> None:
    with pytest.raises(IntegrationError):
        _client(_envelope("<think>only reasoning</think>")).complete("hola")
