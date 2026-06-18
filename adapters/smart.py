"""Read-only SMART health via ``smartctl -a -j`` (JSON).

smartctl's exit code is a bitmask (often non-zero even on healthy disks), so we
rely on the presence of parseable JSON rather than ``CommandResult.ok``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from adapters import command
from adapters.command import CommandResult

Runner = Callable[[Sequence[str]], CommandResult]

_REALLOCATED_ID = 5
_PENDING_ID = 197


@dataclass(frozen=True)
class SmartInfo:
    device: str
    model: str
    serial: str
    health_passed: bool
    reallocated: int
    pending: int
    temperature: int
    warning: bool


def _as_dict(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _intval(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _attr_raw(data: dict[str, object], attr_id: int) -> int:
    table = _as_list(_as_dict(data.get("ata_smart_attributes")).get("table"))
    for entry in table:
        attr = _as_dict(entry)
        if attr.get("id") == attr_id:
            return _intval(_as_dict(attr.get("raw")).get("value"))
    return 0


def smart_for(device: str, runner: Runner = command.run) -> SmartInfo | None:
    result = runner(["smartctl", "-a", "-j", device])
    if not result.stdout.strip():
        return None
    try:
        data = _as_dict(json.loads(result.stdout))
    except json.JSONDecodeError:
        return None
    health_passed = _as_dict(data.get("smart_status")).get("passed") is True
    reallocated = _attr_raw(data, _REALLOCATED_ID)
    pending = _attr_raw(data, _PENDING_ID)
    temperature = _intval(_as_dict(data.get("temperature")).get("current"))
    warning = (not health_passed) or reallocated > 0 or pending > 0
    return SmartInfo(
        device=device,
        model=_str(data, "model_name"),
        serial=_str(data, "serial_number"),
        health_passed=health_passed,
        reallocated=reallocated,
        pending=pending,
        temperature=temperature,
        warning=warning,
    )
