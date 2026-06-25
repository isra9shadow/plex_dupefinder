"""Unit tests for InventoryContextProvider (reads cached inventory JSON)."""

from __future__ import annotations

import json
from pathlib import Path

from aictx.provider import Tier
from aictx.providers.inventory import InventoryContextProvider


def _write_inventory(reports: Path) -> None:
    inv = reports / "inventory"
    inv.mkdir(parents=True, exist_ok=True)
    docker = [
        {"name": "plex", "state": "running"},
        {"name": "sonarr", "state": "exited"},
        {"name": "radarr", "state": "running"},
    ]
    disk = {
        "disks": [
            {"name": "sda", "model": "WD", "size_tb": 8.0, "type": "HDD", "smart_warning": False},
            {"name": "sdb", "model": "WD", "size_tb": 8.0, "type": "HDD", "smart_warning": True},
        ],
        "mounts": [
            {"target": "/mnt/cache", "percent": 73},
            {"target": "/mnt/user", "percent": 51},
            {"target": "/boot", "percent": 12},
        ],
    }
    (inv / "docker_inventory.json").write_text(json.dumps(docker), encoding="utf-8")
    (inv / "disk_inventory.json").write_text(json.dumps(disk), encoding="utf-8")


def test_block_summarises_counts_anomalies_and_mounts(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    _write_inventory(reports)

    block = InventoryContextProvider(reports).block()

    assert block is not None
    assert block.name == "inventory"
    assert block.tier == Tier.HIGH
    assert block.stable is True
    # container count + not-running anomaly named
    assert "3" in block.body
    assert "sonarr" in block.body
    # SMART warning surfaced (count + disk name)
    assert "SMART warning" in block.body
    assert "sdb" in block.body
    # key mount percentage present
    assert "/mnt/cache" in block.body
    assert "73" in block.body


def test_block_none_when_no_files(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    (reports / "inventory").mkdir(parents=True, exist_ok=True)
    assert InventoryContextProvider(reports).block() is None


def test_block_none_when_dir_missing(tmp_path: Path) -> None:
    assert InventoryContextProvider(tmp_path / "nope").block() is None
