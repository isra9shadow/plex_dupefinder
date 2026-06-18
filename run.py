"""izumi CLI — the single entrypoint. Modules self-register; dispatch is by name.

Usage:
    python run.py health [--config PATH]
    python run.py <module|pipeline> [--config PATH] [--dry-run | --audit]
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Sequence
from pathlib import Path

from core import config as config_mod
from core import locks as locks_mod
from core import logging as log_mod
from core import registry
from core import report as report_mod
from core.config import Config
from core.errors import LockError
from core.fs import Fs
from core.notify import Notifier
from core.safety import SafetyPolicy
from core.types import EventKind, NotificationEvent, RunContext, RunReport, SafetyMode


def build_context(cfg: Config, *, run_id: str | None = None) -> RunContext:
    rid = run_id or log_mod.new_run_id()
    log_mod.configure(
        level=cfg.logging.level,
        log_dir=cfg.logging.dir,
        run_id=rid,
        rotate_mb=cfg.logging.rotate_mb,
        backups=cfg.logging.backups,
        console=False,
    )
    mode = cfg.mode
    return RunContext(
        run_id=rid,
        mode=mode,
        config=cfg,
        logger=log_mod.get_logger("run"),
        fs=Fs(cfg, dry_run=mode != SafetyMode.LIVE),
        safety=SafetyPolicy(cfg),
        notify=Notifier(cfg),
    )


def health_check(ctx: RunContext) -> int:
    checks = {
        "logging_dir": ctx.config.logging.dir.exists(),
        "quarantine_parent": ctx.config.safety.quarantine_dir.parent.exists(),
    }
    for name, ok in checks.items():
        ctx.logger.info("health", check=name, ok=str(ok))
    return 0 if all(checks.values()) else 1


def _run_module(ctx: RunContext, name: str) -> int:
    fn = registry.get(name)
    if fn is None:
        ctx.logger.error("unknown module", module=name)
        return 2
    started = time.time()
    try:
        with locks_mod.with_lock(name):
            result = fn(ctx)
    except LockError:
        ctx.logger.warning("module already running", module=name)
        return 0
    report = RunReport(
        run_id=ctx.run_id,
        module=name,
        mode=ctx.mode,
        started=started,
        finished=time.time(),
        failures=result.failures,
        metrics={
            "actions": float(result.actions),
            "quarantined": float(result.quarantined),
            **result.metrics,
        },
    )
    report_mod.write_report(report, ctx.config.reporting.dir)
    kind = EventKind.RUN_OK if result.ok else EventKind.RUN_FAIL
    ctx.notify.send(
        NotificationEvent(
            kind=kind, module=name, run_id=ctx.run_id, title="ok" if result.ok else "failed"
        )
    )
    return 0 if result.ok else 1


def _run_pipeline(ctx: RunContext, pipeline: str, modules: Sequence[str]) -> int:
    ctx.logger.info("pipeline start", pipeline=pipeline, modules=",".join(modules))
    rc = 0
    for name in modules:
        rc = _run_module(ctx, name) or rc
    return rc


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="run.py", description="izumi homelab platform")
    parser.add_argument("target", help="module | pipeline | health")
    parser.add_argument("--config", default=None, help="path to config.json")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="force DRY_RUN")
    group.add_argument("--audit", action="store_true", help="force AUDIT")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(argv)
    cfg = config_mod.load(Path(args.config) if args.config else None)
    if args.dry_run:
        cfg = config_mod.with_mode(cfg, SafetyMode.DRY_RUN)
    elif args.audit:
        cfg = config_mod.with_mode(cfg, SafetyMode.AUDIT)
    registry.discover()
    ctx = build_context(cfg)
    target: str = args.target
    if target == "health":
        return health_check(ctx)
    if registry.get(target) is not None:
        return _run_module(ctx, target)
    if target in cfg.pipelines:
        return _run_pipeline(ctx, target, cfg.pipelines[target])
    ctx.logger.error("unknown target", target=target)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
