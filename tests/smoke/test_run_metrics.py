"""Module-emitted metrics are merged into the written run report."""

from __future__ import annotations

import json
from pathlib import Path

import run
from core import registry
from core.types import FailureRecord, ModuleResult, RunContext
from tests.fakes import make_context


def _metric_module(ctx: RunContext) -> ModuleResult:
    result = ModuleResult("metricmod", ctx.run_id, ctx.mode)
    result.metrics = {"probe_ms": 12.0}
    return result


def _failing_module(ctx: RunContext) -> ModuleResult:
    result = ModuleResult("failmod", ctx.run_id, ctx.mode)
    result.add_failure(FailureRecord(category="integration", message="boom"))
    return result


def test_module_metrics_merge_into_report(tmp_path: Path) -> None:
    registry.register("metricmod")(_metric_module)
    ctx = make_context(tmp_path)
    assert run._run_module(ctx, "metricmod") == 0
    reports = list((tmp_path / "reports").glob("*.json"))
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    assert data["metrics"]["probe_ms"] == 12.0
    assert data["metrics"]["actions"] == 0.0


def test_module_failures_persisted_in_report(tmp_path: Path) -> None:
    registry.register("failmod")(_failing_module)
    ctx = make_context(tmp_path)
    assert run._run_module(ctx, "failmod") == 1  # failure -> rc 1
    data = json.loads(next((tmp_path / "reports").glob("*.json")).read_text(encoding="utf-8"))
    assert any(f["category"] == "integration" for f in data["failures"])
