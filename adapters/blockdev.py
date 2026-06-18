"""Read-only block device + filesystem introspection (lsblk, df)."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from adapters import command
from adapters.command import CommandResult

Runner = Callable[[Sequence[str]], CommandResult]


@dataclass(frozen=True)
class BlockDevice:
    name: str
    size_bytes: int
    type: str
    mountpoint: str
    fstype: str
    model: str
    rotational: bool


@dataclass(frozen=True)
class MountUsage:
    target: str
    total: int
    used: int
    avail: int

    @property
    def percent(self) -> int:
        return round(self.used / self.total * 100) if self.total > 0 else 0


def _as_dict(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _int(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _to_device(raw: object) -> BlockDevice:
    data = _as_dict(raw)
    return BlockDevice(
        name=_str(data, "name"),
        size_bytes=_int(data, "size"),
        type=_str(data, "type"),
        mountpoint=_str(data, "mountpoint"),
        fstype=_str(data, "fstype"),
        model=_str(data, "model").strip(),
        rotational=data.get("rota") in (True, 1, "1"),
    )


def lsblk_devices(runner: Runner = command.run) -> list[BlockDevice]:
    result = runner(["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,MODEL,ROTA"])
    if not result.ok:
        return []
    try:
        data = _as_dict(json.loads(result.stdout))
    except json.JSONDecodeError:
        return []
    return [_to_device(dev) for dev in _as_list(data.get("blockdevices"))]


def disk_usage(runner: Runner = command.run) -> list[MountUsage]:
    result = runner(["df", "-B1", "--output=target,size,used,avail"])
    if not result.ok:
        return []
    usages: list[MountUsage] = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        target, size, used, avail = " ".join(parts[:-3]), parts[-3], parts[-2], parts[-1]
        if size.isdigit() and used.isdigit() and avail.isdigit():
            usages.append(MountUsage(target, int(size), int(used), int(avail)))
    return usages
