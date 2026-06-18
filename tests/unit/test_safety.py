"""Tests for core.safety (the shared guard gauntlet)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from core import safety as safetymod
from core.config import Config, SafetyConfig
from core.types import QuarantineEntry, SafetyMode


def _pol(**kw: object) -> safetymod.SafetyPolicy:
    return safetymod.SafetyPolicy(Config(safety=SafetyConfig(**kw)))  # type: ignore[arg-type]


def test_resolve_mode() -> None:
    assert _pol(mode=SafetyMode.AUDIT).resolve_mode() is SafetyMode.AUDIT


def test_min_age(tmp_path: Path) -> None:
    f = tmp_path / "f.bin"
    f.write_bytes(b"x")
    pol = _pol(min_file_age_hours=1.0)
    assert pol.min_age_ok(f) is False
    old = time.time() - 2 * 3600
    os.utime(f, (old, old))
    assert pol.min_age_ok(f) is True
    assert _pol(min_file_age_hours=0.0).min_age_ok(tmp_path / "missing") is True
    assert pol.min_age_ok(tmp_path / "missing") is False


def test_is_stable_static(tmp_path: Path) -> None:
    f = tmp_path / "f.bin"
    f.write_bytes(b"abc")
    assert _pol(stability_check_seconds=0.0).is_stable([f]) is True


def test_is_stable_changing(monkeypatch: pytest.MonkeyPatch) -> None:
    sizes = iter([{"a": 1}, {"a": 2}])
    monkeypatch.setattr(safetymod, "_snapshot_sizes", lambda _paths: next(sizes))
    assert _pol(stability_check_seconds=0.0).is_stable([Path("a")]) is False


def test_snapshot_sizes_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert safetymod._snapshot_sizes([missing])[str(missing)] == -1


def test_size_ratio() -> None:
    pol = _pol(max_size_ratio=5.0)
    assert pol.size_ratio_ok(3, 1) is True
    assert pol.size_ratio_ok(10, 1) is False
    assert _pol(max_size_ratio=0.0).size_ratio_ok(999, 1) is True


def test_should_purge() -> None:
    old = QuarantineEntry("o", "q", "s", "r", "cmd", time.time() - 10 * 86400)
    fresh = QuarantineEntry("o", "q", "s", "r", "cmd", time.time())
    pol = _pol(retention_days=7.0)
    assert pol.should_purge(old) is True
    assert pol.should_purge(fresh) is False
    assert _pol(retention_days=0.0).should_purge(old) is False


def test_passes_guards(tmp_path: Path) -> None:
    f = tmp_path / "f.bin"
    f.write_bytes(b"x")
    old = time.time() - 2 * 3600
    os.utime(f, (old, old))
    pol = _pol(min_file_age_hours=1.0, stability_check_seconds=0.0, max_size_ratio=5.0)
    assert pol.passes_guards([f]) is True
    assert pol.passes_guards([f], keeper_size=1, candidate_size=100) is False
    fresh = tmp_path / "g.bin"
    fresh.write_bytes(b"x")
    assert pol.passes_guards([fresh]) is False
