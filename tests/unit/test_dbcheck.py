"""Tests for modules.ops.dbcheck (read-only SQLite corruption detector)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from modules.ops import dbcheck
from tests.fakes import make_context


def _read_results(tmp_path: Path) -> list[dict[str, object]]:
    plan = json.loads((tmp_path / "reports" / "dbcheck" / "plan.json").read_text(encoding="utf-8"))
    results = plan["results"]
    assert isinstance(results, list)
    return [r for r in results if isinstance(r, dict)]


def _read_plan(tmp_path: Path) -> dict[str, object]:
    plan = json.loads((tmp_path / "reports" / "dbcheck" / "plan.json").read_text(encoding="utf-8"))
    assert isinstance(plan, dict)
    return plan


def _read_summary(tmp_path: Path) -> str:
    return (tmp_path / "reports" / "dbcheck" / "summary.md").read_text(encoding="utf-8")


def _make_healthy_db(path: Path) -> None:
    """Create a tiny, valid SQLite database with one populated table."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE metadata (id INTEGER PRIMARY KEY, title TEXT)")
        conn.executemany(
            "INSERT INTO metadata (title) VALUES (?)",
            [("Movie A",), ("Show B",), ("Episode C",)],
        )
        conn.commit()
    finally:
        conn.close()


def _make_garbage_db(path: Path) -> None:
    """Write a file that is NOT a valid SQLite database (truncated garbage)."""
    path.write_bytes(b"SQLite format 3\x00 totally not a real database, just noise" * 4)


# --- pure logic ----------------------------------------------------------------


def test_evaluate_ok() -> None:
    assert dbcheck.evaluate(["ok"]) == (True, "ok")


def test_evaluate_reports_corruption_detail() -> None:
    ok, detail = dbcheck.evaluate(["*** in database main ***", "row 3 missing from index idx"])
    assert ok is False
    assert "row 3 missing" in detail


def test_evaluate_empty_is_not_ok() -> None:
    ok, _ = dbcheck.evaluate([])
    assert ok is False


def test_databases_parsing_skips_invalid(tmp_path: Path) -> None:
    ctx = make_context(
        tmp_path,
        integrations={
            "dbcheck": {
                "databases": [
                    {"name": "Plex", "path": "/data/plex.db"},
                    {"path": "/data/sonarr.db"},  # name falls back to basename
                    {"name": "no-path"},  # skipped: no path
                    "garbage",  # skipped: not a dict
                ]
            }
        },
    )
    parsed = dbcheck._databases(ctx)
    assert parsed == [("Plex", "/data/plex.db"), ("sonarr.db", "/data/sonarr.db")]


# --- real sqlite detection -----------------------------------------------------


def test_quick_check_on_healthy_db(tmp_path: Path) -> None:
    db = tmp_path / "healthy.db"
    _make_healthy_db(db)
    assert dbcheck.quick_check(str(db)) == ["ok"]


def test_check_database_detects_garbage(tmp_path: Path) -> None:
    db = tmp_path / "garbage.db"
    _make_garbage_db(db)
    res = dbcheck.check_database("Broken", str(db), dbcheck.quick_check)
    assert res.corrupt
    assert res.detail != "ok"


def test_check_database_missing_file(tmp_path: Path) -> None:
    res = dbcheck.check_database("Gone", str(tmp_path / "nope.db"), dbcheck.quick_check)
    assert res.corrupt
    assert res.detail == "file not found"


# --- run integration -----------------------------------------------------------


def test_run_detects_one_healthy_one_corrupt(tmp_path: Path) -> None:
    healthy = tmp_path / "plex.db"
    garbage = tmp_path / "sonarr.db"
    _make_healthy_db(healthy)
    _make_garbage_db(garbage)

    ctx = make_context(
        tmp_path,
        integrations={
            "dbcheck": {
                "databases": [
                    {"name": "Plex", "path": str(healthy)},
                    {"name": "Sonarr", "path": str(garbage)},
                ]
            }
        },
    )
    result = dbcheck.run(ctx)

    assert result.actions == 0  # read-only
    assert result.metrics["databases_checked"] == 2.0
    assert result.metrics["corrupt_count"] == 1.0
    assert not result.ok  # the corrupt DB is surfaced as a failure
    assert any("Sonarr" in f.message for f in result.failures)

    plan = _read_plan(tmp_path)
    assert plan["corrupt_count"] == 1
    by_name = {r["name"]: r for r in _read_results(tmp_path)}
    assert by_name["Plex"]["ok"] is True
    assert by_name["Sonarr"]["ok"] is False

    summary = _read_summary(tmp_path)
    assert "[OK] Plex" in summary
    assert "[CORRUPT] Sonarr" in summary


def test_run_all_healthy_is_ok(tmp_path: Path) -> None:
    healthy = tmp_path / "radarr.db"
    _make_healthy_db(healthy)

    ctx = make_context(
        tmp_path,
        integrations={"dbcheck": {"databases": [{"name": "Radarr", "path": str(healthy)}]}},
    )
    result = dbcheck.run(ctx)

    assert result.ok
    assert result.metrics["corrupt_count"] == 0.0
    assert _read_plan(tmp_path)["corrupt_count"] == 0


def test_run_no_databases_configured(tmp_path: Path) -> None:
    ctx = make_context(tmp_path)
    result = dbcheck.run(ctx)

    assert result.ok  # nothing to check is not a failure
    assert result.metrics["databases_checked"] == 0.0
    assert "No databases configured" in _read_summary(tmp_path)


def test_run_missing_file_recorded_not_aborted(tmp_path: Path) -> None:
    healthy = tmp_path / "plex.db"
    _make_healthy_db(healthy)

    ctx = make_context(
        tmp_path,
        integrations={
            "dbcheck": {
                "databases": [
                    {"name": "Ghost", "path": str(tmp_path / "missing.db")},
                    {"name": "Plex", "path": str(healthy)},
                ]
            }
        },
    )
    result = dbcheck.run(ctx)

    # The missing DB is recorded as a failure but the healthy one is still checked.
    assert any("Ghost" in f.message for f in result.failures)
    assert result.metrics["databases_checked"] == 2.0
    assert result.metrics["corrupt_count"] == 1.0
    by_name = {r["name"]: r for r in _read_results(tmp_path)}
    assert by_name["Plex"]["ok"] is True


def test_run_uses_patched_checker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = tmp_path / "fake.db"
    db.write_bytes(b"placeholder")  # exists so the file check passes

    calls: list[str] = []

    def fake_checker(path: str) -> list[str]:
        calls.append(path)
        return ["*** in database main ***", "page 5 is never used"]

    monkeypatch.setattr(dbcheck, "quick_check", fake_checker)
    ctx = make_context(
        tmp_path,
        integrations={"dbcheck": {"databases": [{"name": "Injected", "path": str(db)}]}},
    )
    result = dbcheck.run(ctx)

    assert calls == [str(db)]
    assert result.metrics["corrupt_count"] == 1.0
    assert "page 5 is never used" in str(_read_results(tmp_path)[0]["detail"])
