"""Tests for modules.ops.webdashboard (HTML health panel)."""

from __future__ import annotations

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


def test_render_html_status_table_and_refresh() -> None:
    status = [
        {"module": "dbcheck", "ok": False, "failures": 1, "ts": "2026-07-06"},
        {"module": "uptime", "ok": True, "failures": 0, "ts": "2026-07-06"},
    ]
    sparks = {"dbcheck": ("corrupt_count", [0.0, 1.0])}
    out = webdashboard.render_html(status, sparks, [], generated="now")
    assert "izumi · panel de salud" in out
    assert "✓ OK" in out and "✕ FALLO" in out  # icon+label, not colour alone
    assert "corrupt_count: 1" in out
    assert "<polyline" in out  # sparkline for dbcheck
    assert "<table" in out and "<td>dbcheck</td>" in out  # accessible table view
    assert "http-equiv=refresh" in out  # auto-refresh


def test_render_html_module_cards() -> None:
    out = webdashboard.render_html([], {}, [("dbcheck", "1 corrupta")], generated="now")
    assert "Detalle por módulo" in out
    assert "dbcheck" in out and "1 corrupta" in out
    assert 'href="dbcheck/summary.md"' in out  # link to the raw report


def test_render_html_escapes_module_name() -> None:
    out = webdashboard.render_html(
        [{"module": "<x>", "ok": True, "failures": 0, "ts": ""}], {}, [], generated="now"
    )
    assert "&lt;x&gt;" in out


def test_render_html_empty() -> None:
    out = webdashboard.render_html([], {}, [], generated="now")
    assert "Sin informes todavía" in out


def test_run_writes_index_with_tiles_and_cards(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    with MetricsStore(reports / "cache" / "metrics.db") as store:
        store.record("r1", "uptime", {"down": 2.0}, ok=False, failures=1)
    # A module report subdir with a summary → becomes a detail card.
    (reports / "dbcheck").mkdir(parents=True, exist_ok=True)
    (reports / "dbcheck" / "summary.md").write_text("1 corrupta", encoding="utf-8")

    ctx = make_context(tmp_path)
    result = webdashboard.run(ctx, now="2026-07-06 03:00")

    page = (reports / "index.html").read_text(encoding="utf-8")
    assert "uptime" in page and "✕ FALLO" in page  # status tile
    assert "1 corrupta" in page  # detail card
    assert result.metrics["modules"] == 1.0 and result.metrics["cards"] == 1.0


def test_run_skips_cache_and_own_dir(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    for skip in ("cache", "webdashboard", "inventory"):
        (reports / skip).mkdir()
        (reports / skip / "summary.md").write_text("x", encoding="utf-8")
    ctx = make_context(tmp_path)
    result = webdashboard.run(ctx, now="now")
    assert result.metrics["cards"] == 0.0  # infra dirs are not cards
