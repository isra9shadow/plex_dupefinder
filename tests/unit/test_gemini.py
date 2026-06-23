"""Tests for the Gemini identification client (no network — injected poster)."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from core.errors import IntegrationError
from integrations.gemini import GeminiClient, build_prompt


def _envelope(suggestions: list[dict[str, object]]) -> str:
    """Wrap a suggestions array the way Gemini's generateContent does."""
    return json.dumps({"candidates": [{"content": {"parts": [{"text": json.dumps(suggestions)}]}}]})


def _client(response: str, *, captured: list[bytes] | None = None) -> GeminiClient:
    def poster(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        if captured is not None:
            captured.append(body)
        return response

    return GeminiClient("KEY", poster=poster)


def test_build_prompt_numbers_filenames() -> None:
    prompt = build_prompt(["a.mkv", "b.mkv"])
    assert "1. a.mkv" in prompt
    assert "2. b.mkv" in prompt


def test_empty_api_key_rejected() -> None:
    with pytest.raises(IntegrationError):
        GeminiClient("")


def test_identify_parses_suggestions() -> None:
    payload = [{"filename": "x.mkv", "type": "movie", "title": "X", "confidence": 95}]
    out = _client(_envelope(payload)).identify(["x.mkv"])
    assert out == payload


def test_identify_batches_requests() -> None:
    captured: list[bytes] = []
    client = GeminiClient(
        "KEY",
        batch_size=1,
        poster=lambda u, b, h, t: captured.append(b) or _envelope([{"filename": "n"}]),
    )
    client.identify(["a.mkv", "b.mkv", "c.mkv"])
    assert len(captured) == 3  # one request per item at batch_size=1


def test_bad_envelope_raises() -> None:
    with pytest.raises(IntegrationError):
        _client("not json").identify(["a.mkv"])


def test_no_candidates_raises() -> None:
    with pytest.raises(IntegrationError):
        _client(json.dumps({"candidates": []})).identify(["a.mkv"])


def test_suggestion_payload_not_array_raises() -> None:
    bad = json.dumps({"candidates": [{"content": {"parts": [{"text": '{"not":"array"}'}]}}]})
    with pytest.raises(IntegrationError):
        _client(bad).identify(["a.mkv"])
