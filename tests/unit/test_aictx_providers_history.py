"""Tests for aictx.providers.history.HistoryContextProvider."""

from __future__ import annotations

from dataclasses import dataclass, field

from aictx.provider import ContextBlock, Tier
from aictx.providers.history import HistoryContextProvider


@dataclass(frozen=True)
class FakeIncident:
    """Structural stand-in for core.cache.Incident (keeps aictx free of core)."""

    severity: str = "high"
    title: str = "untitled"
    status: str = "open"
    first_seen: float = 1_700_000_000.0
    last_seen: float = 1_700_000_000.0
    applied: list[object] = field(default_factory=list)


def test_block_lists_open_incidents_and_applied_actions() -> None:
    incidents = [
        FakeIncident(title="OOM killer", severity="critical", status="open"),
        FakeIncident(
            title="Disk full",
            severity="high",
            status="resolved",
            applied=["expanded volume", {"action": "pruned snapshots"}],
        ),
    ]
    block = HistoryContextProvider(incidents).block()
    assert isinstance(block, ContextBlock)
    assert block.tier is Tier.MEDIUM
    assert block.name == "history"
    assert "OOM killer" in block.body
    assert "critical" in block.body
    assert "expanded volume" in block.body
    assert "pruned snapshots" in block.body
    assert "aplicad" in block.body.lower()


def test_block_returns_none_when_empty() -> None:
    assert HistoryContextProvider([]).block() is None


def test_block_returns_none_when_resolved_without_applied() -> None:
    # A resolved incident with no applied actions contributes nothing.
    incidents = [FakeIncident(title="x", status="resolved", applied=[])]
    assert HistoryContextProvider(incidents).block() is None


def test_block_renders_last_seen_timestamp() -> None:
    block = HistoryContextProvider([FakeIncident(title="x", last_seen=1_700_000_000.0)]).block()
    assert block is not None
    assert "UTC" in block.body
