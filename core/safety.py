"""Safety policy inherited by every module (the shared gauntlet)."""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

from core.config import Config
from core.types import QuarantineEntry, SafetyMode


def _snapshot_sizes(paths: Sequence[Path]) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for path in paths:
        try:
            sizes[str(path)] = path.stat().st_size
        except OSError:
            sizes[str(path)] = -1
    return sizes


class SafetyPolicy:
    def __init__(self, config: Config) -> None:
        self._cfg = config.safety

    def resolve_mode(self) -> SafetyMode:
        return self._cfg.mode

    def min_age_ok(self, path: Path) -> bool:
        threshold = self._cfg.min_file_age_hours * 3600.0
        if threshold <= 0:
            return True
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        return age >= threshold

    def is_stable(self, paths: Sequence[Path], wait_seconds: float | None = None) -> bool:
        wait = self._cfg.stability_check_seconds if wait_seconds is None else wait_seconds
        before = _snapshot_sizes(paths)
        if wait > 0:
            time.sleep(wait)
        return before == _snapshot_sizes(paths)

    def size_ratio_ok(self, candidate_size: int, keeper_size: int) -> bool:
        if self._cfg.max_size_ratio <= 0 or keeper_size <= 0:
            return True
        return candidate_size <= keeper_size * self._cfg.max_size_ratio

    def passes_guards(
        self,
        paths: Sequence[Path],
        *,
        keeper_size: int | None = None,
        candidate_size: int | None = None,
    ) -> bool:
        if not all(self.min_age_ok(p) for p in paths):
            return False
        if not self.is_stable(paths):
            return False
        if keeper_size is not None and candidate_size is not None:
            return self.size_ratio_ok(candidate_size, keeper_size)
        return True

    def should_purge(self, entry: QuarantineEntry, retention_days: float | None = None) -> bool:
        days = self._cfg.retention_days if retention_days is None else retention_days
        if days <= 0:
            return False
        return (time.time() - entry.ts) >= days * 86400.0
