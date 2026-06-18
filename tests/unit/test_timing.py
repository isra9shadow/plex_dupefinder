"""Tests for core.timing (phase profiler)."""

from __future__ import annotations

from core.timing import Profiler


def test_record_and_metrics() -> None:
    profiler = Profiler()
    profiler.record("x", 0.5)
    profiler.record("x", 0.5)
    metrics = profiler.metrics()
    assert metrics["x_ms"] == 1000.0
    assert metrics["x_count"] == 2.0


def test_phase_context_manager() -> None:
    profiler = Profiler()
    with profiler.phase("y"):
        pass
    metrics = profiler.metrics()
    assert "y_ms" in metrics
    assert metrics["y_count"] == 1.0
