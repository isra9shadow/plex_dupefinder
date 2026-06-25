"""Tests for SqliteCache incident memory (record/recent/resolve)."""

from __future__ import annotations

from pathlib import Path

from core.cache import Incident, SqliteCache


def test_record_incident_inserts_open(tmp_path: Path) -> None:
    with SqliteCache(tmp_path / "c.db") as cache:
        cache.record_incident(
            "fp1",
            module="logwatch",
            severity="high",
            title="OOM killer",
            recommended=["increase ram"],
        )
        rows = cache.recent_incidents("logwatch")
    assert len(rows) == 1
    inc = rows[0]
    assert isinstance(inc, Incident)
    assert inc.fingerprint == "fp1"
    assert inc.status == "open"
    assert inc.severity == "high"
    assert inc.title == "OOM killer"
    assert inc.recommended == ["increase ram"]
    assert inc.applied == []
    assert inc.first_seen == inc.last_seen


def test_record_incident_upsert_updates_last_seen_and_merges(tmp_path: Path) -> None:
    with SqliteCache(tmp_path / "c.db") as cache:
        cache.record_incident(
            "fp1", module="logwatch", severity="low", title="t1", recommended=["a"]
        )
        first = cache.recent_incidents("logwatch")[0]
        cache.record_incident(
            "fp1",
            module="logwatch",
            severity="high",
            title="t2",
            recommended=["a", "b"],  # 'a' already present -> merged once
        )
        second = cache.recent_incidents("logwatch")[0]

    assert len(cache_rows(tmp_path)) == 1
    assert second.last_seen >= first.last_seen
    assert second.first_seen == first.first_seen
    assert second.severity == "high"  # latest observation wins
    assert second.title == "t2"
    assert second.recommended == ["a", "b"]  # union, order-preserving, no dup


def test_recent_incidents_ordering_and_limit(tmp_path: Path) -> None:
    with SqliteCache(tmp_path / "c.db") as cache:
        for i in range(5):
            cache.record_incident(
                f"fp{i}", module="m", severity="low", title=f"t{i}", recommended=[]
            )
        # Re-record fp0 so it becomes the most recently seen.
        cache.record_incident("fp0", module="m", severity="low", title="t0", recommended=[])
        ordered = cache.recent_incidents("m")
        limited = cache.recent_incidents("m", limit=2)

    assert ordered[0].fingerprint == "fp0"
    assert [i.last_seen for i in ordered] == sorted((i.last_seen for i in ordered), reverse=True)
    assert len(limited) == 2


def test_recent_incidents_filters_by_module(tmp_path: Path) -> None:
    with SqliteCache(tmp_path / "c.db") as cache:
        cache.record_incident("a", module="m1", severity="low", title="x", recommended=[])
        cache.record_incident("b", module="m2", severity="low", title="y", recommended=[])
        m1 = cache.recent_incidents("m1")
    assert [i.fingerprint for i in m1] == ["a"]


def test_resolve_incident(tmp_path: Path) -> None:
    with SqliteCache(tmp_path / "c.db") as cache:
        cache.record_incident("fp1", module="m", severity="low", title="t", recommended=["a"])
        cache.resolve_incident("fp1", applied=["restart container", {"action": "patch"}])
        inc = cache.recent_incidents("m")[0]
    assert inc.status == "resolved"
    assert inc.applied == ["restart container", {"action": "patch"}]


def test_incidents_persist_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    with SqliteCache(db) as cache:
        cache.record_incident("fp1", module="m", severity="low", title="t", recommended=["a"])
    with SqliteCache(db) as reopened:
        rows = reopened.recent_incidents("m")
    assert len(rows) == 1
    assert rows[0].fingerprint == "fp1"


def cache_rows(tmp_path: Path) -> list[Incident]:
    """Helper: read back all incidents for the single-module upsert test."""
    with SqliteCache(tmp_path / "c.db") as cache:
        return cache.recent_incidents("logwatch")
