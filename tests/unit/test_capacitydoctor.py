"""Tests for modules.ops.capacitydoctor (days-to-full forecast, read-only)."""

from __future__ import annotations

import json
from pathlib import Path

from modules.ops import capacitydoctor
from tests.fakes import make_context

_DAY = 86400.0
_GB = 1024**3


def _read_plan(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / "reports" / "capacitydoctor" / "plan.json").read_text(encoding="utf-8")
    )


# --- days_to_full (pure) -------------------------------------------------------


def test_days_to_full_linear_growth() -> None:
    # used grows 1 GB/day; 10 GB left -> ~10 days.
    total = 100 * _GB
    points = [(0.0, 89 * _GB), (1 * _DAY, 90 * _GB)]  # +1GB/day, 10GB remaining at last point
    dtf = capacitydoctor.days_to_full(points, float(total))
    assert 9.5 < dtf < 10.5


def test_days_to_full_flat_is_never() -> None:
    points = [(0.0, 50 * _GB), (1 * _DAY, 50 * _GB)]
    assert capacitydoctor.days_to_full(points, float(100 * _GB)) == -1.0


def test_days_to_full_needs_two_points() -> None:
    assert capacitydoctor.days_to_full([(0.0, 1.0)], 100.0) == -1.0


# --- evaluate ------------------------------------------------------------------


def test_evaluate_over_threshold() -> None:
    f = capacitydoctor.evaluate("cache", "/c", (100, 95, 5), [], warn_pct=90, warn_days=14)
    assert f.status == "over_threshold" and f.percent == 95.0


def test_evaluate_full_soon_from_trend() -> None:
    total = 100 * _GB
    hist = [(0.0, 80 * _GB), (1 * _DAY, 85 * _GB)]  # +5GB/day, 15GB left -> ~3 days
    f = capacitydoctor.evaluate(
        "array", "/a", (total, 85 * _GB, 15 * _GB), hist, warn_pct=99, warn_days=14
    )
    assert f.status == "full_soon" and 0 < f.days_to_full <= 14


def test_evaluate_ok() -> None:
    f = capacitydoctor.evaluate("cache", "/c", (100, 10, 90), [], warn_pct=90, warn_days=14)
    assert f.status == "ok"


# --- run -----------------------------------------------------------------------


def test_run_records_history_and_flags(tmp_path: Path) -> None:
    ctx = make_context(
        tmp_path,
        integrations={
            "capacitydoctor": {
                "paths": [{"name": "cache", "path": "/mnt/cache"}],
                "warn_percent": 90,
            }
        },
    )
    result = capacitydoctor.run(ctx, usage=lambda p: (100, 95, 5), now=1000.0)
    assert result.metrics["checked"] == 1.0
    assert result.metrics["at_risk"] == 1.0  # 95% over threshold
    assert not result.ok
    # A second run appended history.
    capacitydoctor.run(ctx, usage=lambda p: (100, 96, 4), now=1000.0 + _DAY)
    hist = json.loads(
        (tmp_path / "reports" / "cache" / "capacity.json").read_text(encoding="utf-8")
    )
    assert len(hist["history"]["/mnt/cache"]) == 2


def test_run_unreadable_path_is_error(tmp_path: Path) -> None:
    ctx = make_context(
        tmp_path, integrations={"capacitydoctor": {"paths": [{"name": "gone", "path": "/nope"}]}}
    )

    def boom(path: str) -> tuple[int, int, int]:
        raise OSError("no such path")

    result = capacitydoctor.run(ctx, usage=boom, now=1000.0)
    assert any("gone" in f.message for f in result.failures)
    assert _read_plan(tmp_path)["paths"][0]["status"] == "error"


def test_run_no_paths_note(tmp_path: Path) -> None:
    ctx = make_context(tmp_path)
    result = capacitydoctor.run(ctx, usage=lambda p: (1, 0, 1))
    assert result.ok
    assert "No paths configured" in _read_plan(tmp_path)["note"]
