"""Read-only NVIDIA GPU stats via ``nvidia-smi`` (no direct subprocess).

Returns ``None`` whenever ``nvidia-smi`` is missing or the output cannot be
parsed, so callers can treat "no GPU" and "GPU busy/unavailable" identically.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from adapters import command
from adapters.command import CommandResult

Runner = Callable[[Sequence[str]], CommandResult]

_QUERY = "utilization.gpu,memory.used,memory.total,temperature.gpu"
_FIELDS = 4


@dataclass(frozen=True)
class GpuStats:
    util_pct: int
    vram_used_mb: int
    vram_total_mb: int
    temp_c: int


def gpu_stats(*, runner: Runner = command.run) -> GpuStats | None:
    """First GPU's live stats, or ``None`` on any failure (never raises)."""
    result = runner(
        [
            "nvidia-smi",
            f"--query-gpu={_QUERY}",
            "--format=csv,noheader,nounits",
        ]
    )
    if not result.ok:
        return None
    line = next((ln for ln in result.stdout.splitlines() if ln.strip()), "")
    if not line:
        return None
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < _FIELDS:
        return None
    try:
        util, used, total, temp = (int(parts[i]) for i in range(_FIELDS))
    except ValueError:
        return None
    return GpuStats(util_pct=util, vram_used_mb=used, vram_total_mb=total, temp_c=temp)
