"""Tests for integrations.discord (send-only webhook, offline)."""

from __future__ import annotations

from collections.abc import Mapping

from integrations import discord


def test_split_message_packs_on_line_boundaries() -> None:
    text = "\n".join(f"line {i:02d}" for i in range(100))
    chunks = discord.split_message(text, limit=40)
    assert len(chunks) > 1
    assert all(len(c) <= 40 for c in chunks)
    assert "".join(chunks) == text  # lossless


def test_split_message_hard_splits_overlong_line() -> None:
    chunks = discord.split_message("x" * 50, limit=20)
    assert chunks == ["x" * 20, "x" * 20, "x" * 10]


def test_send_posts_each_chunk() -> None:
    posted: list[str] = []

    def poster(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        posted.append(url)
        return ""

    res = discord.send("https://hook", "a\nb", poster=poster)
    assert res.ok and res.sent == res.total == 1
    assert posted == ["https://hook"]


def test_send_missing_url() -> None:
    assert discord.send("", "hi").ok is False


def test_send_swallows_network_error() -> None:
    def boom(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> str:
        raise OSError("connection refused")

    res = discord.send("https://hook", "hi", poster=boom)
    assert res.ok is False and "connection refused" in (res.error or "")


def test_send_webhook_returns_bool() -> None:
    assert discord.send_webhook("https://hook", "hi", poster=lambda u, b, h, t: "") is True
    assert discord.send_webhook("", "hi") is False
