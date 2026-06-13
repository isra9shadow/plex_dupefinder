"""Smoke tests: run.py dispatch, health, dry-run (no real FS changes)."""

from __future__ import annotations

import json
from pathlib import Path

import run


def _config(tmp_path: Path, *, mode: str = "dry_run") -> Path:
    cfg = {
        "safety": {"mode": mode, "quarantine_dir": str(tmp_path / "q")},
        "logging": {"dir": str(tmp_path / "logs")},
        "reporting": {"dir": str(tmp_path / "reports")},
        "pipelines": {"daily": ["noop"]},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def test_health_ok(tmp_path: Path) -> None:
    assert run.main(["health", "--config", str(_config(tmp_path))]) == 0


def test_run_noop_dry_run_writes_report(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    assert run.main(["noop", "--config", str(cfg), "--dry-run"]) == 0
    assert len(list((tmp_path / "reports").glob("*.json"))) == 1


def test_run_pipeline(tmp_path: Path) -> None:
    assert run.main(["daily", "--config", str(_config(tmp_path))]) == 0


def test_unknown_target_returns_2(tmp_path: Path) -> None:
    assert run.main(["bogus", "--config", str(_config(tmp_path))]) == 2
