"""Retention / growth control — keep the metrics store and report/log dirs bounded.

Read-only w.r.t. user data. The ONLY mutation is pruning old rows from the
persistent metrics store (a SQL ``DELETE``, not a file removal — so INVARIANT I1
"no ``rm`` in modules" is trivially satisfied; nothing is deleted through the
filesystem). It then measures the on-disk footprint of the report and log
directories and flags any that exceed their configured cap so unbounded growth
is caught early on the dashboard.

Why this exists: ``metrics.db`` gains one row per module per run and nothing else
prunes it, so at an hourly cadence it grows without bound. Logs are already size-
capped by the rotating handler; per-module reports overwrite in place. This module
closes the one real leak and reports the rest.

Config (config.json):
  integrations.retention :
    metrics_days   : keep this many days of metrics history (default 90)
    reports_max_mb : warn if reporting.dir exceeds this many MB (default 500)
    logs_max_mb    : warn if logging.dir exceeds this many MB (default 100)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from core.metrics import MetricsStore
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext

_DEFAULT_METRICS_DAYS = 90
_DEFAULT_REPORTS_MAX_MB = 500.0
_DEFAULT_LOGS_MAX_MB = 100.0


@dataclass(frozen=True)
class _Settings:
    metrics_days: int
    reports_max_mb: float
    logs_max_mb: float


def _num(raw: object, default: float) -> float:
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else default


def _settings(ctx: RunContext) -> _Settings:
    cfg = ctx.config.integrations.get("retention", {})
    days = _num(cfg.get("metrics_days"), _DEFAULT_METRICS_DAYS)
    return _Settings(
        metrics_days=int(days) if days > 0 else _DEFAULT_METRICS_DAYS,
        reports_max_mb=_num(cfg.get("reports_max_mb"), _DEFAULT_REPORTS_MAX_MB),
        logs_max_mb=_num(cfg.get("logs_max_mb"), _DEFAULT_LOGS_MAX_MB),
    )


def dir_size_bytes(path: Path) -> int:
    """Total size of every file under ``path`` (0 if absent; unreadable files skipped)."""
    if not path.is_dir():
        return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:  # a file that vanished / is unreadable never breaks the audit
            continue
    return total


def _mb(num_bytes: int) -> float:
    return round(num_bytes / (1024 * 1024), 1)


@register("retention")
def run(ctx: RunContext) -> ModuleResult:
    """Prune old metrics rows and report the report/log footprint (growth control)."""
    result = ModuleResult(module="retention", run_id=ctx.run_id, mode=ctx.mode)
    settings = _settings(ctx)
    reports = ctx.config.reporting.dir
    logs = ctx.config.logging.dir
    db = reports / "cache" / "metrics.db"

    pruned = 0
    note = ""
    if db.is_file():
        try:
            with MetricsStore(db) as store:
                pruned = store.prune(older_than_days=settings.metrics_days)
        except Exception as exc:  # pragma: no cover - defensive; never abort housekeeping
            note = f"could not prune metrics store: {exc}"
            result.add_failure(FailureRecord(category="integration", message=note))
    else:
        note = "no metrics.db yet — nothing to prune."

    reports_bytes = dir_size_bytes(reports)
    logs_bytes = dir_size_bytes(logs)
    reports_mb = _mb(reports_bytes)
    logs_mb = _mb(logs_bytes)
    db_mb = _mb(db.stat().st_size) if db.is_file() else 0.0

    # Compare on raw bytes (display MB is rounded, so a small cap wouldn't trigger).
    over: list[str] = []
    if settings.reports_max_mb > 0 and reports_bytes > settings.reports_max_mb * 1024 * 1024:
        msg = f"reports dir {reports_mb} MB > cap {settings.reports_max_mb} MB ({reports})"
        over.append(msg)
        result.add_failure(FailureRecord(category="capacity", message=msg))
    if settings.logs_max_mb > 0 and logs_bytes > settings.logs_max_mb * 1024 * 1024:
        msg = f"logs dir {logs_mb} MB > cap {settings.logs_max_mb} MB ({logs})"
        over.append(msg)
        result.add_failure(FailureRecord(category="capacity", message=msg))

    out_dir = reports / "retention"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "metrics_days": settings.metrics_days,
                "rows_pruned": pruned,
                "reports_mb": reports_mb,
                "logs_mb": logs_mb,
                "metrics_db_mb": db_mb,
                "reports_max_mb": settings.reports_max_mb,
                "logs_max_mb": settings.logs_max_mb,
                "over_cap": over,
                "note": note,
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    lines = [
        "# Retention — control de crecimiento (solo poda métricas)",
        "",
        f"Métricas retenidas: {settings.metrics_days} días · filas podadas: {pruned}",
        f"reports/: {reports_mb} MB (tope {settings.reports_max_mb} MB) · "
        f"metrics.db: {db_mb} MB",
        f"logs/: {logs_mb} MB (tope {settings.logs_max_mb} MB)",
    ]
    if over:
        lines += ["", "## Por encima del tope"]
        lines += [f"- {m}" for m in over]
    if note:
        lines += ["", f"> {note}"]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    ctx.logger.info("retention done", rows_pruned=pruned, reports_mb=reports_mb, logs_mb=logs_mb)
    result.metrics["rows_pruned"] = float(pruned)
    result.metrics["reports_mb"] = reports_mb
    result.metrics["logs_mb"] = logs_mb
    result.metrics["metrics_db_mb"] = db_mb
    result.actions = 0  # SQL-only prune; no filesystem mutation
    return result
