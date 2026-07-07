"""Capacity doctor — forecast days-until-full from a disk-usage trend (read-only).

For each configured path it records used-bytes over time (its own tiny history) and
fits a linear trend to estimate **days until the filesystem is full**, plus the
current usage %. It flags a path that is already above ``warn_percent`` OR trending
to full within ``warn_days``. Proactive: warns before the array/cache actually fills.

Config (config.json), under ``integrations.capacitydoctor``:
  paths          : list of {name, path}
  warn_percent   : warn at/above this usage % (default 90)
  warn_days      : warn if projected-days-to-full is below this (default 14)
  history_points : trend window, most-recent points (default 12)

Strictly read-only (INVARIANT I1): reads ``shutil.disk_usage`` + a JSON ledger,
writes only its report.

Metrics: ``checked``, ``at_risk`` (over %-threshold or trending to full).
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass

from core.cache import Cache
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext

# (path) -> (total, used, free) bytes. Injected so tests don't touch the real FS.
UsageFn = Callable[[str], "tuple[int, int, int]"]

_DEFAULT_WARN_PCT = 90.0
_DEFAULT_WARN_DAYS = 14.0
_DEFAULT_HISTORY = 12
_LEDGER_KEY = "history"  # {path: [[ts, used_bytes], ...]}


@dataclass(frozen=True)
class PathFinding:
    name: str
    path: str
    status: str  # ok | full_soon | over_threshold | error
    percent: float
    days_to_full: float  # -1 = not enough data / never filling
    detail: str = ""


def default_usage(path: str) -> tuple[int, int, int]:
    """Real ``shutil.disk_usage`` → (total, used, free)."""
    u = shutil.disk_usage(path)
    return u.total, u.used, u.free


def days_to_full(points: list[tuple[float, float]], total: float) -> float:
    """Linear-trend estimate of days until ``used`` reaches ``total`` (-1 if never).

    Needs ≥2 points; fits used = a·t + b by least squares. A non-positive slope
    (usage flat or shrinking) means "not filling" → -1. Pure and deterministic.
    """
    if len(points) < 2 or total <= 0:
        return -1.0
    n = float(len(points))
    sx = sum(t for t, _ in points)
    sy = sum(u for _, u in points)
    sxx = sum(t * t for t, _ in points)
    sxy = sum(t * u for t, u in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return -1.0
    slope = (n * sxy - sx * sy) / denom  # bytes per second
    if slope <= 0:
        return -1.0
    _last_t, last_used = points[-1]
    remaining = total - last_used
    if remaining <= 0:
        return 0.0
    return (remaining / slope) / 86400.0


def evaluate(
    name: str,
    path: str,
    usage: tuple[int, int, int],
    history: list[tuple[float, float]],
    *,
    warn_pct: float,
    warn_days: float,
) -> PathFinding:
    """Classify a path from its current usage + trend (pure)."""
    total, used, _free = usage
    percent = (used / total * 100.0) if total > 0 else 0.0
    dtf = days_to_full(history, float(total))
    if percent >= warn_pct:
        status = "over_threshold"
    elif 0 <= dtf <= warn_days:
        status = "full_soon"
    else:
        status = "ok"
    detail = ""
    if 0 <= dtf < 1e6:
        detail = f"proyección: {dtf:.1f} días para llenarse"
    return PathFinding(name, path, status, round(percent, 1), round(dtf, 1), detail)


def _paths(ctx: RunContext) -> list[tuple[str, str]]:
    cfg = ctx.config.integrations.get("capacitydoctor", {})
    raw = cfg.get("paths")
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        name = entry.get("name")
        label = name if isinstance(name, str) and name else path
        out.append((label, path.strip()))
    return out


def _load_history(cache: Cache, path: str, limit: int) -> list[tuple[float, float]]:
    raw = cache.get(_LEDGER_KEY)
    store = raw if isinstance(raw, dict) else {}
    rows = store.get(path)
    out: list[tuple[float, float]] = []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, list) and len(row) == 2:
                try:
                    out.append((float(row[0]), float(row[1])))
                except (TypeError, ValueError):
                    continue
    return out[-limit:]


def _save_history(cache: Cache, path: str, history: list[tuple[float, float]], limit: int) -> None:
    raw = cache.get(_LEDGER_KEY)
    store = dict(raw) if isinstance(raw, dict) else {}
    store[path] = [[t, u] for t, u in history[-limit:]]
    cache.set(_LEDGER_KEY, store)


def _write_report(ctx: RunContext, findings: list[PathFinding], note: str) -> None:
    out_dir = ctx.config.reporting.dir / "capacitydoctor"
    out_dir.mkdir(parents=True, exist_ok=True)
    at_risk = [f for f in findings if f.status in ("full_soon", "over_threshold")]
    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "checked": len(findings),
                "at_risk": len(at_risk),
                "note": note,
                "paths": [
                    {
                        "name": f.name,
                        "path": f.path,
                        "status": f.status,
                        "percent": f.percent,
                        "days_to_full": f.days_to_full,
                        "detail": f.detail,
                    }
                    for f in findings
                ],
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    lines = [
        "# Capacitydoctor — capacidad de disco + previsión (solo lectura)",
        "",
        f"Comprobados: {len(findings)} · en riesgo: {len(at_risk)}",
        "",
    ]
    for f in sorted(findings, key=lambda x: -x.percent):
        proj = f" · {f.detail}" if f.detail else ""
        lines.append(f"- [{f.status}] {f.name} ({f.path}) · {f.percent:g}%{proj}")
    if not findings:
        lines.append("(sin rutas configuradas — integrations.capacitydoctor.paths)")
    if note:
        lines += ["", f"> {note}"]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


@register("capacitydoctor")
def run(
    ctx: RunContext, *, usage: UsageFn = default_usage, now: float | None = None
) -> ModuleResult:
    """Record disk usage, fit a trend, and flag paths filling up (read-only)."""
    result = ModuleResult(module="capacitydoctor", run_id=ctx.run_id, mode=ctx.mode)
    cfg = ctx.config.integrations.get("capacitydoctor", {})
    warn_pct = (
        float(cfg.get("warn_percent"))  # type: ignore[arg-type]
        if isinstance(cfg.get("warn_percent"), int | float)
        and not isinstance(cfg.get("warn_percent"), bool)
        else _DEFAULT_WARN_PCT
    )
    warn_days = (
        float(cfg.get("warn_days"))  # type: ignore[arg-type]
        if isinstance(cfg.get("warn_days"), int | float)
        and not isinstance(cfg.get("warn_days"), bool)
        else _DEFAULT_WARN_DAYS
    )
    limit = cfg.get("history_points")
    hist_limit = (
        limit
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 1
        else _DEFAULT_HISTORY
    )
    when = now if now is not None else time.time()

    cache = Cache(ctx.config.reporting.dir / "cache" / "capacity.json")
    findings: list[PathFinding] = []
    for name, path in _paths(ctx):
        try:
            u = usage(path)
        except OSError as exc:
            findings.append(PathFinding(name, path, "error", -1.0, -1.0, str(exc)))
            result.add_failure(
                FailureRecord(category="integration", message=f"{name}: {exc}", src=path)
            )
            continue
        history = _load_history(cache, path, hist_limit)
        history.append((when, float(u[1])))  # append current used-bytes
        _save_history(cache, path, history, hist_limit)
        finding = evaluate(name, path, u, history, warn_pct=warn_pct, warn_days=warn_days)
        findings.append(finding)
        if finding.status in ("full_soon", "over_threshold"):
            result.add_failure(
                FailureRecord(
                    category="integration",
                    message=f"{name}: {finding.status} ({finding.percent}%, {finding.detail})",
                    src=path,
                )
            )
    cache.save()

    note = (
        ""
        if findings
        else "No paths configured — set integrations.capacitydoctor.paths ({name, path})."
    )
    _write_report(ctx, findings, note)
    at_risk = sum(1 for f in findings if f.status in ("full_soon", "over_threshold"))
    ctx.logger.info("capacitydoctor done", checked=len(findings), at_risk=at_risk)
    result.metrics["checked"] = float(len(findings))
    result.metrics["at_risk"] = float(at_risk)
    result.actions = 0
    return result
