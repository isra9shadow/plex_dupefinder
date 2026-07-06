"""Tests for modules.ops.shadowcheck (parity-window drift monitor, read-only)."""

from __future__ import annotations

import json
from pathlib import Path

from modules.ops import shadowcheck
from modules.ops.shadowcheck import RunShadow
from tests.fakes import make_context


def _write_report(dir_: Path, name: str, shadow: dict | None, mtime: float) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    data: dict = {"phases": {}}
    if shadow is not None:
        data["phases"]["shadow_compare"] = shadow
    p = dir_ / name
    p.write_text(json.dumps(data), encoding="utf-8")
    import os

    os.utime(p, (mtime, mtime))


def _read_plan(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / "reports" / "shadowcheck" / "plan.json").read_text(encoding="utf-8")
    )


# --- extract / summarize (pure) ------------------------------------------------


def test_extract_shadow_reads_block() -> None:
    data = {"phases": {"shadow_compare": {"checked": 5, "drift": 0, "drift_groups": []}}}
    block = shadowcheck.extract_shadow("r.json", data)
    assert block == RunShadow("r.json", 5, 0, [])


def test_extract_shadow_absent() -> None:
    assert shadowcheck.extract_shadow("r.json", {"phases": {}}) is None
    assert shadowcheck.extract_shadow("r.json", "nope") is None


def test_summarize_clean() -> None:
    blocks = [RunShadow("a", 10, 0, []), RunShadow("b", 8, 0, [])]
    s = shadowcheck.summarize(blocks)
    assert s.verdict == "clean"
    assert s.total_checked == 18 and s.total_drift == 0


def test_summarize_drift_surfaces_run() -> None:
    blocks = [
        RunShadow("new", 10, 0, []),
        RunShadow("old", 8, 1, [{"title": "X", "diffs": ["keeper_id: native=1 legacy=2"]}]),
    ]
    s = shadowcheck.summarize(blocks)
    assert s.verdict == "drift"
    assert s.total_drift == 1
    assert s.first_drift is not None and s.first_drift.report == "old"


def test_summarize_no_data() -> None:
    assert shadowcheck.summarize([]).verdict == "no_data"


# --- run -----------------------------------------------------------------------


def test_run_clean_over_window(tmp_path: Path) -> None:
    reports = tmp_path / "REPORTS"
    _write_report(reports, "dupefinder_report_1.json", {"checked": 5, "drift": 0}, 1000)
    _write_report(reports, "dupefinder_report_2.json", {"checked": 7, "drift": 0}, 2000)
    ctx = make_context(tmp_path, integrations={"shadowcheck": {"reports": str(reports)}})
    result = shadowcheck.run(ctx)
    assert result.ok
    plan = _read_plan(tmp_path)
    assert plan["verdict"] == "clean"
    assert plan["total_checked"] == 12


def test_run_drift_is_a_failure(tmp_path: Path) -> None:
    reports = tmp_path / "REPORTS"
    _write_report(
        reports,
        "dupefinder_report_1.json",
        {"checked": 5, "drift": 1, "drift_groups": [{"title": "X", "diffs": ["skip: a b"]}]},
        1000,
    )
    ctx = make_context(tmp_path, integrations={"shadowcheck": {"reports": str(reports)}})
    result = shadowcheck.run(ctx)
    assert not result.ok
    assert any("drift" in f.message for f in result.failures)
    assert _read_plan(tmp_path)["verdict"] == "drift"


def test_run_falls_back_to_analyst_reports_dir(tmp_path: Path) -> None:
    reports = tmp_path / "DF"
    _write_report(reports, "dupefinder_report_1.json", {"checked": 3, "drift": 0}, 1000)
    ctx = make_context(tmp_path, integrations={"analyst": {"dupefinder_reports": str(reports)}})
    result = shadowcheck.run(ctx)
    assert _read_plan(tmp_path)["verdict"] == "clean"
    assert result.metrics["total_checked"] == 3.0


def test_run_no_data_note(tmp_path: Path) -> None:
    reports = tmp_path / "REPORTS"
    _write_report(reports, "dupefinder_report_1.json", None, 1000)  # no shadow block
    ctx = make_context(tmp_path, integrations={"shadowcheck": {"reports": str(reports)}})
    result = shadowcheck.run(ctx)
    assert result.ok
    assert _read_plan(tmp_path)["verdict"] == "no_data"
