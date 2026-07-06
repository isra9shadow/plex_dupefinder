"""Tests for core.metrics (persistent queryable metrics store)."""

from __future__ import annotations

from pathlib import Path

from core.metrics import MetricsStore


def test_record_and_latest_metrics(tmp_path: Path) -> None:
    with MetricsStore(tmp_path / "m.db") as store:
        store.record("r1", "dbcheck", {"corrupt_count": 1.0, "checked": 3.0}, ok=False, failures=2)
        store.record("r2", "dbcheck", {"corrupt_count": 0.0, "checked": 3.0}, ok=True, failures=0)

        latest = {m["key"]: m["value"] for m in store.latest_metrics() if m["module"] == "dbcheck"}
        assert latest == {"corrupt_count": 0.0, "checked": 3.0}  # r2 is newest

        status = {s["module"]: s for s in store.latest_status()}
        assert status["dbcheck"]["ok"] is True
        assert status["dbcheck"]["failures"] == 0


def test_series_orders_oldest_first(tmp_path: Path) -> None:
    with MetricsStore(tmp_path / "m.db") as store:
        store.record("r1", "diskwatch", {"temp": 40.0}, ok=True, failures=0)
        store.record("r2", "diskwatch", {"temp": 42.0}, ok=True, failures=0)
        series = store.series("diskwatch", "temp", days=30)
        assert [v for _ts, v in series] == [40.0, 42.0]


def test_non_numeric_and_bool_metrics_are_skipped(tmp_path: Path) -> None:
    with MetricsStore(tmp_path / "m.db") as store:
        store.record("r1", "x", {"n": 5, "flag": True, "name": "hi"}, ok=True, failures=0)  # type: ignore[dict-item]
        keys = {m["key"] for m in store.latest_metrics()}
        assert keys == {"n"}  # bool and str dropped


def test_prune_removes_old_rows(tmp_path: Path) -> None:
    store = MetricsStore(tmp_path / "m.db")
    try:
        store.record("r1", "x", {"n": 1.0}, ok=True, failures=0)
        # Nothing is older than 90 days yet.
        assert store.prune(older_than_days=90) == 0
        # Everything is "older than -1 days" (future cutoff) -> all removed.
        removed = store.prune(older_than_days=-1)
        assert removed >= 2  # the metric row + the run_status row
        assert store.latest_metrics() == []
    finally:
        store.close()


def test_empty_store_queries(tmp_path: Path) -> None:
    with MetricsStore(tmp_path / "m.db") as store:
        assert store.latest_metrics() == []
        assert store.latest_status() == []
        assert store.series("none", "none") == []
