"""Tests for modules.ops.webdashboard (HTML health panel)."""

from __future__ import annotations

import json
from pathlib import Path

from core.metrics import MetricsStore
from modules.ops import webdashboard
from tests.fakes import make_context


def test_sparkline_flat_for_few_points() -> None:
    assert "<line" in webdashboard.sparkline_svg([])
    assert "<line" in webdashboard.sparkline_svg([5.0])


def test_sparkline_polyline_for_series() -> None:
    svg = webdashboard.sparkline_svg([1.0, 2.0, 3.0])
    assert "<polyline" in svg
    assert svg.count(",") >= 3  # three x,y coords


def test_render_html_status_and_table() -> None:
    status = [
        {"module": "dbcheck", "ok": False, "failures": 1, "ts": "2026-07-06"},
        {"module": "uptime", "ok": True, "failures": 0, "ts": "2026-07-06"},
    ]
    sparks = {"dbcheck": ("corrupt_count", [0.0, 1.0])}
    out = webdashboard.render_html(status, sparks, generated="now")
    assert "izumi · panel de salud" in out
    assert "✓ OK" in out and "✕ FALLO" in out  # icon+label, not colour alone
    assert "corrupt_count: 1" in out
    assert "<polyline" in out  # sparkline for dbcheck
    assert "<table" in out and "<td>dbcheck</td>" in out  # accessible table view


def test_render_html_escapes_module_name() -> None:
    out = webdashboard.render_html(
        [{"module": "<x>", "ok": True, "failures": 0, "ts": ""}], {}, generated="now"
    )
    assert "&lt;x&gt;" in out and "<x>" not in out.replace("<x>", "", 0)


def test_render_html_empty() -> None:
    out = webdashboard.render_html([], {}, generated="now")
    assert "Sin métricas" in out


def test_run_writes_index_html(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    with MetricsStore(reports / "cache" / "metrics.db") as store:
        store.record("r1", "uptime", {"down": 2.0}, ok=False, failures=1)

    ctx = make_context(tmp_path)
    result = webdashboard.run(ctx, now="2026-07-06 03:00")

    page = (reports / "index.html").read_text(encoding="utf-8")
    assert "uptime" in page and "✕ FALLO" in page
    assert result.metrics["modules"] == 1.0
    plan = json.loads((reports / "webdashboard" / "plan.json").read_text(encoding="utf-8"))
    assert plan["modules"] == 1
