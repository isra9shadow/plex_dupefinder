"""Shadow-compare monitor — summarize native-vs-legacy drift across recent runs.

The engine writes a ``phases.shadow_compare`` block into each
``dupefinder_report_*.json`` when ``SHADOW_COMPARE`` is on (see the engine wiring,
CTO-12). This READ-ONLY module aggregates that block across the most recent reports
so the operator can watch the **parity window**: it answers "has the native port
matched the legacy decision on every run for long enough to consider activating
it?" without opening JSONs by hand.

Verdict:
  * ``clean``  — every considered run reported ``drift == 0``.
  * ``drift``  — at least one run diverged; the offending run + fields are surfaced.
  * ``no_data``— no report carried a shadow_compare block (turn SHADOW_COMPARE on).

Strictly READ-ONLY (INVARIANT I1): it only reads report JSON and writes its own
report. It decides nothing about activation — that stays an explicit operator call
after a long clean window.

Config (config.json):
  integrations.shadowcheck :
    reports : dir holding dupefinder_report_*.json (reuses the analyst key shape;
              falls back to integrations.analyst.dupefinder_reports when unset)
    window  : how many most-recent reports to consider (default 20)

Metrics: ``runs_with_data``, ``total_checked``, ``total_drift``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext

_DEFAULT_WINDOW = 20


@dataclass(frozen=True)
class _Settings:
    reports_dir: str | None
    window: int


@dataclass(frozen=True)
class RunShadow:
    """The shadow-compare block extracted from one report."""

    report: str
    checked: int
    drift: int
    drift_groups: list[dict[str, object]]


@dataclass(frozen=True)
class Summary:
    verdict: str  # clean | drift | no_data
    runs_with_data: int
    total_checked: int
    total_drift: int
    first_drift: RunShadow | None


def _pos_int(raw: object, default: int) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else default


def _settings(ctx: RunContext) -> _Settings:
    cfg = ctx.config.integrations.get("shadowcheck", {})
    reports = cfg.get("reports")
    if not (isinstance(reports, str) and reports.strip()):
        # Reuse the analyst's dupefinder reports dir when shadowcheck has none.
        analyst = ctx.config.integrations.get("analyst", {})
        alt = analyst.get("dupefinder_reports")
        reports = alt if isinstance(alt, str) and alt.strip() else None
    return _Settings(
        reports_dir=reports if isinstance(reports, str) and reports.strip() else None,
        window=_pos_int(cfg.get("window"), _DEFAULT_WINDOW),
    )


def extract_shadow(report: str, data: object) -> RunShadow | None:
    """Pull the ``phases.shadow_compare`` block out of one report (None if absent)."""
    if not isinstance(data, dict):
        return None
    phases = data.get("phases")
    block = phases.get("shadow_compare") if isinstance(phases, dict) else None
    if not isinstance(block, dict):
        return None
    checked = block.get("checked")
    drift = block.get("drift")
    groups = block.get("drift_groups")
    return RunShadow(
        report=report,
        checked=checked if isinstance(checked, int) and not isinstance(checked, bool) else 0,
        drift=drift if isinstance(drift, int) and not isinstance(drift, bool) else 0,
        drift_groups=[g for g in groups if isinstance(g, dict)] if isinstance(groups, list) else [],
    )


def _load_reports(directory: Path, window: int) -> list[RunShadow]:
    """Return shadow blocks from the newest ``window`` reports, newest first."""
    if not directory.is_dir():
        return []
    reports = sorted(
        directory.glob("dupefinder_report_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:window]
    out: list[RunShadow] = []
    for path in reports:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        block = extract_shadow(path.name, data)
        if block is not None:
            out.append(block)
    return out


def summarize(blocks: list[RunShadow]) -> Summary:
    """Aggregate shadow blocks into a parity verdict (pure).

    ``clean`` only if there is at least one run with data AND no run drifted;
    the oldest drifting run (last in newest-first order) is surfaced as the
    representative divergence to investigate.
    """
    if not blocks:
        return Summary("no_data", 0, 0, 0, None)
    total_checked = sum(b.checked for b in blocks)
    total_drift = sum(b.drift for b in blocks)
    drifting = [b for b in blocks if b.drift > 0]
    if drifting:
        return Summary("drift", len(blocks), total_checked, total_drift, drifting[-1])
    return Summary("clean", len(blocks), total_checked, total_drift, None)


def _write_report(ctx: RunContext, summary: Summary, window: int, note: str) -> None:
    out_dir = ctx.config.reporting.dir / "shadowcheck"
    out_dir.mkdir(parents=True, exist_ok=True)
    first = summary.first_drift
    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "verdict": summary.verdict,
                "window": window,
                "runs_with_data": summary.runs_with_data,
                "total_checked": summary.total_checked,
                "total_drift": summary.total_drift,
                "first_drift": (
                    {"report": first.report, "drift": first.drift, "groups": first.drift_groups}
                    if first is not None
                    else None
                ),
                "note": note,
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    label = {
        "clean": "PARIDAD LIMPIA",
        "drift": "DERIVA DETECTADA",
        "no_data": "SIN DATOS",
    }.get(summary.verdict, summary.verdict)
    lines = [
        "# Shadowcheck — monitor de la ventana de paridad (solo lectura)",
        "",
        f"Veredicto: {label}",
        f"Runs con datos: {summary.runs_with_data} (ventana {window})",
        f"Decisiones comparadas: {summary.total_checked}",
        f"Derivas totales: {summary.total_drift}",
    ]
    if summary.verdict == "clean":
        lines += [
            "",
            "> Native == legacy en todas las corridas consideradas. Cuantas más",
            "> corridas limpias acumules, más seguro es activar el motor nativo.",
        ]
    elif summary.verdict == "drift" and first is not None:
        lines += ["", f"> Deriva en `{first.report}` ({first.drift}):"]
        for g in first.drift_groups[:20]:
            title = g.get("title")
            diffs = g.get("diffs")
            diffs_s = ", ".join(str(d) for d in diffs) if isinstance(diffs, list) else ""
            lines.append(f"  - {title}: {diffs_s}")
        lines += ["", "> NO actives el nativo hasta resolver la deriva."]
    if note:
        lines += ["", f"> {note}"]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


@register("shadowcheck")
def run(ctx: RunContext) -> ModuleResult:
    """Summarize native-vs-legacy shadow drift across recent engine reports."""
    result = ModuleResult(module="shadowcheck", run_id=ctx.run_id, mode=ctx.mode)
    settings = _settings(ctx)

    note = ""
    if settings.reports_dir is None:
        note = (
            "No reports dir configured — set integrations.shadowcheck.reports (or "
            "integrations.analyst.dupefinder_reports) to where dupefinder_report_*.json live."
        )
        summary = Summary("no_data", 0, 0, 0, None)
    else:
        blocks = _load_reports(Path(settings.reports_dir), settings.window)
        summary = summarize(blocks)
        if summary.verdict == "no_data":
            note = (
                "No report carried a shadow_compare block — enable SHADOW_COMPARE in the "
                "engine config to start the parity window."
            )
        elif summary.verdict == "drift" and summary.first_drift is not None:
            result.add_failure(
                FailureRecord(
                    category="integration",
                    message=(
                        f"native/legacy drift in {summary.first_drift.report} "
                        f"({summary.total_drift} total) — do NOT activate the native engine"
                    ),
                )
            )

    _write_report(ctx, summary, settings.window, note)
    ctx.logger.info(
        "shadowcheck done",
        verdict=summary.verdict,
        runs=summary.runs_with_data,
        drift=summary.total_drift,
    )
    result.metrics["runs_with_data"] = float(summary.runs_with_data)
    result.metrics["total_checked"] = float(summary.total_checked)
    result.metrics["total_drift"] = float(summary.total_drift)
    result.actions = 0  # read-only
    return result
