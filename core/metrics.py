"""Persistent, queryable metrics store — per-run module metrics for trends.

Complements :class:`core.cache.SqliteCache` (opaque run blobs + incidents): this
stores each metric as a queryable ``(module, key, value, ts)`` row plus a per-run
``ok``/``failures`` status, so the dashboard and capacity forecasting can plot
series over time. ``run.py`` records into it after every module run; it is
best-effort (a failure to persist never affects a run).

stdlib-only and 3.9-safe (host-side tooling may read it): no StrEnum, timezone.utc,
tuple-form isinstance.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class MetricsStore:
    """SQLite-backed metrics history. One row per (run, module, metric key)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS metrics ("
            "run_id TEXT, module TEXT NOT NULL, ts TEXT NOT NULL, "
            "key TEXT NOT NULL, value REAL)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_metrics_mod_key_ts ON metrics(module, key, ts)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS run_status ("
            "run_id TEXT, module TEXT NOT NULL, ts TEXT NOT NULL, "
            "ok INTEGER, failures INTEGER)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_status_mod_ts ON run_status(module, ts)"
        )
        self._conn.commit()

    def record(
        self,
        run_id: str,
        module: str,
        metrics: Mapping[str, object],
        *,
        ok: bool,
        failures: int,
    ) -> None:
        """Persist a module run: each numeric metric + the run's ok/failures."""
        ts = _utcnow()
        rows = [
            (run_id, module, ts, str(k), float(v))
            for k, v in metrics.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]
        if rows:
            self._conn.executemany(
                "INSERT INTO metrics (run_id, module, ts, key, value) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        self._conn.execute(
            "INSERT INTO run_status (run_id, module, ts, ok, failures) VALUES (?, ?, ?, ?, ?)",
            (run_id, module, ts, 1 if ok else 0, int(failures)),
        )
        self._conn.commit()

    def series(self, module: str, key: str, *, days: int = 30) -> list[tuple[str, float]]:
        """``(ts, value)`` points for one metric over the last ``days``, oldest first."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self._conn.execute(
            "SELECT ts, value FROM metrics WHERE module = ? AND key = ? AND ts >= ? ORDER BY ts",
            (module, key, cutoff),
        ).fetchall()
        return [(str(r[0]), float(r[1])) for r in rows]

    def latest_status(self) -> list[dict[str, object]]:
        """The most recent ok/failures per module (newest run of each)."""
        rows = self._conn.execute(
            "SELECT module, ts, ok, failures FROM run_status r WHERE ts = "
            "(SELECT MAX(ts) FROM run_status WHERE module = r.module) ORDER BY module"
        ).fetchall()
        return [
            {"module": r[0], "ts": r[1], "ok": bool(r[2]), "failures": int(r[3] or 0)} for r in rows
        ]

    def latest_metrics(self) -> list[dict[str, object]]:
        """The most recent value of every (module, key)."""
        rows = self._conn.execute(
            "SELECT module, key, value, ts FROM metrics m WHERE ts = "
            "(SELECT MAX(ts) FROM metrics WHERE module = m.module AND key = m.key) "
            "ORDER BY module, key"
        ).fetchall()
        return [{"module": r[0], "key": r[1], "value": float(r[2]), "ts": r[3]} for r in rows]

    def prune(self, *, older_than_days: int = 90) -> int:
        """Delete metric/status rows older than the cutoff. Returns rows removed."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        n = self._conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,)).rowcount
        n += self._conn.execute("DELETE FROM run_status WHERE ts < ?", (cutoff,)).rowcount
        self._conn.commit()
        return int(n)

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def __enter__(self) -> MetricsStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
