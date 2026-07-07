"""Send-only Discord webhook client (stdlib HTTP, injectable poster).

The second alert channel next to Telegram: POST a message to a Discord *webhook*
URL (Server Settings → Integrations → Webhooks). Send-only (no gateway/bot) — the
same "alerts" role sentinel/notifypush already fill, now on Discord too.

Mirrors :mod:`integrations.telegram`: pure ``split_message`` (Discord's hard cap is
2000 chars; we stay under with a margin), an injected ``poster`` so tests never hit
the network, and a typed :class:`SendResult` that never raises on a transport error.
The webhook URL is a secret — the caller resolves it via ``core/secrets`` and passes
it in (this layer stays config-agnostic).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

# (url, body, headers, timeout) -> response text. Injected so tests stay offline.
JsonPoster = Callable[[str, bytes, Mapping[str, str], float], str]

_MSG_LIMIT = 1900  # Discord's hard cap is 2000; leave a margin.


@dataclass(frozen=True)
class SendResult:
    """Outcome of a (possibly multi-chunk) webhook send. Never carries an exception."""

    ok: bool
    sent: int = 0
    total: int = 0
    error: str | None = None
    chunks: tuple[str, ...] = field(default_factory=tuple)


def split_message(text: str, limit: int = _MSG_LIMIT) -> list[str]:
    """Split text into Discord-sized chunks on line boundaries (lossless).

    Identical strategy to ``telegram.split_message``: pack whole lines until the
    next would overflow ``limit``; a single over-long line is hard-split. Always
    returns at least one (possibly empty) chunk.
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
        while len(line) > limit:
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


def send(
    webhook_url: str,
    content: str,
    *,
    timeout: float = 15,
    poster: JsonPoster = _urllib_post,
) -> SendResult:
    """Post ``content`` (chunked) to the webhook; typed result, never raises."""
    if not webhook_url:
        return SendResult(ok=False, error="missing webhook_url")
    chunks = split_message(content)
    headers = {"content-type": "application/json"}
    sent = 0
    for chunk in chunks:
        body = json.dumps({"content": chunk}).encode("utf-8")
        try:
            poster(webhook_url, body, headers, timeout)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return SendResult(
                ok=False, sent=sent, total=len(chunks), error=str(exc), chunks=tuple(chunks)
            )
        sent += 1
    return SendResult(ok=True, sent=sent, total=len(chunks), chunks=tuple(chunks))


def send_webhook(
    webhook_url: str, content: str, *, timeout: float = 15, poster: JsonPoster = _urllib_post
) -> bool:
    """Send ``content`` to the webhook; return True iff every chunk was delivered."""
    return send(webhook_url, content, timeout=timeout, poster=poster).ok
