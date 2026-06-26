"""Tests for modules.ops.status (read-only homelab status snapshot)."""

from __future__ import annotations

import json
from pathlib import Path

from adapters.docker import ContainerInfo
from adapters.gpu import GpuStats
from adapters.sysstat import SysStats
from modules.ops import status
from tests.fakes import make_context


def _container(name: str, state: str, restart_count: int = 0) -> ContainerInfo:
    return ContainerInfo(
        name=name,
        image="img",
        state=state,
        ports=[],
        networks=[],
        mounts=[],
        restart_count=restart_count,
    )


def _sys() -> SysStats:
    # 4 GB total, 1 GB available -> 75% used.
    return SysStats(mem_total_kb=4_000_000, mem_available_kb=1_000_000, load1=1.23)


def _gpu() -> GpuStats:
    return GpuStats(util_pct=42, vram_used_mb=3000, vram_total_mb=8000, temp_c=55)


def _read_plan(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "reports" / "status" / "plan.json").read_text(encoding="utf-8"))


def _read_summary(tmp_path: Path) -> str:
    return (tmp_path / "reports" / "status" / "summary.md").read_text(encoding="utf-8")


# --- pure status_text rendering ------------------------------------------------


def test_status_text_full_snapshot_is_ascii_and_compact() -> None:
    snapshot = status.StatusSnapshot(
        sys=_sys(),
        gpu=_gpu(),
        containers_total=5,
        containers_not_running=["radarr", "sonarr"],
        docker_reachable=True,
        disk_count=5,
        smart_warnings=[status.DiskWarning("disk3")],
        mounts=[status.MountUsage("/mnt/cache", 80.0), status.MountUsage("/mnt/user", 61.4)],
        inventory_present=True,
    )
    text = status.status_text(snapshot)

    assert text.isascii()  # no accents -> safe everywhere
    assert "CPU load1: 1.23 | RAM: 75% usada" in text
    assert "GPU: 42% util | VRAM 3000/8000 MB | 55C" in text
    assert "Docker: 5 contenedores, 2 parados" in text
    assert "parados: radarr, sonarr" in text
    assert "Discos: 5 | SMART warning en 1" in text
    assert "SMART: disk3" in text
    assert "/mnt/cache 80%" in text
    assert "/mnt/user 61%" in text


def test_status_text_all_healthy_no_warnings() -> None:
    snapshot = status.StatusSnapshot(
        sys=_sys(),
        gpu=_gpu(),
        containers_total=3,
        containers_not_running=[],
        docker_reachable=True,
        disk_count=5,
        smart_warnings=[],
        mounts=[],
        inventory_present=True,
    )
    text = status.status_text(snapshot)
    assert "Docker: 3 contenedores, todos arriba" in text
    assert "Discos: 5 | SMART OK" in text


def test_status_text_degraded_sources() -> None:
    snapshot = status.StatusSnapshot(
        sys=None,
        gpu=None,
        docker_reachable=False,
        inventory_present=False,
        notes=["Docker no accesible.", "Sin inventario."],
    )
    text = status.status_text(snapshot)
    assert "CPU/RAM: sin datos" in text
    assert "GPU: sin datos" in text
    assert "Docker: no accesible" in text
    assert "Discos: sin inventario en cache" in text
    assert "! Docker no accesible." in text
    assert "! Sin inventario." in text


def test_status_text_caps_long_lists() -> None:
    many = [f"c{i}" for i in range(15)]
    snapshot = status.StatusSnapshot(
        containers_total=15,
        containers_not_running=many,
        docker_reachable=True,
        inventory_present=True,
    )
    text = status.status_text(snapshot)
    assert "..." in text  # capped at 10 + ellipsis
    assert "c0" in text and "c9" in text
    assert "c14" not in text.split("parados: ")[1].split("\n")[0]


# --- gather_snapshot assembly --------------------------------------------------


def test_gather_snapshot_full() -> None:
    def containers() -> list[ContainerInfo]:
        return [
            _container("plex", "running"),
            _container("radarr", "exited"),
            _container("watchtower", "exited"),  # ignored one-shot
        ]

    inventory = {
        "disks": [
            {"name": "disk1", "smart_warning": False},
            {"name": "disk3", "smart_warning": True},
        ],
        "mounts": [
            {"target": "/mnt/cache", "percent": 80},
            {"target": "/mnt/user", "percent": 61.4},
            {"target": "/bad", "percent": "x"},  # malformed -> dropped
        ],
    }

    snapshot = status.gather_snapshot(
        Path("/unused"),
        ignore_containers={"watchtower"},
        sys=_sys,
        gpu=_gpu,
        containers=containers,
        inventory=lambda: inventory,
    )

    assert snapshot.sys is not None and snapshot.sys.load1 == 1.23
    assert snapshot.gpu is not None and snapshot.gpu.util_pct == 42
    assert snapshot.containers_total == 3
    assert snapshot.containers_not_running == ["radarr"]  # watchtower ignored, plex up
    assert snapshot.docker_reachable is True
    assert snapshot.disk_count == 2
    assert [w.name for w in snapshot.smart_warnings] == ["disk3"]
    assert [(m.target, m.percent) for m in snapshot.mounts] == [
        ("/mnt/cache", 80.0),
        ("/mnt/user", 61.4),
    ]
    assert snapshot.inventory_present is True
    assert snapshot.notes == []


