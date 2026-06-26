"""Reusable send-only Telegram client (stdlib HTTP, injectable poster).

Mirrors the proven approach in ``bot.py`` (``_tg`` / ``split_message``) and
``sentinel.py`` (``send_telegram``): POST to ``api.telegram.org`` ``sendMessage``
with a JSON body, chunking long text under Telegram's 4096-char hard cap.

This is the ONE shared send-only Telegram transport: any module/script that
wants to push a message (alerts, nightly digests, ``core/notify``) should call
``send_message`` here instead of re-implementing the HTTP call.

Design:
  * Send-only — no long-polling, no callbacks (that stays in ``bot.py``).
  * Pure helpers (``split_message``) are deterministic and unit-tested.
  * The HTTP call is injected via a ``JsonPoster`` so tests never touch the
    network; production uses the stdlib ``urllib`` poster (S310 boundary).
  * Never raises on a network error: returns a typed :class:`SendResult`
    (and ``send_message`` returns ``bool``) so callers decide what to do.
  * The token/chat id are NOT read here — the caller resolves them via
    ``core/secrets`` and passes them in (keeps this layer config-agnostic).

NOTE for the Tech Lead: this module uses ``urllib`` to talk to the single
fixed host ``api.telegram.org``; add ``"integrations/telegram.py" = ["S310"]``
to ``[tool.ruff.lint.per-file-ignores]`` in ``pyproject.toml`` (exactly like
the other integrations and ``sentinel.py``).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

# (url, body, headers, timeout) -> response text. Injected so tests stay offline.
JsonPoster = Callable[[str, bytes, Mapping[str, str], float], str]

_API = "https://api.telegram.org/bot{token}/{method}"

# Telegram's hard message cap is 4096 chars; stay under it with a safety margin
# (mirrors bot.py's _MSG_LIMIT = 3500).
_MSG_LIMIT = 3500


@dataclass(frozen=True)
class SendResult:
    """Outcome of a (possibly multi-chunk) send. Never carries an exception.

    ``ok`` is True only if EVERY chunk was delivered. ``sent`` / ``total`` let
    a caller see partial delivery; ``error`` is a short human string when a
    chunk failed (the network exception is swallowed, not propagated).
    """

    ok: bool
    sent: int = 0
    total: int = 0
    error: str | None = None
    chunks: tuple[str, ...] = field(default_factory=tuple)


def split_message(text: str, limit: int = _MSG_LIMIT) -> list[str]:
    """Split text into Telegram-sized chunks on line boundaries (lossless).

    Identical strategy to ``bot.split_message``: pack whole lines into a chunk
    until the next line would overflow ``limit``; a single over-long line is
    hard-split. Always returns at least one (possibly empty) chunk.
    """
    if limit <= 0:
        raise ValueError(f"limit must be positive, got {limit}")
    chunks: list[str] = []
    buf = ""
    for raw_line in (text or "").splitlines(keepends=True):
        line = raw_line
        if buf and len(buf) + len(line) > limit:
            chunks.append(buf)
            buf = ""
        while len(line) > limit:  # a single over-long line: hard-split it
            chunks.append(line[:limit])
            line = line[limit:]
        buf += line
    if buf:
        chunks.append(buf)
    return chunks or [""]


def _urllib_post(
    url: str, body: bytes, headers: Mapping[str, str], timeout: float
) -> str:  # pragma: no cover - real IO
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return str(response.read().decode("utf-8"))


def _build_payload(chat_id: int | str, text: str, *, thread_id: int | None) -> dict[str, object]:
    """Build one ``sendMessage`` JSON payload (pure; unit-tested)."""
    payload: dict[str, object] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    return payload


def send_message(
    token: str,
    chat_id: int | str,
    text: str,
    *,
    timeout: float = 15,
    thread_id: int | None = None,
    poster: JsonPoster = _urllib_post,
) -> bool:
    """Send ``text`` to ``chat_id`` (chunked); return True iff all chunks sent.

    Best-effort: any network/parse failure is swallowed and reported as False
    (the caller decides whether to retry or log). For richer detail use
    :func:`send` which returns a :class:`SendResult`.
    """
    return send(
        token,
        chat_id,
        text,
        timeout=timeout,
        thread_id=thread_id,
        poster=poster,
    ).ok


def send(
    token: str,
    chat_id: int | str,
    text: str,
    *,
    timeout: float = 15,
    thread_id: int | None = None,
    poster: JsonPoster = _urllib_post,
) -> SendResult:
    """Send ``text`` (chunked) and return a typed :class:`SendResult`.

    Stops at the first failed chunk (so ``sent`` reflects partial delivery) and
    never raises on a transport error — the exception is captured in
    ``error``. A missing token/chat id fails closed with ``ok=False``.
    """
    if not token:
        return SendResult(ok=False, error="missing token")
    if chat_id == "" or chat_id is None:
        return SendResult(ok=False, error="missing chat_id")

    chunks = split_message(text)
    url = _API.format(token=token, method="sendMessage")
    headers = {"content-type": "application/json"}
    sent = 0
    for chunk in chunks:
        payload = _build_payload(chat_id, chunk, thread_id=thread_id)
        body = json.dumps(payload).encode("utf-8")
        try:
            poster(url, body, headers, timeout)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return SendResult(
                ok=False,
                sent=sent,
                total=len(chunks),
                error=str(exc),
                chunks=tuple(chunks),
            )
        sent += 1
    return SendResult(ok=True, sent=sent, total=len(chunks), chunks=tuple(chunks))
