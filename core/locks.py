"""Single-instance job locks via atomic directory creation (portable).

A lock records its owner (pid + timestamp) in ``owner.json``. If a later run
finds the lock held by a **dead** process (POSIX liveness check) or one that has
**exceeded the TTL**, it breaks the stale lock and proceeds — so a process killed
by SIGKILL/OOM/power-loss (a real Unraid scenario) can no longer wedge every
future run into a silent "already running" no-op (CTO-13).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from core.errors import LockError

# A crashed holder frees after this; set well above any real job runtime
# (the dupefinder wrapper caps at 3600 s) so a live job is never broken.
_DEFAULT_TTL_SECONDS = 6 * 3600.0
_OWNER = "owner.json"


def _default_lock_dir() -> Path:
    return Path(tempfile.gettempdir()) / "izumi-locks"


def is_locked(name: str, *, lock_dir: Path | None = None) -> bool:
    base = lock_dir or _default_lock_dir()
    return (base / f"{name}.lock").exists()


def _read_owner(lockpath: Path) -> dict[str, object]:
    try:
        data: object = json.loads((lockpath / _OWNER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness. On non-POSIX we cannot prove death, so assume alive
    and let the TTL break the lock instead."""
    if os.name != "posix" or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists but e.g. no permission
    return True


def _is_stale(lockpath: Path, ttl_seconds: float) -> bool:
    owner = _read_owner(lockpath)
    pid = owner.get("pid")
    ts = owner.get("ts")
    if isinstance(pid, int) and not isinstance(pid, bool) and not _pid_alive(pid):
        return True
    if isinstance(ts, int | float) and not isinstance(ts, bool):
        return (time.time() - float(ts)) > ttl_seconds
    return False


def _clear(lockpath: Path) -> None:
    (lockpath / _OWNER).unlink(missing_ok=True)
    try:
        lockpath.rmdir()
    except OSError:
        pass


@contextmanager
def with_lock(
    name: str, *, lock_dir: Path | None = None, ttl_seconds: float = _DEFAULT_TTL_SECONDS
) -> Iterator[None]:
    """Hold an exclusive lock for ``name`` or raise ``LockError`` if held by a
    live, non-expired owner. A dead/expired lock is broken and reacquired."""
    base = lock_dir or _default_lock_dir()
    base.mkdir(parents=True, exist_ok=True)
    lockpath = base / f"{name}.lock"
    try:
        lockpath.mkdir()  # atomic on all platforms
    except FileExistsError as exc:
        if not _is_stale(lockpath, ttl_seconds):
            raise LockError(f"job already running: {name}") from exc
        _clear(lockpath)  # break a dead/expired lock, then reacquire once
        try:
            lockpath.mkdir()
        except FileExistsError as exc2:  # lost a race to break it
            raise LockError(f"job already running: {name}") from exc2
    try:
        (lockpath / _OWNER).write_text(
            json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8"
        )
    except OSError:
        pass
    try:
        yield
    finally:
        _clear(lockpath)
