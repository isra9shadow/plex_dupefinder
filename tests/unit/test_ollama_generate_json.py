"""Tests for OllamaClient.generate_json (structured output — injected poster)."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from core.errors import IntegrationError
from integrations.ollama import OllamaClient

_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "year": {"type": "integer"}},
    "required": ["title", "year"],
    "additionalProperties": False,
}


def _envelope(text: str) -> str:
    """Wrap a JSON payload the way Ollama's /api/generate does (response str)."""
    return json.dumps({"response": text})


def _client(response: str) -> OllamaClient:
    return OllamaClient(poster=lambda u, b, h, t: response)


def test_valid_json_is_returned() -> None:
    payload = {"title": "Dune", "year": 2021}
    out = _client(_envelope(json.dumps(payload))).generate_json("id", _SCHEMA)
    assert out == payload


def test_think_block_stripped_before_parsing() -> None:
    raw = _envelope('<think>razonando</think>\n{"title": "Dune", "year": 2021}')
    out = _client(raw).generate_json("id", _SCHEMA)
    assert out == {"title": "Dune", "year": 2021}


def test_invalid_then_valid_retries_once() -> None:
    responses = iter(
        [
            _envelope('{"title": "Dune"}'),  # missing required "year"
            _envelope('{"title": "Dune", "year": 2021}'),
        ]
    )
    captured: list[bytes] = []

    def poster(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        captured.append(body)
        return next(responses)

    out = OllamaClient(poster=poster).generate_json("id", _SCHEMA)
    assert out == {"title": "Dune", "year": 2021}
    assert len(captured) == 2  # exactly one retry
    retry = json.loads(captured[1])
    assert "corrige:" in retry["prompt"]  # repair instruction appended


def test_always_invalid_raises_after_retry() -> None:
    captured: list[bytes] = []

    def poster(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        captured.append(body)
        return _envelope('{"title": "Dune"}')  # always missing "year"

    with pytest.raises(IntegrationError):
        OllamaClient(poster=poster).generate_json("id", _SCHEMA)
    assert len(captured) == 2  # original + exactly one retry, then give up


def test_unparseable_then_valid_retries_once() -> None:
    responses = iter([_envelope("not json at all"), _envelope('{"title": "X", "year": 1}')])
    out = OllamaClient(poster=lambda u, b, h, t: next(responses)).generate_json("id", _SCHEMA)
    assert out == {"title": "X", "year": 1}


def test_request_body_forwards_schema_and_options() -> None:
    captured: list[bytes] = []

    def poster(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        captured.append(body)
        return _envelope('{"title": "X", "year": 1}')

    OllamaClient(poster=poster).generate_json("id", _SCHEMA, system="eres un experto", num_ctx=8192)
    sent = json.loads(captured[0])
    assert sent["format"] == _SCHEMA  # schema passed as the format field
    assert sent["stream"] is False
    assert sent["system"] == "eres un experto"
    assert sent["options"]["num_ctx"] == 8192
    assert sent["options"]["temperature"] == 0


def test_optional_fields_omitted_when_not_given() -> None:
    captured: list[bytes] = []

    def poster(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        captured.append(body)
        return _envelope('{"title": "X", "year": 1}')

    OllamaClient(poster=poster).generate_json("id", _SCHEMA)
    sent = json.loads(captured[0])
    assert "system" not in sent
    assert "num_ctx" not in sent["options"]


def test_bad_envelope_raises() -> None:
    with pytest.raises(IntegrationError):
        _client("not json").generate_json("id", _SCHEMA)
