"""Tests for core.cache.SqliteCache (queryable persistent media cache, IMP-06)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core.cache import SqliteCache


def _touch(path: Path, data: bytes = b"x") -> Path:
    path.write_bytes(data)
    return path


def test_put_get_roundtrip(tmp_path: Path) -> None:
    media = _touch(tmp_path / "m.mkv")
    cache = SqliteCache(tmp_path / "c.db")
    assert cache.get(media) is None
    cache.put(
        media, media_id="42", score=1234.5, bitrate=8000, duration=3600.0, extra={"codec": "hevc"}
    )
    rec = cache.get(media)
    assert rec is not None
    assert rec.media_id == "42"
    assert rec.score == 1234.5
    assert rec.bitrate == 8000
    assert rec.duration == 3600.0
    assert rec.extra == {"codec": "hevc"}
    cache.close()


def test_persists_across_instances(tmp_path: Path) -> None:
    media = _touch(tmp_path / "m.mkv")
    db = tmp_path / "c.db"
    with SqliteCache(db) as cache:
        cache.put(media, duration=10.0)
    with SqliteCache(db) as reopened:
        rec = reopened.get(media)
        assert rec is not None and rec.duration == 10.0


def test_fingerprint_invalidates_on_change(tmp_path: Path) -> None:
    media = _touch(tmp_path / "m.mkv", b"abc")
    cache = SqliteCache(tmp_path / "c.db")
    cache.put(media, duration=1.0)
    assert cache.get(media) is not None
    media.write_bytes(b"abcd")  # size changes -> fingerprint mismatch -> miss
    assert cache.get(media) is None
    cache.close()


def test_get_missing_file_is_none(tmp_path: Path) -> None:
    media = _touch(tmp_path / "m.mkv")
    cache = SqliteCache(tmp_path / "c.db")
    cache.put(media, duration=1.0)
    media.unlink()  # file gone -> fingerprint "missing" -> no false hit
    assert cache.get(media) is None
    cache.close()


def test_prune_evicts_stale_rows(tmp_path: Path) -> None:
    media = _touch(tmp_path / "m.mkv")
    db = tmp_path / "c.db"
    cache = SqliteCache(db)
    cache.put(media, duration=1.0)
    cache.save()
    # Backdate last_seen well past the retention window.
    old = (datetime.now(UTC) - timedelta(days=99)).isoformat()
    cache._conn.execute("UPDATE media SET last_seen = ?", (old,))
    cache._conn.commit()
    assert cache.prune(older_than_days=30) == 1
    assert cache.query() == []
    cache.close()


def test_query_by_column(tmp_path: Path) -> None:
    cache = SqliteCache(tmp_path / "c.db")
    a = _touch(tmp_path / "a.mkv")
    b = _touch(tmp_path / "b.mkv")
    cache.put(a, score=100.0)
    cache.put(b, score=900.0)
    high = cache.query("score > ?", (500.0,))
    assert len(high) == 1
    assert high[0].path == str(b)
    assert cache.count() == 2
    cache.close()


def test_query_by_allowlist(tmp_path: Path) -> None:
    cache = SqliteCache(tmp_path / "c.db")
    a = _touch(tmp_path / "a.mkv")
    b = _touch(tmp_path / "b.mkv")
    cache.put(a, score=100.0)
    cache.put(b, score=900.0)
    assert len(cache.query_by("score", ">", 500.0)) == 1
    cache.close()


def test_query_by_rejects_bad_column(tmp_path: Path) -> None:
    cache = SqliteCache(tmp_path / "c.db")
    with pytest.raises(ValueError):
        cache.query_by("score; DROP TABLE media", "=", 1)
    with pytest.raises(ValueError):
        cache.query_by("score", "OR 1=1", 1)
    cache.close()


def test_last_seen_index_exists(tmp_path: Path) -> None:
    cache = SqliteCache(tmp_path / "c.db")
    names = [
        r[0]
        for r in cache._conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    ]
    assert "idx_media_last_seen" in names
    cache.close()


def test_run_metrics_history(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    with SqliteCache(db) as cache:
        cache.record_run("run-a", {"probe_ms": 10.0})
        cache.record_run("run-b", {"probe_ms": 20.0})
    with SqliteCache(db) as reopened:
        runs = reopened.recent_runs(limit=5)
        assert len(runs) == 2
        assert runs[0]["run_id"] == "run-b"  # newest first
        assert runs[0]["metrics"] == {"probe_ms": 20.0}
