"""InventoryContextProvider — recent docker + disk inventory as a compact block.

Reads the JSON already emitted by the inventory modules
(``reports/inventory/docker_inventory.json`` and ``disk_inventory.json``) and
summarises it: container counts + anomalies, disk/SMART counts and the key mount
usages. It never scans hardware or calls an adapter — it only consumes the cache.
Returns None when neither file exists (nothing to contribute).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aictx.provider import ContextBlock, Tier

_TITLE = "INVENTARIO (cache reciente)"
_NAME_LIMIT = 20
_KEY_MOUNTS = ("/mnt/cache", "/mnt/user")


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class InventoryContextProvider:
    """Summarises the cached docker + disk inventory JSON into one block."""

    name = "inventory"

    def __init__(self, reports_dir: Path) -> None:
        self._dir = Path(reports_dir) / "inventory"

    def _docker_lines(self, data: Any) -> list[str]:
        containers = _as_list(data)
        if not containers:
            return []
        not_running: list[str] = []
        for raw in containers:
            entry = _as_dict(raw)
            state = _as_str(entry.get("state")).lower()
            if state and state != "running":
                name = _as_str(entry.get("name"))
                if name:
                    not_running.append(name)
        line = f"- Contenedores: {len(containers)} ({len(not_running)} no running)"
        if not_running:
            shown = ", ".join(not_running[:_NAME_LIMIT])
            if len(not_running) > _NAME_LIMIT:
                shown += ", ..."
            line += f": {shown}"
        return [line]

    def _disk_lines(self, data: Any) -> list[str]:
        payload = _as_dict(data)
        disks = _as_list(payload.get("disks"))
        mounts = _as_list(payload.get("mounts"))
        if not disks and not mounts:
            return []
        warnings = sum(1 for raw in disks if _as_dict(raw).get("smart_warning") is True)
        lines = [f"- Discos: {len(disks)} ({warnings} con SMART warning)"]
        warned = [
            _as_str(_as_dict(raw).get("name"))
            for raw in disks
            if _as_dict(raw).get("smart_warning") is True
        ]
        warned = [n for n in warned if n]
        if warned:
            lines.append(f"  - SMART warning en: {', '.join(warned[:_NAME_LIMIT])}")
        key: list[str] = []
        other: list[str] = []
        for raw in mounts:
            mount = _as_dict(raw)
            target = _as_str(mount.get("target"))
            if not target:
                continue
            percent = mount.get("percent")
            entry = f"{target} {percent}%" if isinstance(percent, int | float) else target
            if target.startswith(_KEY_MOUNTS):
                key.append(entry)
            else:
                other.append(entry)
        shown_mounts = key or other
        if shown_mounts:
            lines.append(f"  - Mounts: {', '.join(shown_mounts[:_NAME_LIMIT])}")
        return lines

    def block(self) -> ContextBlock | None:
        docker = _load_json(self._dir / "docker_inventory.json")
        disk = _load_json(self._dir / "disk_inventory.json")
        if docker is None and disk is None:
            return None
        body_lines = self._docker_lines(docker) + self._disk_lines(disk)
        if not body_lines:
            return None
        return ContextBlock(
            name="inventory",
            title=_TITLE,
            body="\n".join(body_lines),
            tier=Tier.HIGH,
            stable=True,
        )