def test_gather_snapshot_docker_unreachable_and_no_inventory() -> None:
    snapshot = status.gather_snapshot(
        Path("/unused"),
        sys=lambda: None,
        gpu=lambda: None,
        containers=list,  # no containers -> docker unreachable
        inventory=lambda: None,
    )
    assert snapshot.docker_reachable is False
    assert snapshot.inventory_present is False
    assert any("Docker no accesible" in n for n in snapshot.notes)
    assert any("inventario" in n for n in snapshot.notes)
    assert snapshot.containers_not_running == []


def test_gather_snapshot_sorts_not_running_case_insensitively() -> None:
    def containers() -> list[ContainerInfo]:
        return [
            _container("Zulu", "exited"),
            _container("alpha", "exited"),
            _container("Bravo", "exited"),
        ]

    snapshot = status.gather_snapshot(
        Path("/unused"),
        sys=lambda: None,
        gpu=lambda: None,
        containers=containers,
        inventory=lambda: None,
    )
    assert snapshot.containers_not_running == ["alpha", "Bravo", "Zulu"]


# --- run integration -----------------------------------------------------------


def test_run_writes_reports_and_metrics(tmp_path: Path) -> None:
    ctx = make_context(
        tmp_path,
        integrations={"status": {"ignore_containers": ["watchtower"]}},
    )

    def containers() -> list[ContainerInfo]:
        return [_container("plex", "running"), _container("radarr", "exited")]

    inventory = {
        "disks": [{"name": "disk3", "smart_warning": True}],
        "mounts": [{"target": "/mnt/cache", "percent": 80}],
    }

    result = status.run(
        ctx,
        sys=_sys,
        gpu=_gpu,
        containers=containers,
        inventory=lambda: inventory,
    )

    assert result.actions == 0  # read-only
    assert result.ok  # docker + inventory present -> no degradation notes
    assert result.metrics["containers_not_running"] == 1.0
    assert result.metrics["smart_warnings"] == 1.0

    plan = _read_plan(tmp_path)
    assert plan["docker"] == {"reachable": True, "total": 2, "not_running": ["radarr"]}
    assert plan["disks"]["smart_warnings"] == ["disk3"]
    assert plan["cpu"]["ram_used_pct"] == 75

    summary = _read_summary(tmp_path)
    assert "# Status summary" in summary
    assert "Docker: 2 contenedores, 1 parados" in summary
    assert "SMART warning en 1" in summary


def test_run_records_degradations_as_failures(tmp_path: Path) -> None:
    ctx = make_context(tmp_path)  # no integrations.status at all

    result = status.run(
        ctx,
        sys=lambda: None,
        gpu=lambda: None,
        containers=list,  # docker unreachable
        inventory=lambda: None,  # no inventory cache
    )

    assert result.actions == 0
    assert not result.ok  # degradations recorded as failures
    assert any("Docker no accesible" in f.message for f in result.failures)
    assert any("inventario" in f.message for f in result.failures)
    assert result.metrics["containers_not_running"] == 0.0


def test_run_loads_inventory_from_reports_dir_by_default(tmp_path: Path) -> None:
    # When no inventory loader is injected, run() reads the cached JSON from
    # reporting.dir/inventory/disk_inventory.json.
    ctx = make_context(tmp_path)
    inv_dir = tmp_path / "reports" / "inventory"
    inv_dir.mkdir(parents=True, exist_ok=True)
    (inv_dir / "disk_inventory.json").write_text(
        json.dumps(
            {
                "disks": [{"name": "disk1", "smart_warning": False}],
                "mounts": [{"target": "/mnt/user", "percent": 50}],
            }
        ),
        encoding="utf-8",
    )

    result = status.run(
        ctx,
        sys=_sys,
        gpu=lambda: None,
        containers=lambda: [_container("plex", "running")],
    )

    plan = _read_plan(tmp_path)
    assert plan["disks"]["inventory_present"] is True
    assert plan["disks"]["count"] == 1
    assert result.metrics["smart_warnings"] == 0.0


def test_load_disk_inventory_missing_and_malformed(tmp_path: Path) -> None:
    assert status.load_disk_inventory(tmp_path) is None  # missing file

    inv_dir = tmp_path / "inventory"
    inv_dir.mkdir(parents=True, exist_ok=True)
    (inv_dir / "disk_inventory.json").write_text("{not json", encoding="utf-8")
    assert status.load_disk_inventory(tmp_path) is None  # malformed -> None
