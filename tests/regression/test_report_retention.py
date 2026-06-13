"""Tests for _prune_old_files — opt-in retention of report/plan JSON (CTO-18).

Run with:  pytest tests/regression/test_report_retention.py -q
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import plex_dupefinder as pd


def _make(directory: Path, n: int) -> None:
    for i in range(n):
        f = directory / f"dupefinder_report_{i}.json"
        f.write_text("{}", encoding="utf-8")
        os.utime(f, (time.time() + i, time.time() + i))  # ascending mtime


def test_keep_zero_disables_pruning(tmp_path: Path) -> None:
    _make(tmp_path, 5)
    assert pd._prune_old_files(str(tmp_path), "dupefinder_report_*.json", 0) == 0
    assert len(list(tmp_path.glob("*.json"))) == 5


def test_keeps_newest_n(tmp_path: Path) -> None:
    _make(tmp_path, 5)
    removed = pd._prune_old_files(str(tmp_path), "dupefinder_report_*.json", 2)
    assert removed == 3
    remaining = sorted(p.name for p in tmp_path.glob("*.json"))
    # Newest mtimes were i=3,4 -> those survive.
    assert remaining == ["dupefinder_report_3.json", "dupefinder_report_4.json"]


def test_pattern_scoped(tmp_path: Path) -> None:
    _make(tmp_path, 3)
    (tmp_path / "other.json").write_text("{}", encoding="utf-8")
    pd._prune_old_files(str(tmp_path), "dupefinder_report_*.json", 1)
    assert (tmp_path / "other.json").exists()  # unrelated files untouched


def test_missing_dir_is_safe(tmp_path: Path) -> None:
    assert pd._prune_old_files(str(tmp_path / "nope"), "*.json", 2) == 0
