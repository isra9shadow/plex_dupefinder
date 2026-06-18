"""Disk inventory + SMART health (read-only).

Surfaces disks (model/type/size), filesystem usage and SMART health, flagging any
disk with reallocated/pending sectors or a failed health check — catch a failing
disk before data loss. Emits `reports/inventory/disk_inventory.{json,md}` (replaces
the hand-maintained homelab-infra disk docs).
"""

from __future__ import annotations

import json

from adapters import blockdev, smart
from adapters.blockdev import BlockDevice, MountUsage
from adapters.smart import SmartInfo
from core.registry import register
from core.types import ModuleResult, RunContext


def _disk_record(device: BlockDevice, info: SmartInfo | None) -> dict[str, object]:
    return {
        "name": device.name,
        "model": device.model,
        "size_bytes": device.size_bytes,
        "size_tb": round(device.size_bytes / 1e12, 2),
        "type": "HDD" if device.rotational else "SSD",
        "smart": None
        if info is None
        else {
            "health_passed": info.health_passed,
            "reallocated": info.reallocated,
            "pending": info.pending,
            "temperature": info.temperature,
        },
        "smart_warning": bool(info and info.warning),
    }


def _mount_record(usage: MountUsage) -> dict[str, object]:
    return {
        "target": usage.target,
        "total": usage.total,
        "used": usage.used,
        "avail": usage.avail,
        "percent": usage.percent,
    }


def _write_report(
    ctx: RunContext, disks: list[dict[str, object]], mounts: list[dict[str, object]]
) -> None:
    out_dir = ctx.config.reporting.dir / "inventory"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "disk_inventory.json").write_text(
        json.dumps({"disks": disks, "mounts": mounts}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    lines = ["# Disk Inventory", "", f"## Disks ({len(disks)})", ""]
    for disk in disks:
        flag = " ⚠️ SMART" if disk["smart_warning"] else ""
        lines.append(
            f"- {disk['name']} — {disk['model']} · {disk['type']} · {disk['size_tb']} TB{flag}"
        )
    lines += ["", f"## Mounts ({len(mounts)})", ""]
    lines += [f"- {m['target']} — {m['percent']}% used" for m in mounts]
    (out_dir / "disk_inventory.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


@register("disk_inventory")
def run(ctx: RunContext) -> ModuleResult:
    result = ModuleResult(module="disk_inventory", run_id=ctx.run_id, mode=ctx.mode)
    disks: list[dict[str, object]] = []
    warnings = 0
    for device in blockdev.lsblk_devices():
        if device.type != "disk":
            continue
        record = _disk_record(device, smart.smart_for(f"/dev/{device.name}"))
        disks.append(record)
        if record["smart_warning"]:
            warnings += 1
    mounts = [_mount_record(usage) for usage in blockdev.disk_usage()]
    _write_report(ctx, disks, mounts)
    ctx.logger.info("disk inventory", disks=len(disks), smart_warnings=warnings)
    result.actions = warnings
    return result
