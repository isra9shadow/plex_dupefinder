"""Tests for modules.inventory.disk_inventory (combine blockdev + smart)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from adapters.blockdev import BlockDevice, MountUsage
from adapters.smart import SmartInfo
from modules.inventory import disk_inventory as di
from tests.fakes import make_context


def test_run_reports_disks_and_flags_smart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        di.blockdev,
        "lsblk_devices",
        lambda: [
            BlockDevice("sda", 16_000_000_000_000, "disk", "", "xfs", "Seagate Exos", True),
            BlockDevice("nvme0n1", 1_000_000_000_000, "disk", "", "btrfs", "Kioxia", False),
            BlockDevice("sda1", 1000, "part", "/mnt/disk1", "xfs", "", True),
        ],
    )
    monkeypatch.setattr(
        di.blockdev,
        "disk_usage",
        lambda: [
            MountUsage("/mnt/disk1", 16_000_000_000_000, 12_000_000_000_000, 4_000_000_000_000)
        ],
    )

    def smart_for(device: str) -> SmartInfo:
        if "sda" in device:
            return SmartInfo(device, "Seagate", "S1", True, 0, 0, 35, False)
        return SmartInfo(device, "Kioxia", "S2", True, 12, 0, 40, True)

    monkeypatch.setattr(di.smart, "smart_for", smart_for)

    result = di.run(make_context(tmp_path))

    assert result.actions == 1  # nvme has reallocated sectors -> warning
    data = json.loads(
        (tmp_path / "reports" / "inventory" / "disk_inventory.json").read_text(encoding="utf-8")
    )
    assert len(data["disks"]) == 2  # the partition is skipped
    assert data["mounts"][0]["percent"] == 75
    assert "⚠️ SMART" in (tmp_path / "reports" / "inventory" / "disk_inventory.md").read_text(
        encoding="utf-8"
    )
