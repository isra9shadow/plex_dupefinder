"""Plex DupeFinder integrated as a platform module — behavior-preserving (ADR-0011).

The proven, deployed root ``plex_dupefinder.py`` stays the source of truth for the
dedupe logic. This module orchestrates it through the command adapter so it runs
inside the single platform pipeline (`run.py plex_dupefinder`) with no behavior
change and no reimplementation. A native port onto core/* will follow later, gated
by parity tests (see MIGRATION_PLAN §9).

Safety boundary (CTO-01): the platform ``ctx.mode`` is propagated to the engine via
``IZUMI_EXECUTION_MODE`` (the engine reads it with priority over its config.json), so
``run.py plex_dupefinder --dry-run`` is actually honoured. LIVE maps to QUARANTINE,
never DELETE — the platform can never trigger the engine's irreversible direct-delete
path (invariant I1).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from adapters import command
from adapters.command import CommandResult
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext, SafetyMode

# The legacy script lives at the repo root (two levels up from modules/media/).
_SCRIPT = Path(__file__).resolve().parents[2] / "plex_dupefinder.py"
_TIMEOUT_SECONDS = 3600.0

# Platform mode -> engine EXECUTION_MODE. LIVE -> QUARANTINE (reversible) by design:
# the platform never authorises the engine's irreversible DELETE path.
_MODE_TO_EXECUTION = {
    SafetyMode.DRY_RUN: "AUDIT",
    SafetyMode.AUDIT: "AUDIT",
    SafetyMode.LIVE: "QUARANTINE",
}


def _write_report(ctx: RunContext, execution_mode: str, cmd: CommandResult) -> None:
    """Surface the legacy engine's outcome as a platform report so the panel/web
    'ver informe' works (the engine's own JSON/activity.log stay untouched)."""
    out_dir = ctx.config.reporting.dir / "plex_dupefinder"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(
        json.dumps(
            {"execution_mode": execution_mode, "returncode": cmd.returncode, "ok": cmd.ok},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    status = "OK ✅" if cmd.ok else "FALLO ❌"
    tail = "\n".join((cmd.stdout or "").splitlines()[-40:]) or "(sin salida)"
    lines = [
        "# Plex DupeFinder",
        "",
        f"Modo motor: {execution_mode} · returncode: {cmd.returncode} · {status}",
        "",
        "## Salida del motor (últimas líneas)",
        "```",
        tail,
        "```",
        "",
    ]
    if not cmd.ok and cmd.stderr.strip():
        lines += ["## Error", "```", "\n".join(cmd.stderr.splitlines()[-15:]), "```", ""]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


@register("plex_dupefinder")
def run(ctx: RunContext) -> ModuleResult:
    result = ModuleResult(module="plex_dupefinder", run_id=ctx.run_id, mode=ctx.mode)
    if not _SCRIPT.exists():
        ctx.logger.error("plex_dupefinder script not found", path=str(_SCRIPT))
        result.add_failure(FailureRecord(category="config", message=f"missing script: {_SCRIPT}"))
        return result

    execution_mode = _MODE_TO_EXECUTION.get(ctx.mode, "AUDIT")
    ctx.logger.info(
        "invoking plex_dupefinder", script=str(_SCRIPT), execution_mode=execution_mode
    )
    cmd = command.run(
        [sys.executable, str(_SCRIPT)],
        timeout=_TIMEOUT_SECONDS,
        env={"IZUMI_EXECUTION_MODE": execution_mode},
    )
    if cmd.ok:
        ctx.logger.info("plex_dupefinder finished", returncode=cmd.returncode)
    else:
        ctx.logger.error(
            "plex_dupefinder failed", returncode=cmd.returncode, stderr=cmd.stderr[-500:]
        )
        result.add_failure(
            FailureRecord(category="integration", message=f"plex_dupefinder exit {cmd.returncode}")
        )
    _write_report(ctx, execution_mode, cmd)
    result.metrics["returncode"] = float(cmd.returncode)
    return result
