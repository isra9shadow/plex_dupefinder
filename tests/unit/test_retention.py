"""Tests for modules.ops.retention (metrics prune + report/log footprint audit)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.metrics import MetricsStore
from modules.ops import retention
from tests.fakes import make_context


def _db_path(tmp_path: Path) -> Path:
    return tmp_path / "reports" / "cache" / "metrics.db"


def _plan(tmp_path: Path) -> dict[str, object]:
    data = json.loads(
        (tmp_path / "reports" / "retention" / "plan.json").read_text(encoding="utf-8")
    )
    assert isinstance(data, dict)
    return data


def test_dir_size_counts_files_recursively(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"12345")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_bytes(b"678")
    assert retention.dir_size_bytes(tmp_path) == 8
    assert retention.dir_size_bytes(tmp_path / "missing") == 0


def test_no_db_notes_and_prunes_nothing(tmp_path: Path) -> None:
    result = retention.run(make_context(tmp_path))
    assert result.ok
    assert result.actions == 0
    assert result.metrics["rows_pruned"] == 0.0
    assert "no metrics.db" in str(_plan(tmp_path)["note"])


def test_prunes_old_rows_and_keeps_recent(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    old = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    with MetricsStore(db) as store:
        store.record("run-new", "uptime", {"x": 1.0}, ok=True, failures=0)  # fresh, survives
        store._conn.execute(
            "INSERT INTO run_status (run_id, module, ts, ok, failures) VALUES (?, ?, ?, ?, ?)",
            ("run-old", "dbcheck", old, 1, 0),
        )
        store._conn.execute(
            "INSERT INTO metrics (run_id, module, ts, key, value) VALUES (?, ?, ?, ?, ?)",
            ("run-old", "dbcheck", old, "y", 2.0),
        )
        store._conn.commit()

    result = retention.run(make_context(tmp_path))

    assert result.metrics["rows_pruned"] >= 2.0  # old status row + old metric row
    plan = _plan(tmp_path)
    assert plan["metrics_days"] == 90
    assert float(plan["metrics_db_mb"]) >= 0.0  # type: ignore[arg-type]
    with MetricsStore(db) as store:  # the fresh row is still there
        assert any(s["module"] == "uptime" for s in store.latest_status())


def test_reports_cap_exceeded_records_failure(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "big.bin").write_bytes(b"x" * 4096)
    ctx = make_context(tmp_path, integrations={"retention": {"reports_max_mb": 0.001}})

    result = retention.run(ctx)

    assert not result.ok
    assert any("reports dir" in f.message for f in result.failures)
    assert _plan(tmp_path)["over_cap"]  # non-empty list
