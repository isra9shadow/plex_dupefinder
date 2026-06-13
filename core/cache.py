"""Persistent cross-run cache — IMP-06.

Two backends, same idea (a file change invalidates its entry):

* ``Cache``       — JSON-backed opaque key→value store. Simple, whole-file load.
* ``SqliteCache`` — queryable per-media table (path, fingerprint, media_id, score,
  bitrate, duration, last_seen, extra). ``last_seen`` enables staleness pruning so
  files that stop being seen age out; the typed columns carry dupefinder decision
  inputs and support partial updates and ``last_seen`` queries.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from types import TracebackType


class Cache:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, object] = {}
        self._dirty = False
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                self._data = {str(k): v for k, v in loaded.items()}

    @staticmethod
    def _file_key(path: Path, namespace: str) -> str:
        try:
            stat = path.stat()
            fingerprint = f"{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            fingerprint = "missing"
        return f"{namespace}|{path}|{fingerprint}"

    def get(self, key: str) -> object | None:
        return self._data.get(key)

    def set(self, key: str, value: object) -> None:
        self._data[key] = value
        self._dirty = True

    def get_for_file(self, path: Path, namespace: str) -> object | None:
        return self._data.get(self._file_key(path, namespace))

    def set_for_file(self, path: Path, namespace: str, value: object) -> None:
        self.set(self._file_key(path, namespace), value)

    def save(self) -> None:
        if not self._dirty:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)
        self._dirty = False


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class MediaRecord:
    """One cached media file. ``extra`` is the parsed JSON blob (or None)."""

    path: str
    fingerprint: str
    media_id: str | None
    score: float | None
    bitrate: int | None
    duration: float | None
    last_seen: str
    extra: dict[str, object] | None


class SqliteCache:
    """Queryable persistent media cache (SQLite) — IMP-06 upgrade.

    One row per media file, keyed by ``path`` and validated by an mtime+size
    fingerprint so any file change invalidates its row. Writes are buffered in a
    single transaction and committed by ``save()``/``close()`` (fast bulk scans);
    ``get()`` is a pure read. ``prune()`` evicts rows not seen recently.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        # Single-writer durability/perf: WAL + NORMAL is the right tradeoff for a
        # one-process cache (falls back silently if the filesystem can't do WAL).
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS media ("
            "path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, media_id TEXT, "
            "score REAL, bitrate INTEGER, duration REAL, last_seen TEXT NOT NULL, extra TEXT)"
        )
        # last_seen drives prune() and staleness queries — index it to avoid a
        # full table scan as the library grows.
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_media_last_seen ON media(last_seen)")
        # Cross-run discovery/probe metrics history (observability).
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS runs (run_id TEXT, ts TEXT NOT NULL, metrics TEXT)"
        )
        self._conn.commit()
        self._dirty = False

    @staticmethod
    def _fingerprint(path: Path) -> str:
        try:
            stat = path.stat()
        except OSError:
            return "missing"
        return f"{stat.st_mtime_ns}:{stat.st_size}"

    def get(self, path: Path) -> MediaRecord | None:
        """Return the row for ``path`` iff its file is unchanged (fingerprint match)."""
        row = self._conn.execute(
            "SELECT path, fingerprint, media_id, score, bitrate, duration, last_seen, extra "
            "FROM media WHERE path = ?",
            (str(path),),
        ).fetchone()
        if row is None or row[1] != self._fingerprint(path):
            return None
        raw_extra = json.loads(row[7]) if row[7] else None
        return MediaRecord(
            path=row[0],
            fingerprint=row[1],
            media_id=row[2],
            score=row[3],
            bitrate=row[4],
            duration=row[5],
            last_seen=row[6],
            extra=raw_extra if isinstance(raw_extra, dict) else None,
        )

    def put(
        self,
        path: Path,
        *,
        media_id: str | None = None,
        score: float | None = None,
        bitrate: int | None = None,
        duration: float | None = None,
        extra: dict[str, object] | None = None,
    ) -> None:
        """Insert/replace the row for ``path`` and refresh its ``last_seen``."""
        self._conn.execute(
            "INSERT OR REPLACE INTO media "
            "(path, fingerprint, media_id, score, bitrate, duration, last_seen, extra) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(path),
                self._fingerprint(path),
                media_id,
                score,
                bitrate,
                duration,
                _utcnow(),
                json.dumps(extra) if extra is not None else None,
            ),
        )
        self._dirty = True

    def prune(self, *, older_than_days: int) -> int:
        """Delete rows whose ``last_seen`` is older than the cutoff. Returns count."""
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        deleted = self._conn.execute("DELETE FROM media WHERE last_seen < ?", (cutoff,)).rowcount
        self._dirty = True
        return deleted

    # Columns a caller may filter on — used to validate query_by (CTO-17).
    _COLUMNS = ("path", "fingerprint", "media_id", "score", "bitrate", "duration", "last_seen")
    _OPS = ("=", "!=", "<", "<=", ">", ">=")

    def query_by(self, column: str, op: str, value: object) -> list[MediaRecord]:
        """Safe filtered read: ``column``/``op`` are validated against allowlists
        and ``value`` is always parametrised — no SQL is built from caller input
        (CTO-17). Prefer this over ``query`` for any non-literal filter."""
        if column not in self._COLUMNS:
            raise ValueError(f"unknown column: {column!r}")
        if op not in self._OPS:
            raise ValueError(f"unsupported operator: {op!r}")
        return self.query(f"{column} {op} ?", (value,))

    def query(self, where: str = "", params: Iterable[object] = ()) -> list[MediaRecord]:
        """Read rows matching an optional WHERE clause (queryable schema, IMP-06).

        WARNING: ``where`` is raw SQL — pass only code literals here, never a
        string built from external data. For dynamic filters use ``query_by``.
        """
        sql = (
            "SELECT path, fingerprint, media_id, score, bitrate, duration, last_seen, extra "
            "FROM media"
        )
        if where:
            sql += " WHERE " + where
        records: list[MediaRecord] = []
        for row in self._conn.execute(sql, tuple(params)).fetchall():
            raw_extra = json.loads(row[7]) if row[7] else None
            records.append(
                MediaRecord(
                    path=row[0],
                    fingerprint=row[1],
                    media_id=row[2],
                    score=row[3],
                    bitrate=row[4],
                    duration=row[5],
                    last_seen=row[6],
                    extra=raw_extra if isinstance(raw_extra, dict) else None,
                )
            )
        return records

    def count(self) -> int:
        """Number of cached media rows (observability)."""
        row = self._conn.execute("SELECT COUNT(*) FROM media").fetchone()
        return int(row[0]) if row else 0

    def record_run(self, run_id: str, metrics: Mapping[str, object]) -> None:
        """Append a discovery/probe metrics snapshot for this run (history)."""
        self._conn.execute(
            "INSERT INTO runs (run_id, ts, metrics) VALUES (?, ?, ?)",
            (run_id, _utcnow(), json.dumps(metrics)),
        )
        self._dirty = True

    def recent_runs(self, limit: int = 10) -> list[dict[str, object]]:
        """Most recent run metrics, newest first (for trend reports)."""
        rows = self._conn.execute(
            "SELECT run_id, ts, metrics FROM runs ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        out: list[dict[str, object]] = []
        for row in rows:
            parsed = json.loads(row[2]) if row[2] else {}
            out.append(
                {
                    "run_id": row[0],
                    "ts": row[1],
                    "metrics": parsed if isinstance(parsed, dict) else {},
                }
            )
        return out

    def save(self) -> None:
        if self._dirty:
            self._conn.commit()
            self._dirty = False

    def close(self) -> None:
        self.save()
        self._conn.close()

    def __enter__(self) -> SqliteCache:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
