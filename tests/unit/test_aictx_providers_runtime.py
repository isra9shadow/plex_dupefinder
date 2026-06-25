"""Tests for aictx.providers.runtime (compact live block, injected fakes)."""

from __future__ import annotations

from adapters.docker import ContainerInfo
from adapters.gpu import GpuStats
from adapters.sysstat import SysStats
from aictx.provider import Tier
from aictx.providers.runtime import RuntimeContextProvider


def _container(name: str, state: str = "running", restart_count: int = 0) -> ContainerInfo:
    return ContainerInfo(
        name=name,
        image="img",
        state=state,
        ports=[],
        networks=[],
        mounts=[],
        restart_count=restart_count,
        started_at="",
    )


def test_compact_block_with_all_sources() -> None:
    provider = RuntimeContextProvider(
        gpu=lambda: GpuStats(util_pct=20, vram_used_mb=2000, vram_total_mb=8000, temp_c=55),
        sys=lambda: SysStats(mem_total_kb=1000, mem_available_kb=400, load1=0.5),
        containers=lambda: [
            _container("Plex"),
            _container("Sonarr", state="exited"),
            _container("Radarr", restart_count=4),
        ],
    )
    block = provider.block()
    assert block is not None
    assert block.name == "runtime"
    assert block.tier == Tier.HIGH
    assert block.stable is False
    body = block.body
    assert "load1 0.50" in body
    assert "RAM 60% usada" in body
    assert "20% util" in body
    assert "2000/8000 MB" in body
    assert "3 contenedores, 1 no 'running'" in body
    assert "parados: Sonarr" in body
    assert "con reinicios: Radarr" in body


def test_gpu_line_omitted_when_none() -> None:
    provider = RuntimeContextProvider(
        gpu=lambda: None,
        sys=lambda: SysStats(mem_total_kb=1000, mem_available_kb=500, load1=1.0),
        containers=lambda: [],
    )
    block = provider.block()
    assert block is not None
    assert "GPU" not in block.body
    assert "Host" in block.body


def test_docker_omitted_when_empty() -> None:
    provider = RuntimeContextProvider(
        gpu=lambda: None,
        sys=lambda: SysStats(mem_total_kb=1000, mem_available_kb=500, load1=1.0),
        containers=lambda: [],
    )
    block = provider.block()
    assert block is not None
    assert "Docker" not in block.body


def test_none_when_everything_unavailable() -> None:
    provider = RuntimeContextProvider(
        gpu=lambda: None,
        sys=lambda: None,
        containers=lambda: [],
    )
    assert provider.block() is None
