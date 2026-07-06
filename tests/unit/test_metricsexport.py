"""Tests for modules.ops.metricsexport (Prometheus textfile export)."""

from __future__ import annotations

import json
from pathlib import Path

from core.metrics import MetricsStore
from modules.ops import metricsexport
from tests.fakes import make_context


def test_render_prometheus_shape() -> None:
    status = [{"module": "dbcheck", "ok": True, "failures": 0}]
    metrics = [{"module": "dbcheck", "key": "corrupt_count", "value": 1.0}]
    out = metricsexport.render_prometheus(status, metrics)
    assert 'izumi_module_ok{module="dbcheck"} 1' in out
    assert 'izumi_module_failures{module="dbcheck"} 0' in out
    assert 'izumi_module_metric{module="dbcheck",metric="corrupt_count"} 1.0' in out
    assert out.endswith("\n")


def test_render_escapes_label_values() -> None:
    out = metricsexport.render_prometheus([{"module": 'a"b', "ok": False, "failures": 1}], [])
    assert 'module="a\\"b"' in out


def test_run_reads_store_and_writes_prom(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    with MetricsStore(reports / "cache" / "metrics.db") as store:
        store.record("r1", "uptime", {"down": 2.0}, ok=False, failures=1)

    ctx = make_context(tmp_path)  # reporting.dir == tmp_path/"reports"
    result = metricsexport.run(ctx)

    prom = (reports / "izumi.prom").read_text(encoding="utf-8")
    assert 'izumi_module_ok{module="uptime"} 0' in prom
    assert 'izumi_module_metric{module="uptime",metric="down"} 2.0' in prom
    assert result.metrics["metrics_exported"] == 1.0
    plan = json.loads((reports / "metricsexport" / "plan.json").read_text(encoding="utf-8"))
    assert plan["modules"] == 1


def test_run_without_store_notes_and_writes_empty_prom(tmp_path: Path) -> None:
    ctx = make_context(tmp_path)  # no metrics.db yet
    result = metricsexport.run(ctx)
    assert result.ok
    prom = (tmp_path / "reports" / "izumi.prom").read_text(encoding="utf-8")
    assert "izumi_module_ok" in prom  # headers present even with no data
    plan = json.loads(
        (tmp_path / "reports" / "metricsexport" / "plan.json").read_text(encoding="utf-8")
    )
    assert "no metrics yet" in plan["note"]
