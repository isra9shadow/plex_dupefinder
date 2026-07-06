"""Tests for the send-only Telegram client (no network — injected poster)."""

from __future__ import annotations

import dataclasses
import json
import urllib.error
from collections.abc import Mapping

import pytest
from integrations.telegram import (
    _MSG_LIMIT,
    SendResult,
    send,
    send_message,
    split_message,
)


def _recorder() -> tuple[list[dict[str, object]], object]:
    """A poster that records each decoded JSON payload and returns ``{ok:true}``."""
    seen: list[dict[str, object]] = []

    def poster(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        seen.append(json.loads(body.decode("utf-8")))
        return json.dumps({"ok": True})

    return seen, poster


# --- split_message (pure helper) ------------------------------------------------


def test_split_message_short_text_single_chunk() -> None:
    assert split_message("hello") == ["hello"]


def test_split_message_empty_returns_one_empty_chunk() -> None:
    assert split_message("") == [""]


def test_split_message_packs_on_line_boundaries() -> None:
    text = "\n".join(f"line {i:02d}" for i in range(100))
    chunks = split_message(text, limit=40)
    assert len(chunks) > 1
    assert all(len(c) <= 40 for c in chunks)
    # Lossless: re-joining reproduces the original exactly.
    assert "".join(chunks) == text


def test_split_message_hard_splits_overlong_line() -> None:
    text = "x" * 250
    chunks = split_message(text, limit=100)
    assert chunks == ["x" * 100, "x" * 100, "x" * 50]
    assert "".join(chunks) == text


def test_split_message_default_limit_under_telegram_cap() -> None:
    assert _MSG_LIMIT < 4096
    chunks = split_message("y" * (_MSG_LIMIT * 2 + 5))
    assert all(len(c) <= _MSG_LIMIT for c in chunks)


def test_split_message_rejects_nonpositive_limit() -> None:
    with pytest.raises(ValueError):
        split_message("x", limit=0)


# --- send / send_message --------------------------------------------------------


def test_send_message_returns_true_on_success() -> None:
    _, poster = _recorder()
    assert send_message("TOK", 123, "hi", poster=poster) is True


def test_send_posts_to_sendmessage_endpoint_with_json_payload() -> None:
    seen, poster = _recorder()
    captured_url: list[str] = []

    def capturing(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        captured_url.append(url)
        assert headers["content-type"] == "application/json"
        return poster(url, body, headers, timeout)

    send("TOK", 123, "hi", poster=capturing)
    assert captured_url == ["https://api.telegram.org/botTOK/sendMessage"]
    assert seen[0]["chat_id"] == 123
    assert seen[0]["text"] == "hi"
    assert seen[0]["disable_web_page_preview"] is True
    assert "message_thread_id" not in seen[0]


def test_send_includes_thread_id_when_given() -> None:
    seen, poster = _recorder()
    send("TOK", "@chan", "hi", thread_id=42, poster=poster)
    assert seen[0]["message_thread_id"] == 42


def test_send_chunks_long_text_into_multiple_posts() -> None:
    seen, poster = _recorder()
    text = "y" * (_MSG_LIMIT * 2 + 10)
    result = send("TOK", 1, text, poster=poster)
    assert result.ok is True
    assert result.total == len(seen) >= 3
    assert result.sent == result.total
    # Every posted chunk respects the cap.
    assert all(len(str(p["text"])) <= _MSG_LIMIT for p in seen)


def test_send_swallows_network_error_and_reports_false() -> None:
    def boom(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        raise urllib.error.URLError("down")

    result = send("TOK", 1, "hi", poster=boom)
    assert result.ok is False
    assert result.sent == 0
    assert result.error is not None
    assert send_message("TOK", 1, "hi", poster=boom) is False


def test_send_swallows_oserror() -> None:
    def boom(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        raise OSError("socket dead")

    assert send("TOK", 1, "hi", poster=boom).ok is False


def test_send_reports_partial_delivery_on_midstream_failure() -> None:
    calls = {"n": 0}

    def flaky(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            raise urllib.error.URLError("flaked")
        return "{}"

    text = "y" * (_MSG_LIMIT * 3)
    result = send("TOK", 1, text, poster=flaky)
    assert result.ok is False
    assert result.sent == 1
    assert result.total >= 3


def test_send_fails_closed_on_missing_token() -> None:
    seen, poster = _recorder()
    result = send("", 1, "hi", poster=poster)
    assert result.ok is False
    assert result.error == "missing token"
    assert seen == []  # nothing posted


def test_send_fails_closed_on_missing_chat_id() -> None:
    seen, poster = _recorder()
    assert send("TOK", "", "hi", poster=poster).ok is False
    assert seen == []


def test_send_result_is_frozen() -> None:
    result = SendResult(ok=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.ok = False  # type: ignore[misc]
