"""Tests for the Gemini identification client (no network — injected poster)."""

from __future__ import annotations

import json
import urllib.error
from collections.abc import Mapping

import pytest
from core.errors import IntegrationError
from integrations.gemini import GeminiClient, build_prompt


def _http_error(code: int) -> urllib.error.HTTPError:
    """Build an HTTPError the way urllib raises one for a non-2xx response."""
    return urllib.error.HTTPError("https://x", code, f"err {code}", {}, None)  # type: ignore[arg-type]


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


def test_transient_503_then_success_retries() -> None:
    payload = [{"filename": "x.mkv", "type": "movie", "title": "X", "confidence": 90}]
    calls = 0
    slept: list[float] = []

    def poster(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _http_error(503)
        return _envelope(payload)

    client = GeminiClient("KEY", poster=poster, backoff_base=0.01, sleeper=slept.append)
    out = client.identify(["x.mkv"])
    assert out == payload
    assert calls == 2  # one failure, one success
    assert slept == [0.01]  # backed off once before retrying


def test_persistent_503_exhausts_retries() -> None:
    calls = 0
    slept: list[float] = []

    def poster(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        nonlocal calls
        calls += 1
        raise _http_error(503)

    client = GeminiClient(
        "KEY", poster=poster, max_retries=2, backoff_base=0.01, sleeper=slept.append
    )
    with pytest.raises(IntegrationError):
        client.identify(["x.mkv"])
    assert calls == 3  # initial + 2 retries
    assert len(slept) == 2  # backed off before each retry


def test_429_message_mentions_quota_and_429() -> None:
    def poster(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        raise _http_error(429)

    client = GeminiClient("KEY", poster=poster, max_retries=0)
    with pytest.raises(IntegrationError) as excinfo:
        client.identify(["x.mkv"])
    message = str(excinfo.value).lower()
    assert "429" in message
    assert "quota" in message or "rate-limit" in message


@pytest.mark.parametrize("code", [400, 403])
def test_non_retryable_http_error_raises_immediately(code: int) -> None:
    calls = 0
    slept: list[float] = []

    def poster(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        nonlocal calls
        calls += 1
        raise _http_error(code)

    client = GeminiClient("KEY", poster=poster, max_retries=3, sleeper=slept.append)
    with pytest.raises(IntegrationError):
        client.identify(["x.mkv"])
    assert calls == 1  # no retry on non-transient status
    assert slept == []  # never slept


def test_identify_skips_failed_batch_when_errors_collected() -> None:
    responses = iter([_envelope([{"filename": "a"}]), "BADJSON", _envelope([{"filename": "c"}])])
    client = GeminiClient("KEY", batch_size=1, poster=lambda u, b, h, t: next(responses))
    errors: list[IntegrationError] = []
    out = client.identify(["a", "b", "c"], errors=errors)
    assert [r["filename"] for r in out] == ["a", "c"]  # middle batch skipped, not aborted
    assert len(errors) == 1


def test_identify_throttles_between_batches() -> None:
    slept: list[float] = []
    client = GeminiClient(
        "KEY",
        batch_size=1,
        request_delay=2.0,
        sleeper=slept.append,
        poster=lambda u, b, h, t: _envelope([{"filename": "n"}]),
    )
    client.identify(["a", "b", "c"])
    assert slept == [2.0, 2.0]  # delay before the 2nd and 3rd batch, not the first
