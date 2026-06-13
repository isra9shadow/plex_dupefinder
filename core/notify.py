"""Single notifier. The Telegram transport is a stub for Sprint 1 (no real network).

A real ``TelegramTransport`` is added later; for now the default ``NullTransport``
records messages so the routing/level logic is fully testable without secrets.
"""

from __future__ import annotations

from typing import Protocol

from core.config import Config
from core.types import EventKind, NotificationEvent, NotifyLevel

_IMPORTANT = {EventKind.RUN_FAIL, EventKind.ALERT}


class Transport(Protocol):
    def send(self, text: str) -> None: ...


class NullTransport:
    """A no-network transport that records what would have been sent."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


def _should_send(level: NotifyLevel, kind: EventKind) -> bool:
    if level == NotifyLevel.NONE:
        return False
    if level == NotifyLevel.ALL:
        return True
    return kind in _IMPORTANT


def _render(event: NotificationEvent) -> str:
    head = f"[{event.kind}] {event.module} ({event.run_id}): {event.title}"
    if event.fields:
        extra = " ".join(f"{k}={v}" for k, v in sorted(event.fields.items()))
        return f"{head}\n{event.body}\n{extra}" if event.body else f"{head} | {extra}"
    return f"{head}\n{event.body}" if event.body else head


class Notifier:
    def __init__(self, config: Config, transport: Transport | None = None) -> None:
        self._cfg = config.notify
        self._transport: Transport = transport or NullTransport()

    @property
    def transport(self) -> Transport:
        return self._transport

    def send(self, event: NotificationEvent) -> None:
        if not self._cfg.enabled or not _should_send(self._cfg.level, event.kind):
            return
        self._transport.send(_render(event))
