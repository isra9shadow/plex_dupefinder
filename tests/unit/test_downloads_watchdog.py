"""Tests for modules.downloads.watchdog (report-only stalled detection)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from modules.downloads import watchdog
from tests.fakes import make_context

_DAY2 = 48 * 3600


def test_is_stalled_by_state() -> None:
    assert watchdog.is_stalled({"state": "stalledDL"}, _DAY2)


def test_is_stalled_by_inactivity() -> None:
    torrent = {"state": "downloading", "time_active": 200000, "dlspeed": 0, "upspeed": 0}
    assert watchdog.is_stalled(torrent, _DAY2)


def test_not_stalled_when_active() -> None:
    torrent = {"state": "downloading", "time_active": 200000, "dlspeed": 50, "upspeed": 0}
    assert not watchdog.is_stalled(torrent, _DAY2)


def test_not_stalled_when_recent() -> None:
    torrent = {"state": "downloading", "time_active": 10, "dlspeed": 0, "upspeed": 0}
    assert not watchdog.is_stalled(torrent, _DAY2)


def test_category_of() -> None:
    assert watchdog.category_of("tv-sonarr") == "sonarr"
    assert watchdog.category_of("radarr") == "radarr"
    assert watchdog.category_of("misc") == "unknown"


class _StubQbit:
    def torrents(self) -> list[dict[str, object]]:
        return [
            {"name": "Stuck", "hash": "h1", "state": "stalledDL", "category": "radarr"},
            {
                "name": "Fine",
                "state": "downloading",
                "time_active": 10,
                "dlspeed": 5,
                "upspeed": 0,
                "category": "sonarr",
            },
        ]


def test_run_reports_stalled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(watchdog, "_qbit", lambda ctx: _StubQbit())
    result = watchdog.run(make_context(tmp_path))
    assert result.actions == 1
    data = json.loads(
        (tmp_path / "reports" / "downloads" / "stalled.json").read_text(encoding="utf-8")
    )
    assert data[0]["name"] == "Stuck"
    assert data[0]["category"] == "radarr"


def test_run_fails_without_config(tmp_path: Path) -> None:
    result = watchdog.run(make_context(tmp_path))
    assert not result.ok
    assert result.failures[0].category == "config"
