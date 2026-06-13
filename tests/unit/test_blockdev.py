"""Tests for adapters.blockdev (lsblk + df parsing, injected runner)."""

from __future__ import annotations

import json
from collections.abc import Sequence

from adapters import blockdev
from adapters.command import CommandResult


def test_lsblk_parses_devices() -> None:
    payload = json.dumps(
        {
            "blockdevices": [
                {
                    "name": "sda",
                    "size": 16000000000000,
                    "type": "disk",
                    "mountpoint": None,
                    "fstype": None,
                    "model": "Seagate Exos ",
                    "rota": True,
                },
                {
                    "name": "nvme0n1",
                    "size": 1000000000000,
                    "type": "disk",
                    "mountpoint": None,
                    "fstype": "btrfs",
                    "model": "Kioxia",
                    "rota": False,
                },
            ]
        }
    )

    def runner(argv: Sequence[str]) -> CommandResult:
        return CommandResult(tuple(argv), 0, payload, "")

    devices = blockdev.lsblk_devices(runner=runner)
    assert devices[0].name == "sda"
    assert devices[0].rotational
    assert devices[0].model == "Seagate Exos"
    assert not devices[1].rotational


def test_lsblk_empty_on_failure() -> None:
    def runner(argv: Sequence[str]) -> CommandResult:
        return CommandResult(tuple(argv), 1, "", "boom")

    assert blockdev.lsblk_devices(runner=runner) == []


def test_disk_usage_parses_and_percent() -> None:
    out = (
        "Mounted on 1B-blocks Used Available\n"
        "/mnt/disk1 16000000000000 12000000000000 4000000000000\n"
        "/boot 100 50 50\n"
    )

    def runner(argv: Sequence[str]) -> CommandResult:
        return CommandResult(tuple(argv), 0, out, "")

    usages = blockdev.disk_usage(runner=runner)
    assert usages[0].target == "/mnt/disk1"
    assert usages[0].percent == 75
    assert usages[1].percent == 50
