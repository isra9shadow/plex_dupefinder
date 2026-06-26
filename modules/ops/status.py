"""Status snapshot — a quick, read-only "how is the homelab right now?" probe.

Gathers a compact one-screen picture of the host by REUSING the existing
adapters/providers (no new probing logic, no subprocess of its own):

  * CPU load + RAM  — ``adapters.sysstat.system_stats`` (reads /proc).
  * GPU + VRAM      — ``adapters.gpu.gpu_stats`` (nvidia-smi via the command adapter).
  * Disks + SMART + mounts — the cached ``reports/inventory/disk_inventory.json``
    (same source the ``InventoryContextProvider`` consumes; no hardware scan here).
  * Docker containers NOT running — ``adapters.docker.list_containers``.

Each source degrades independently: a missing GPU, an unreachable docker or an
absent inventory cache simply drops its section instead of aborting the run (a
single sub-failure is recorded via ``result.add_failure`` but never raised).

The pure :func:`status_text` renders the snapshot as a compact Spanish, ASCII
(no accents) summary that fits one Telegram screen — the bot's ``/estado`` builds
the exact same text from the exact same snapshot.

This module is strictly READ-ONLY (INVARIANT I1): it never moves, deletes or
modifies any host file or media. The only thing it writes is its own report
under ``reporting.dir / "status"`` (summary.md + plan.json).

Config (config.json):
  integrations.status :
    ignore_containers : container names that exit normally (batch / one-shot
                        dockers such as Configarr, recyclarr, watchtower) and must
                        NOT be listed as "not running".
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from adapters import docker
from adapters.docker import ContainerInfo
from adapters.gpu import GpuStats, gpu_stats
from adapters.sysstat import SysStats, system_stats
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext

# Injected I/O so tests stay deterministic and offline.
GpuSource = Callable[[], GpuStats | None]
SysSource = Callable[[], SysStats | None]
ContainersSource = Callable[[], list[ContainerInfo]]
InventoryLoader = Callable[[], dict[str, object] | None]


@dataclass(frozen=True)
class DiskWarning:
    """A single disk flagged with a SMART warning."""

    name: str


@dataclass(frozen=True)
class MountUsage:
    """Filesystem usage for one mount point (from the cached inventory)."""

    target: str
    percent: float


@dataclass(frozen=True)
class StatusSnapshot:
    """Everything :func:`status_text` needs — fully assembled, no I/O."""

    sys: SysStats | None = None
    gpu: GpuStats | None = None
    containers_total: int = 0
    containers_not_running: list[str] = field(default_factory=list)
    docker_reachable: bool = True
    disk_count: int = 0
    smart_warnings: list[DiskWarning] = field(default_factory=list)
    mounts: list[MountUsage] = field(default_factory=list)
    inventory_present: bool = True
    notes: list[str] = field(default_factory=list)


def _str_list(raw: object) -> list[str]:
    """Coerce a config value into a clean list of non-empty strings."""
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def _as_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def load_disk_inventory(reports_dir: Path) -> dict[str, object] | None:
    """Read the cached ``inventory/disk_inventory.json`` (or None if absent/bad).

    This is the same artifact the ``InventoryContextProvider`` consumes — we read
    the cache rather than re-scanning hardware, keeping the snapshot fast and the
    module free of any privileged disk access.
    """
    path = Path(reports_dir) / "inventory" / "disk_inventory.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _disk_section(
    data: dict[str, object] | None,
) -> tuple[int, list[DiskWarning], list[MountUsage]]:
    """Extract (disk_count, SMART warnings, mount usages) from the inventory dict."""
    if data is None:
        return 0, [], []
    disks = _as_list(data.get("disks"))
    warnings: list[DiskWarning] = []
    for raw in disks:
        entry = _as_dict(raw)
        if entry.get("smart_warning") is True:
            name = _as_str(entry.get("name"))
            if name:
                warnings.append(DiskWarning(name=name))
    mounts: list[MountUsage] = []
    for raw in _as_list(data.get("mounts")):
        mount = _as_dict(raw)
        target = _as_str(mount.get("target"))
        percent = mount.get("percent")
        if target and isinstance(percent, int | float) and not isinstance(percent, bool):
            mounts.append(MountUsage(target=target, percent=float(percent)))
    return len(disks), warnings, mounts


def gather_snapshot(
    reports_dir: Path,
    *,
    ignore_containers: set[str] | None = None,
    sys: SysSource = system_stats,
    gpu: GpuSource = gpu_stats,
    containers: ContainersSource = docker.list_containers,
    inventory: InventoryLoader | None = None,
) -> StatusSnapshot:
    """Assemble a :class:`StatusSnapshot` from the injected sources (best-effort).

    Every source is optional: a returning ``None``/empty source just leaves its
    section out. ``ignore_containers`` (case-insensitive) drops batch/one-shot
    dockers that exit normally so a healthy one-shot is never reported as down.
    """
    ignore = {name.lower() for name in (ignore_containers or set())}
    loader = inventory if inventory is not None else (lambda: load_disk_inventory(reports_dir))
    notes: list[str] = []

    sys_stats = sys()
    gpu_stats_value = gpu()

    container_list = containers()
    docker_reachable = bool(container_list)
    not_running: list[str] = []
    for info in container_list:
        if info.state == "running":
            continue
        if info.name.lower() in ignore:
            continue
        not_running.append(info.name)
    not_running.sort(key=str.lower)
    if not docker_reachable:
        notes.append("Docker no accesible (binario ausente o /var/run/docker.sock sin montar).")

    inventory_data = loader()
    inventory_present = inventory_data is not None
    disk_count, warnings, mounts = _disk_section(inventory_data)
    if not inventory_present:
        notes.append("Sin inventario de discos en cache (ejecuta el modulo disk_inventory).")

    return StatusSnapshot(
        sys=sys_stats,
        gpu=gpu_stats_value,
        containers_total=len(container_list),
        containers_not_running=not_running,
        docker_reachable=docker_reachable,
        disk_count=disk_count,
        smart_warnings=warnings,
        mounts=mounts,
        inventory_present=inventory_present,
        notes=notes,
    )


def _join_capped(names: list[str], limit: int) -> str:
    """Join names with ', ', appending '...' when more than ``limit`` exist."""
    shown = ", ".join(names[:limit])
    if len(names) > limit:
        shown += ", ..."
    return shown


def status_text(snapshot: StatusSnapshot) -> str:
    """Render a compact Spanish, ASCII one-screen summary (pure: no I/O).

    Suitable to send verbatim to Telegram. No accents are used so it is safe on
    any terminal/encoding. The bot's ``/estado`` builds this exact text.
    """
    lines: list[str] = ["== ESTADO HOMELAB =="]

    if snapshot.sys is not None:
        lines.append(
            f"CPU load1: {snapshot.sys.load1:.2f} | RAM: {snapshot.sys.mem_used_pct}% usada"
        )
    else:
        lines.append("CPU/RAM: sin datos")

    if snapshot.gpu is not None:
        gpu = snapshot.gpu
        lines.append(
            f"GPU: {gpu.util_pct}% util | VRAM {gpu.vram_used_mb}/{gpu.vram_total_mb} MB"
            f" | {gpu.temp_c}C"
        )
    else:
        lines.append("GPU: sin datos")

    # Docker.
    if not snapshot.docker_reachable:
        lines.append("Docker: no accesible")
    elif snapshot.containers_not_running:
        count = len(snapshot.containers_not_running)
        lines.append(f"Docker: {snapshot.containers_total} contenedores, {count} parados")
        lines.append("  parados: " + _join_capped(snapshot.containers_not_running, 10))
    else:
        lines.append(f"Docker: {snapshot.containers_total} contenedores, todos arriba")

    # Disks + SMART.
    if not snapshot.inventory_present:
        lines.append("Discos: sin inventario en cache")
    elif snapshot.smart_warnings:
        warned = [w.name for w in snapshot.smart_warnings]
        lines.append(f"Discos: {snapshot.disk_count} | SMART warning en {len(warned)}")
        lines.append("  SMART: " + _join_capped(warned, 10))
    else:
        lines.append(f"Discos: {snapshot.disk_count} | SMART OK")

    # Mounts (key first: cache + array shares).
    if snapshot.mounts:
        rendered = [f"{m.target} {m.percent:.0f}%" for m in snapshot.mounts]
        lines.append("Mounts: " + _join_capped(rendered, 6))

    for note in snapshot.notes:
        lines.append(f"! {note}")

    return "\n".join(lines)


def _plan_payload(snapshot: StatusSnapshot) -> dict[str, object]:
    """Machine-readable snapshot for ``plan.json`` (stable, JSON-safe)."""
    return {
        "cpu": None
        if snapshot.sys is None
        else {"load1": snapshot.sys.load1, "ram_used_pct": snapshot.sys.mem_used_pct},
        "gpu": None
        if snapshot.gpu is None
        else {
            "util_pct": snapshot.gpu.util_pct,
            "vram_used_mb": snapshot.gpu.vram_used_mb,
            "vram_total_mb": snapshot.gpu.vram_total_mb,
            "temp_c": snapshot.gpu.temp_c,
        },
        "docker": {
            "reachable": snapshot.docker_reachable,
            "total": snapshot.containers_total,
            "not_running": snapshot.containers_not_running,
        },
        "disks": {
            "inventory_present": snapshot.inventory_present,
            "count": snapshot.disk_count,
            "smart_warnings": [w.name for w in snapshot.smart_warnings],
            "mounts": [{"target": m.target, "percent": m.percent} for m in snapshot.mounts],
        },
        "notes": snapshot.notes,
    }


def _write_report(ctx: RunContext, snapshot: StatusSnapshot) -> None:
    out_dir = ctx.config.reporting.dir / "status"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(
        json.dumps(_plan_payload(snapshot), indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    lines = [
        "# Status summary",
        "",
        "```",
        status_text(snapshot),
        "```",
        "",
    ]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


@register("status")
def run(
    ctx: RunContext,
    *,
    sys: SysSource = system_stats,
    gpu: GpuSource = gpu_stats,
    containers: ContainersSource = docker.list_containers,
    inventory: InventoryLoader | None = None,
) -> ModuleResult:
    result = ModuleResult(module="status", run_id=ctx.run_id, mode=ctx.mode)
    cfg = ctx.config.integrations.get("status", {})
    ignore = {name.lower() for name in _str_list(cfg.get("ignore_containers"))}

    snapshot = gather_snapshot(
        ctx.config.reporting.dir,
        ignore_containers=ignore,
        sys=sys,
        gpu=gpu,
        containers=containers,
        inventory=inventory,
    )

    # Notes are informational degradations (docker/inventory unavailable). Record
    # them as failures so the run is flagged, but they never abort the snapshot.
    for note in snapshot.notes:
        result.add_failure(FailureRecord(category="integration", message=note))

    _write_report(ctx, snapshot)
    ctx.logger.info(
        "status done",
        containers=snapshot.containers_total,
        containers_not_running=len(snapshot.containers_not_running),
        smart_warnings=len(snapshot.smart_warnings),
        docker_reachable=snapshot.docker_reachable,
    )
    result.metrics["containers_not_running"] = float(len(snapshot.containers_not_running))
    result.metrics["smart_warnings"] = float(len(snapshot.smart_warnings))
    result.actions = 0  # read-only
    return result
