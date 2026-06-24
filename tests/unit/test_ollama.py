"""Tests for the Ollama local-LLM client (no network — injected poster)."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from core.errors import IntegrationError
from integrations.ollama import OllamaClient


def _envelope(suggestions: list[dict[str, object]]) -> str:
    """Wrap a suggestions array the way Ollama's /api/generate does (response str)."""
    return json.dumps({"response": json.dumps(suggestions)})


def _client(response: str) -> OllamaClient:
    return OllamaClient(poster=lambda u, b, h, t: response)


def test_identify_parses_response_array() -> None:
    payload = [{"filename": "x.mkv", "type": "movie", "title": "X", "confidence": 95}]
    assert _client(_envelope(payload)).identify(["x.mkv"]) == payload


def test_identify_accepts_object_wrapped_array() -> None:
    # format=json sometimes returns {"results": [...]} instead of a bare array.
    inner = json.dumps({"results": [{"filename": "x.mkv", "type": "movie"}]})
    out = _client(json.dumps({"response": inner})).identify(["x.mkv"])
    assert out == [{"filename": "x.mkv", "type": "movie"}]


def test_identify_batches_requests() -> None:
    captured: list[bytes] = []

    def poster(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        captured.append(body)
        return _envelope([{"filename": "n"}])

    OllamaClient(batch_size=1, poster=poster).identify(["a", "b", "c"])
    assert len(captured) == 3


def test_bad_envelope_raises() -> None:
    with pytest.raises(IntegrationError):
        _client("not json").identify(["a.mkv"])


def test_empty_response_raises() -> None:
    with pytest.raises(IntegrationError):
        _client(json.dumps({"response": ""})).identify(["a.mkv"])


def test_identify_skips_failed_batch_when_errors_collected() -> None:
    responses = iter([_envelope([{"filename": "a"}]), "BADJSON", _envelope([{"filename": "c"}])])
    client = OllamaClient(batch_size=1, poster=lambda u, b, h, t: next(responses))
    errors: list[IntegrationError] = []
    out = client.identify(["a", "b", "c"], errors=errors)
    assert [r["filename"] for r in out] == ["a", "c"]
    assert len(errors) == 1
