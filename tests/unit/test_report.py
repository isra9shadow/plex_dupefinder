"""Tests for core.report (RunReport -> JSON)."""

from __future__ import annotations

import json
from pathlib import Path

from core.report import to_dict, write_report
from core.types import ActionRecord, FailureRecord, RunReport, SafetyMode


def _report() -> RunReport:
    return RunReport(
        run_id="r1",
        module="m",
        mode=SafetyMode.DRY_RUN,
        started=1.0,
        finished=2.0,
        actions=[ActionRecord(action="quarantine", src="/a", dest="/q", bytes=10)],
        failures=[FailureRecord(category="x", message="oops")],
        metrics={"actions": 1.0},
    )


def test_to_dict() -> None:
    data = to_dict(_report())
    assert data["run_id"] == "r1"
    actions = data["actions"]
    assert isinstance(actions, list)
    assert actions[0]["bytes"] == 10


def test_write_report(tmp_path: Path) -> None:
    out = write_report(_report(), tmp_path / "reports")
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["module"] == "m"
    assert data["mode"] == "dry_run"
    assert data["metrics"]["actions"] == 1.0
    assert len(data["actions"]) == 1
    assert data["failures"][0]["category"] == "x"
