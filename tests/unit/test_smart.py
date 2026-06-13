"""Tests for adapters.smart (smartctl JSON parsing, injected runner)."""

from __future__ import annotations

import json
from collections.abc import Sequence

from adapters import smart
from adapters.command import CommandResult


def _runner(payload: str, returncode: int = 0):
    def runner(argv: Sequence[str]) -> CommandResult:
        return CommandResult(tuple(argv), returncode, payload, "")

    return runner


def test_smart_flags_reallocated() -> None:
    payload = json.dumps(
        {
            "model_name": "Disk",
            "serial_number": "S1",
            "smart_status": {"passed": True},
            "ata_smart_attributes": {
                "table": [
                    {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 12}},
                    {"id": 197, "name": "Current_Pending_Sector", "raw": {"value": 0}},
                ]
            },
            "temperature": {"current": 40},
        }
    )
    info = smart.smart_for("/dev/sda", runner=_runner(payload, returncode=4))
    assert info is not None
    assert info.reallocated == 12
    assert info.temperature == 40
    assert info.warning


def test_smart_healthy() -> None:
    payload = json.dumps(
        {
            "model_name": "D",
            "serial_number": "S",
            "smart_status": {"passed": True},
            "ata_smart_attributes": {"table": []},
            "temperature": {"current": 30},
        }
    )
    info = smart.smart_for("/dev/sda", runner=_runner(payload))
    assert info is not None
    assert not info.warning


def test_smart_failed_health() -> None:
    payload = json.dumps({"model_name": "D", "smart_status": {"passed": False}})
    info = smart.smart_for("/dev/sda", runner=_runner(payload))
    assert info is not None
    assert info.warning


def test_smart_no_output_returns_none() -> None:
    assert smart.smart_for("/dev/x", runner=_runner("", returncode=2)) is None
