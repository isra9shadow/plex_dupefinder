"""Silent-disk-error detector (read-only).

Disks rarely die loudly: long before a drive fails outright, its SMART counters
start ticking up — reallocated sectors, pending sectors, a flipped overall health
verdict, a creeping temperature. These are the "silent errors" an operator never
notices until data is already gone. ``diskwatch`` reads SMART for every disk (via
``adapters/smart`` → ``adapters/command`` → ``smartctl -a -j``), evaluates the
degradation signals, and writes a clear per-disk report plus a WARN list of the
disks that need attention.

This module is strictly READ-ONLY (INVARIANT I1): it never moves, deletes or
modifies any host file or media — the only thing it writes is its own report
under ``reporting.dir / "diskwatch"``. A SMART read failing for one disk is
recorded via ``result.add_failure`` and does NOT abort the whole run.

Config (config.json):
  integrations.diskwatch :
    disks     : optional explicit device list (e.g. ["/dev/sda", "/dev/sdb"]);
                when omitted, disks are discovered via ``adapters/blockdev``.
    temp_warn : temperature (°C) at or above which a disk is flagged (default 50)
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from adapters import blockdev, smart
from adapters.smart import SmartInfo
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext

# Reader/lister are injected so tests need no real smartctl / lsblk / subprocess.
SmartReader = Callable[[str], SmartInfo | None]
DeviceLister = Callable[[], list[str]]

_DEFAULT_TEMP_WARN = 50  # °C — conservative warn threshold for spinning/SSD disks


@dataclass(frozen=True)
class DiskStatus:
    """Evaluated SMART status for a single disk + the reasons it was flagged."""

    device: str
    model: str
    serial: str
    health_passed: bool
    reallocated: int
    pending: int
    temperature: int
    reasons: list[str]

    @property
    def warning(self) -> bool:
        return bool(self.reasons)


def evaluate(info: SmartInfo, *, temp_warn: int) -> DiskStatus:
    """Turn raw SMART attributes into a status + human-readable warning reasons.

    Pure logic (no I/O): a disk is flagged when the overall SMART health verdict
    is not PASS, when any error counter is non-zero (reallocated / pending
    sectors — the classic silent-failure signals), or when the temperature is at
    or above the configured threshold.
    """
    reasons: list[str] = []
    if not info.health_passed:
        reasons.append("SMART overall health != PASS")
    if info.reallocated > 0:
        reasons.append(f"Reallocated_Sector_Ct = {info.reallocated}")
    if info.pending > 0:
        reasons.append(f"Current_Pending_Sector = {info.pending}")
    if temp_warn > 0 and info.temperature >= temp_warn:
        reasons.append(f"temperature {info.temperature}°C >= {temp_warn}°C")
    return DiskStatus(
        device=info.device,
        model=info.model,
        serial=info.serial,
        health_passed=info.health_passed,
        reallocated=info.reallocated,
        pending=info.pending,
        temperature=info.temperature,
        reasons=reasons,
    )


@dataclass(frozen=True)
class _Settings:
    disks: list[str] | None  # explicit device list, or None to auto-discover
    temp_warn: int


def _settings(ctx: RunContext) -> _Settings:
    """Read ``integrations.diskwatch`` into a typed settings object."""
    cfg = ctx.config.integrations.get("diskwatch", {})
    raw_disks = cfg.get("disks")
    disks = (
        [d for d in raw_disks if isinstance(d, str) and d] if isinstance(raw_disks, list) else None
    )
    raw_temp = cfg.get("temp_warn", _DEFAULT_TEMP_WARN)
    temp_warn = (
        raw_temp
        if isinstance(raw_temp, int) and not isinstance(raw_temp, bool) and raw_temp > 0
        else _DEFAULT_TEMP_WARN
    )
    return _Settings(disks=disks or None, temp_warn=temp_warn)


def discover_disks(lister: DeviceLister) -> list[str]:
    """Discover physical disk device paths (``/dev/<name>``) via the lister."""
    return lister()


def _default_device_lister() -> list[str]:
    """List physical disks via ``adapters/blockdev`` as ``/dev/<name>`` paths."""
    return [
        f"/dev/{device.name}"
        for device in blockdev.lsblk_devices()
        if device.type == "disk" and device.name
    ]


def _write_report(
    ctx: RunContext,
    statuses: list[DiskStatus],
    note: str,
) -> None:
    out_dir = ctx.config.reporting.dir / "diskwatch"
    out_dir.mkdir(parents=True, exist_ok=True)
    warnings = [s for s in statuses if s.warning]
    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "disks_scanned": len(statuses),
                "disks_with_warnings": len(warnings),
                "temp_warn": ctx.config.integrations.get("diskwatch", {}).get(
                    "temp_warn", _DEFAULT_TEMP_WARN
                ),
                "note": note,
                "disks": [
                    {
                        "device": s.device,
                        "model": s.model,
                        "serial": s.serial,
                        "health_passed": s.health_passed,
                        "reallocated": s.reallocated,
                        "pending": s.pending,
                        "temperature": s.temperature,
                        "warning": s.warning,
                        "reasons": s.reasons,
                    }
                    for s in statuses
                ],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    warn_lines: list[str] = []
    for status in sorted(warnings, key=lambda s: s.device):
        warn_lines.append(f"### {status.device} — {status.model or '?'}")
        warn_lines.extend(f"- {reason}" for reason in status.reasons)
        warn_lines.append("")
    per_disk = [
        f"- {status.device}: {status.model or '?'} · "
        f"health={'PASS' if status.health_passed else 'FAIL'} · "
        f"realloc={status.reallocated} · pending={status.pending} · "
        f"{status.temperature}°C" + ("  ⚠️" if status.warning else "")
        for status in sorted(statuses, key=lambda s: s.device)
    ]
    lines = [
        "# Diskwatch summary",
        "",
        f"Disks scanned: {len(statuses)}",
        f"Disks with warnings: {len(warnings)}",
        "",
        f"## Per disk ({len(statuses)})",
        *(per_disk or ["(ninguno — ¿smartctl accesible? discos visibles?)"]),
        "",
        "## Discos con avisos (errores silenciosos)",
        *(warn_lines or ["(ninguno — todos los discos sanos)"]),
    ]
    if note:
        lines += [f"> {note}", ""]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def scan(
    ctx: RunContext,
    *,
    smart_reader: SmartReader = smart.smart_for,
    device_lister: DeviceLister = _default_device_lister,
) -> ModuleResult:
    """Read-only SMART scan. I/O is injected so tests need no smartctl/lsblk.

    ``run`` is the registered entrypoint and simply delegates here with the real
    adapters bound; tests call ``scan`` directly with fakes.
    """
    result = ModuleResult(module="diskwatch", run_id=ctx.run_id, mode=ctx.mode)
    settings = _settings(ctx)

    devices = settings.disks if settings.disks is not None else discover_disks(device_lister)

    statuses: list[DiskStatus] = []
    note = ""
    for device in devices:
        try:
            info = smart_reader(device)
        except Exception as exc:  # one disk must not abort the whole run
            result.add_failure(
                FailureRecord(category="integration", message=f"{device}: SMART read failed: {exc}")
            )
            continue
        if info is None:
            # No parseable SMART JSON: record it but keep scanning the rest.
            result.add_failure(
                FailureRecord(
                    category="integration",
                    message=f"{device}: no SMART data (smartctl unavailable or unsupported)",
                )
            )
            continue
        statuses.append(evaluate(info, temp_warn=settings.temp_warn))

    warnings = [s for s in statuses if s.warning]
    if not devices:
        note = (
            "No disks to scan: none configured (integrations.diskwatch.disks) and "
            "none discovered. Is smartctl/lsblk available and is the host's block "
            "layer visible from the container?"
        )
        result.add_failure(FailureRecord(category="integration", message=note))
    elif not statuses:
        note = "No SMART data could be read for any disk."
    elif not warnings:
        note = "All scanned disks healthy — no silent SMART errors detected."

    _write_report(ctx, statuses, note)
    ctx.logger.info(
        "diskwatch done",
        disks=len(statuses),
        disks_with_warnings=len(warnings),
    )
    result.metrics["disks_with_warnings"] = float(len(warnings))
    result.actions = 0
    return result


@register("diskwatch")
def run(ctx: RunContext) -> ModuleResult:
    """Registered entrypoint: scan every disk's SMART health with real adapters."""
    return scan(ctx)
