"""Tests for modules.dashboard (aggregate reports → one page)."""

from __future__ import annotations

import json
from pathlib import Path

from modules import dashboard
from tests.fakes import make_context


def test_dashboard_aggregates(tmp_path: Path) -> None:
    base = tmp_path / "reports"
    (base / "inventory").mkdir(parents=True)
    (base / "inventory" / "docker_inventory.json").write_text(
        json.dumps([{"name": "Plex"}, {"name": "Sonarr"}]), encoding="utf-8"
    )
    (base / "inventory" / "disk_inventory.json").write_text(
        json.dumps({"disks": [{"smart_warning": True}, {"smart_warning": False}]}), encoding="utf-8"
    )
    (base / "media").mkdir()
    (base / "media" / "integrity.json").write_text(
        json.dumps([{"verdict": "ok"}, {"verdict": "bad"}]), encoding="utf-8"
    )

    result = dashboard.run(make_context(tmp_path))

    assert result.actions == 3
    md = (base / "dashboard.md").read_text(encoding="utf-8")
    assert "2 containers" in md
    assert "1 ⚠️ SMART" in md
    assert "1 flagged" in md


def test_dashboard_empty(tmp_path: Path) -> None:
    result = dashboard.run(make_context(tmp_path))
    assert result.actions == 0
    assert "no module reports" in (tmp_path / "reports" / "dashboard.md").read_text(
        encoding="utf-8"
    )
