"""Prometheus textfile export — expose izumi metrics to your existing Grafana.

Read-only module: reads the persistent metrics store (populated by run.py after
every module run) and writes ``reports/izumi.prom`` in the node_exporter
*textfile collector* format. Point node_exporter's ``--collector.textfile.directory``
at ``reports/`` (or copy the file there) and Grafana scrapes it — no extra server.

Emits, for the latest run of each module:
  * ``izumi_module_ok{module}``        — 1 if the last run succeeded, else 0
  * ``izumi_module_failures{module}``  — number of FailureRecords
  * ``izumi_module_metric{module,metric}`` — latest value of each numeric metric

Strictly read-only (INVARIANT I1): writes only its own report + the .prom file.
"""

from __future__ import annotations

import json

from core.metrics import MetricsStore
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext


def _escape_label(value: str) -> str:
    """Escape a Prometheus label value (backslash, quote, newline)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _num(value: object) -> float:
    """Coerce a store value to float (0.0 for anything non-numeric)."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def render_prometheus(status: list[dict[str, object]], metrics: list[dict[str, object]]) -> str:
    """Render the textfile-collector exposition (pure; deterministic order)."""
    lines: list[str] = [
        "# HELP izumi_module_ok Last run of the module succeeded (1) or failed (0).",
        "# TYPE izumi_module_ok gauge",
    ]
    for s in status:
        module = _escape_label(str(s.get("module", "")))
        lines.append(f'izumi_module_ok{{module="{module}"}} {1 if s.get("ok") else 0}')
    lines += [
        "# HELP izumi_module_failures FailureRecords in the module's last run.",
        "# TYPE izumi_module_failures gauge",
    ]
    for s in status:
        module = _escape_label(str(s.get("module", "")))
        lines.append(f'izumi_module_failures{{module="{module}"}} {int(_num(s.get("failures")))}')
    lines += [
        "# HELP izumi_module_metric Latest value of an izumi module metric.",
        "# TYPE izumi_module_metric gauge",
    ]
    for m in metrics:
        module = _escape_label(str(m.get("module", "")))
        key = _escape_label(str(m.get("key", "")))
        value = _num(m.get("value"))
        lines.append(f'izumi_module_metric{{module="{module}",metric="{key}"}} {value}')
    return "\n".join(lines) + "\n"


@register("metricsexport")
def run(ctx: RunContext) -> ModuleResult:
    """Write ``reports/izumi.prom`` from the metrics store for Grafana/node_exporter."""
    result = ModuleResult(module="metricsexport", run_id=ctx.run_id, mode=ctx.mode)
    reports = ctx.config.reporting.dir
    db = reports / "cache" / "metrics.db"

    status: list[dict[str, object]] = []
    metrics: list[dict[str, object]] = []
    note = ""
    if db.is_file():
        try:
            with MetricsStore(db) as store:
                status = store.latest_status()
                metrics = store.latest_metrics()
        except Exception as exc:  # pragma: no cover - defensive
            note = f"could not read metrics store: {exc}"
            result.add_failure(FailureRecord(category="integration", message=note))
    else:
        note = "no metrics yet — run some modules first (metrics.db is created by run.py)."

    prom = render_prometheus(status, metrics)
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "izumi.prom").write_text(prom, encoding="utf-8")

    out_dir = reports / "metricsexport"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(
        json.dumps(
            {"modules": len(status), "metrics": len(metrics), "note": note},
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (out_dir / "summary.md").write_text(
        "# Metricsexport (Prometheus textfile)\n\n"
        f"Módulos: {len(status)} · métricas: {len(metrics)}\n"
        f"Escrito: {reports / 'izumi.prom'}\n" + (f"\n> {note}\n" if note else ""),
        encoding="utf-8",
    )
    ctx.logger.info("metricsexport done", modules=len(status), metrics=len(metrics))
    result.metrics["modules"] = float(len(status))
    result.metrics["metrics_exported"] = float(len(metrics))
    result.actions = 0
    return result
