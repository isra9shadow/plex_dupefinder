"""Tests for core.locks (single-instance job locks)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from core import locks
from core.errors import LockError


def _plant_lock(lock_dir: Path, name: str, *, pid: int, ts: float) -> None:
    lockpath = lock_dir / f"{name}.lock"
    lockpath.mkdir()
    (lockpath / "owner.json").write_text(json.dumps({"pid": pid, "ts": ts}), encoding="utf-8")


def test_lock_acquire_release(tmp_path: Path) -> None:
    assert not locks.is_locked("job", lock_dir=tmp_path)
    with locks.with_lock("job", lock_dir=tmp_path):
        assert locks.is_locked("job", lock_dir=tmp_path)
    assert not locks.is_locked("job", lock_dir=tmp_path)


def test_second_lock_raises(tmp_path: Path) -> None:
    with locks.with_lock("job", lock_dir=tmp_path):
        with pytest.raises(LockError):
            with locks.with_lock("job", lock_dir=tmp_path):
                pass


def test_default_lock_dir_used() -> None:
    assert locks.is_locked("definitely-not-a-real-job-xyz") is False


def test_expired_lock_is_broken(tmp_path: Path) -> None:
    # A lock whose owner timestamp is ancient is stale and must be reacquirable.
    _plant_lock(tmp_path, "job", pid=os.getpid(), ts=0.0)
    with locks.with_lock("job", lock_dir=tmp_path, ttl_seconds=10):
        assert locks.is_locked("job", lock_dir=tmp_path)
    assert not locks.is_locked("job", lock_dir=tmp_path)


def test_live_recent_lock_is_not_broken(tmp_path: Path) -> None:
    # Owner is this (live) process with a fresh timestamp -> must NOT be broken.
    _plant_lock(tmp_path, "job", pid=os.getpid(), ts=time.time())
    with pytest.raises(LockError):
        with locks.with_lock("job", lock_dir=tmp_path, ttl_seconds=100000):
            pass


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only pid liveness check")
def test_dead_pid_lock_is_broken(tmp_path: Path) -> None:
    # Recent timestamp but a dead PID (SIGKILL/OOM scenario) -> still broken.
    _plant_lock(tmp_path, "job", pid=2_147_480_000, ts=time.time())
    with locks.with_lock("job", lock_dir=tmp_path, ttl_seconds=100000):
        assert locks.is_locked("job", lock_dir=tmp_path)
