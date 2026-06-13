"""Lightweight phase profiler (IMP-03) — measure where wall-clock goes.

Accumulates per-phase total time + call count; ``metrics()`` flattens it into the
report's ``metrics`` (e.g. ``probe_ms``, ``probe_count``) so every run shows where
discovery spends its time. Profile before optimising.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager


class Profiler:
    def __init__(self) -> None:
        self._totals: dict[str, float] = {}
        self._counts: dict[str, int] = {}

    def record(self, name: str, seconds: float) -> None:
        self._totals[name] = self._totals.get(name, 0.0) + seconds
        self._counts[name] = self._counts.get(name, 0) + 1

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(name, time.perf_counter() - start)

    def metrics(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for name, total in self._totals.items():
            out[f"{name}_ms"] = round(total * 1000.0, 1)
            out[f"{name}_count"] = float(self._counts[name])
        return out
