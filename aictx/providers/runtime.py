"""RuntimeContextProvider — a live, compact snapshot of host + docker state.

Unlike the inventory/system providers, this block is volatile (Tier.HIGH,
``stable=False``): CPU load, RAM/GPU pressure and which containers are unhealthy
right now. Each source degrades independently — a missing GPU or unreachable
docker just drops its line. If nothing at all is available the provider returns
``None`` so the builder can skip it.
"""

from __future__ import annotations

from collections.abc import Callable

from adapters import docker
from adapters.docker import ContainerInfo
from adapters.gpu import GpuStats, gpu_stats
from adapters.sysstat import SysStats, system_stats

from aictx.provider import ContextBlock, Tier

_TITLE = "ESTADO EN VIVO (runtime)"

GpuSource = Callable[[], GpuStats | None]
SysSource = Callable[[], SysStats | None]
ContainersSource = Callable[[], list[ContainerInfo]]


class RuntimeContextProvider:
    """Live host/docker snapshot as a volatile HIGH block."""

    name = "runtime"

    def __init__(
        self,
        *,
        gpu: GpuSource = gpu_stats,
        sys: SysSource = system_stats,
        containers: ContainersSource = docker.list_containers,
    ) -> None:
        self._gpu = gpu
        self._sys = sys
        self._containers = containers

    def _sys_line(self, stats: SysStats) -> str:
        return f"**Host:** load1 {stats.load1:.2f}, RAM {stats.mem_used_pct}% usada"

    def _gpu_line(self, stats: GpuStats) -> str:
        return (
            f"**GPU:** {stats.util_pct}% util, "
            f"VRAM {stats.vram_used_mb}/{stats.vram_total_mb} MB, "
            f"{stats.temp_c}°C"
        )

    def _docker_lines(self, containers: list[ContainerInfo]) -> list[str]:
        total = len(containers)
        not_running = [c for c in containers if c.state != "running"]
        summary = f"**Docker:** {total} contenedores, {len(not_running)} no 'running'"
        lines = [summary]
        if not_running:
            lines.append("- parados: " + ", ".join(sorted(c.name for c in not_running)))
        flapping = sorted(c.name for c in containers if c.restart_count > 0)
        if flapping:
            lines.append("- con reinicios: " + ", ".join(flapping))
        return lines

    def block(self) -> ContextBlock | None:
        parts: list[str] = []

        stats = self._sys()
        if stats is not None:
            parts.append(self._sys_line(stats))

        gpu = self._gpu()
        if gpu is not None:
            parts.append(self._gpu_line(gpu))

        containers = self._containers()
        if containers:
            parts.extend(self._docker_lines(containers))

        if not parts:
            return None

        return ContextBlock(
            name="runtime",
            title=_TITLE,
            body="\n".join(parts),
            tier=Tier.HIGH,
            stable=False,
        )
