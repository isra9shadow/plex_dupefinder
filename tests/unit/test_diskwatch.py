"""Tests for modules.ops.diskwatch (read-only silent-disk-error detector)."""

from __future__ import annotations

import json
from pathlib import Path

from adapters.smart import SmartInfo
from modules.ops import diskwatch
from tests.fakes import make_context


def _read_plan(tmp_path: Path) -> dict[str, object]:
    return json.loads(
        (tmp_path / "reports" / "diskwatch" / "plan.json").read_text(encoding="utf-8")
    )


def _read_summary(tmp_path: Path) -> str:
    return (tmp_path / "reports" / "diskwatch" / "summary.md").read_text(encoding="utf-8")


def _info(
    device: str,
    *,
    health_passed: bool = True,
    reallocated: int = 0,
    pending: int = 0,
    temperature: int = 35,
    model: str = "TestDisk",
    serial: str = "SER",
) -> SmartInfo:
    warning = (not health_passed) or reallocated > 0 or pending > 0
    return SmartInfo(
        device=device,
        model=model,
        serial=serial,
        health_passed=health_passed,
        reallocated=reallocated,
        pending=pending,
        temperature=temperature,
        warning=warning,
    )


# --- pure evaluation -----------------------------------------------------------


def test_evaluate_healthy_disk_has_no_reasons() -> None:
    status = diskwatch.evaluate(_info("/dev/sda"), temp_warn=50)
    assert not status.warning
    assert status.reasons == []


def test_evaluate_flags_failed_health() -> None:
    status = diskwatch.evaluate(_info("/dev/sda", health_passed=False), temp_warn=50)
    assert status.warning
    assert any("health" in r for r in status.reasons)


def test_evaluate_flags_reallocated_and_pending() -> None:
    status = diskwatch.evaluate(_info("/dev/sdb", reallocated=12, pending=3), temp_warn=50)
    assert status.warning
    assert any("Reallocated_Sector_Ct = 12" in r for r in status.reasons)
    assert any("Current_Pending_Sector = 3" in r for r in status.reasons)


def test_evaluate_flags_high_temperature() -> None:
    status = diskwatch.evaluate(_info("/dev/sdc", temperature=55), temp_warn=50)
    assert status.warning
    assert any("temperature" in r for r in status.reasons)


def test_evaluate_temperature_below_threshold_is_ok() -> None:
    status = diskwatch.evaluate(_info("/dev/sdc", temperature=49), temp_warn=50)
    assert not status.warning


def test_evaluate_collects_multiple_reasons() -> None:
    status = diskwatch.evaluate(
        _info("/dev/sdd", health_passed=False, reallocated=1, pending=2, temperature=60),
        temp_warn=50,
    )
    assert len(status.reasons) == 4


# --- run integration -----------------------------------------------------------


def test_run_explicit_disks_writes_reports(tmp_path: Path) -> None:
    data = {
        "/dev/sda": _info("/dev/sda", reallocated=20),
        "/dev/sdb": _info("/dev/sdb"),
    }

    def reader(device: str) -> SmartInfo | None:
        return data.get(device)

    ctx = make_context(tmp_path, integrations={"diskwatch": {"disks": ["/dev/sda", "/dev/sdb"]}})
    result = diskwatch.scan(ctx, smart_reader=reader)

    assert result.ok
    assert result.actions == 0  # read-only
    assert result.metrics["disks_with_warnings"] == 1.0

    plan = _read_plan(tmp_path)
    assert plan["disks_scanned"] == 2
    assert plan["disks_with_warnings"] == 1
    by_device = {d["device"]: d for d in plan["disks"]}
    assert by_device["/dev/sda"]["warning"] is True
    assert by_device["/dev/sdb"]["warning"] is False
    summary = _read_summary(tmp_path)
    assert "/dev/sda" in summary
    assert "Reallocated_Sector_Ct = 20" in summary


def test_run_discovers_disks_when_none_configured(tmp_path: Path) -> None:
    def lister() -> list[str]:
        return ["/dev/sdx"]

    def reader(device: str) -> SmartInfo | None:
        return _info(device, pending=5)

    ctx = make_context(tmp_path)
    result = diskwatch.scan(ctx, smart_reader=reader, device_lister=lister)

    assert result.metrics["disks_with_warnings"] == 1.0
    plan = _read_plan(tmp_path)
    assert [d["device"] for d in plan["disks"]] == ["/dev/sdx"]


def test_run_all_healthy_notes_clean(tmp_path: Path) -> None:
    def reader(device: str) -> SmartInfo | None:
        return _info(device)

    ctx = make_context(tmp_path, integrations={"diskwatch": {"disks": ["/dev/sda"]}})
    result = diskwatch.scan(ctx, smart_reader=reader)

    assert result.ok
    assert result.metrics["disks_with_warnings"] == 0.0
    assert "All scanned disks healthy" in _read_summary(tmp_path)


def test_run_missing_smart_data_is_recorded(tmp_path: Path) -> None:
    def reader(device: str) -> SmartInfo | None:
        return None

    ctx = make_context(tmp_path, integrations={"diskwatch": {"disks": ["/dev/sda"]}})
    result = diskwatch.scan(ctx, smart_reader=reader)

    assert not result.ok
    assert result.failures[0].category == "integration"
    assert "no SMART data" in result.failures[0].message


def test_run_one_disk_read_failure_does_not_abort(tmp_path: Path) -> None:
    def reader(device: str) -> SmartInfo | None:
        if device == "/dev/bad":
            raise RuntimeError("smartctl exploded")
        return _info(device, reallocated=7)

    ctx = make_context(tmp_path, integrations={"diskwatch": {"disks": ["/dev/bad", "/dev/good"]}})
    result = diskwatch.scan(ctx, smart_reader=reader)

    # Bad disk recorded as a failure, good disk still evaluated and reported.
    assert any("/dev/bad" in f.message for f in result.failures)
    plan = _read_plan(tmp_path)
    devices = {d["device"] for d in plan["disks"]}
    assert devices == {"/dev/good"}
    assert plan["disks_with_warnings"] == 1


def test_run_no_disks_records_failure(tmp_path: Path) -> None:
    def lister() -> list[str]:
        return []

    def reader(device: str) -> SmartInfo | None:  # pragma: no cover - never called
        raise AssertionError("reader must not be called when there are no disks")

    ctx = make_context(tmp_path)
    result = diskwatch.scan(ctx, smart_reader=reader, device_lister=lister)

    assert not result.ok
    assert result.metrics["disks_with_warnings"] == 0.0
    assert "No disks to scan" in _read_summary(tmp_path)


def test_run_custom_temp_warn_threshold(tmp_path: Path) -> None:
    def reader(device: str) -> SmartInfo | None:
        return _info(device, temperature=45)

    ctx = make_context(
        tmp_path, integrations={"diskwatch": {"disks": ["/dev/sda"], "temp_warn": 40}}
    )
    result = diskwatch.scan(ctx, smart_reader=reader)

    assert result.metrics["disks_with_warnings"] == 1.0
    assert "temperature 45" in _read_summary(tmp_path)
