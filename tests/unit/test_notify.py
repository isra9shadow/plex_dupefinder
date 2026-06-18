"""Tests for core.notify (single notifier, level routing, stub transport)."""

from __future__ import annotations

from core.config import Config, NotifyConfig
from core.notify import Notifier, NullTransport
from core.types import EventKind, NotificationEvent, NotifyLevel


def _notifier(*, enabled: bool, level: NotifyLevel) -> tuple[Notifier, NullTransport]:
    transport = NullTransport()
    return Notifier(Config(notify=NotifyConfig(enabled=enabled, level=level)), transport), transport


def _ev(
    kind: EventKind, *, body: str = "", fields: dict[str, str] | None = None
) -> NotificationEvent:
    return NotificationEvent(
        kind=kind, module="m", run_id="r", title="t", body=body, fields=fields or {}
    )


def test_disabled_sends_nothing() -> None:
    notifier, transport = _notifier(enabled=False, level=NotifyLevel.ALL)
    notifier.send(_ev(EventKind.RUN_FAIL))
    assert transport.sent == []


def test_level_all_sends_everything() -> None:
    notifier, transport = _notifier(enabled=True, level=NotifyLevel.ALL)
    notifier.send(_ev(EventKind.RUN_OK))
    notifier.send(_ev(EventKind.RUN_FAIL))
    assert len(transport.sent) == 2


def test_level_fail_sends_only_important() -> None:
    notifier, transport = _notifier(enabled=True, level=NotifyLevel.FAIL)
    notifier.send(_ev(EventKind.RUN_OK))
    notifier.send(_ev(EventKind.RUN_FAIL))
    notifier.send(_ev(EventKind.ALERT))
    assert len(transport.sent) == 2


def test_level_none_sends_nothing() -> None:
    notifier, transport = _notifier(enabled=True, level=NotifyLevel.NONE)
    notifier.send(_ev(EventKind.RUN_FAIL))
    assert transport.sent == []


def test_render_variants() -> None:
    notifier, transport = _notifier(enabled=True, level=NotifyLevel.ALL)
    notifier.send(_ev(EventKind.RUN_START, fields={"k": "v"}))  # fields, no body
    notifier.send(_ev(EventKind.ALERT, body="bd"))  # body, no fields
    notifier.send(_ev(EventKind.ALERT, body="bd", fields={"k": "v"}))  # fields + body
    notifier.send(_ev(EventKind.SUMMARY))  # neither
    assert "k=v" in transport.sent[0]
    assert "bd" in transport.sent[1]
    assert "bd" in transport.sent[2] and "k=v" in transport.sent[2]
    assert transport.sent[3].endswith(": t")


def test_default_transport_is_null() -> None:
    notifier = Notifier(Config(notify=NotifyConfig(enabled=True, level=NotifyLevel.ALL)))
    assert isinstance(notifier.transport, NullTransport)
    notifier.send(_ev(EventKind.SUMMARY))
    assert isinstance(notifier.transport, NullTransport)
