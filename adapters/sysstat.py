"""Read-only host system stats from ``/proc`` (pure file reads, no subprocess)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_DEFAULT_PROC = Path("/proc")


@dataclass(frozen=True)
class SysStats:
    mem_total_kb: int
    mem_available_kb: int
    load1: float

    @property
    def mem_used_pct(self) -> int:
        if self.mem_total_kb <= 0:
            return 0
        used = self.mem_total_kb - self.mem_available_kb
        return round(used / self.mem_total_kb * 100)


def _meminfo_kb(text: str, key: str) -> int:
    """Parse a ``key:   <value> kB`` line from /proc/meminfo (raises on miss)."""
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name.strip() == key:
            return int(rest.split()[0])
    raise ValueError(f"missing {key}")


def system_stats(*, proc_root: Path = _DEFAULT_PROC) -> SysStats | None:
    """Memory totals plus the 1-minute load average, or ``None`` on any error."""
    try:
        meminfo = (proc_root / "meminfo").read_text(encoding="utf-8")
        loadavg = (proc_root / "loadavg").read_text(encoding="utf-8")
        mem_total = _meminfo_kb(meminfo, "MemTotal")
        mem_available = _meminfo_kb(meminfo, "MemAvailable")
        load1 = float(loadavg.split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return SysStats(mem_total_kb=mem_total, mem_available_kb=mem_available, load1=load1)
