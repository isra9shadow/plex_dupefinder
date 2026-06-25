"""Results analyst — a "second pair of eyes" over the whole media pipeline.

It gathers what the automation could NOT finish and asks the local LLM to explain
it and recommend actions, so the operator does not have to read raw reports:

  1. ORGANIZER — ``reports/organizer/plan.json`` → the ``needs_review`` files the
     organizer left in place (low confidence / unresolved).
  2. DUPEFINDER — the latest ``dupefinder_report_*.json`` (legacy engine) →
     duplicate groups it did NOT process, aggregated by skip reason, plus the
     failure summary.

Then it builds local heuristics (always written, even if Ollama is down) and one
Ollama summary that groups everything by probable cause and gives a concrete,
prioritized action per group (Spanish output).

Strictly READ-ONLY (INVARIANT I1): reads reports, writes only its own report
under ``reporting.dir / "analyst"``. An unreachable Ollama is recorded and does
NOT abort. The Docker-log half of the diagnosis is the sibling ``logwatch``
module (the menu runs both).

Config (config.json):
  integrations.analyst : {max_items, dupefinder_reports}
    max_items          : cap of needs_review items fed to the AI (default 100)
    dupefinder_reports : dir holding dupefinder_report_*.json (legacy JSON_REPORT_DIR)
  integrations.ollama  : {base_url, model, ...}  # reused as the AI backend
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from core.errors import ConfigError, IntegrationError
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext
from integrations.ollama import OllamaClient

_DEFAULT_MAX_ITEMS = 100
# Collapse the variable numbers in a skip reason so identical causes bucket
# together, e.g. "score delta 740 below threshold 1000" -> "score delta N below
# threshold N".
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _as_list(value: object) -> list[dict[str, object]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _conf(item: dict[str, object]) -> float:
    value = item.get("confidence", 0)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def _str(item: dict[str, object], key: str) -> str:
    value = item.get(key)
    return value if isinstance(value, str) else ""


def heuristics(needs_review: list[dict[str, object]]) -> dict[str, object]:
    """Pure local breakdown of the un-moved files (no AI): by kind and confidence."""
    by_kind: dict[str, int] = {}
    buckets = {"0": 0, "1-49": 0, "50-79": 0, "80+": 0}
    for item in needs_review:
        kind = _str(item, "kind") or "unknown"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        conf = _conf(item)
        if conf <= 0:
            buckets["0"] += 1
        elif conf < 50:
            buckets["1-49"] += 1
        elif conf < 80:
            buckets["50-79"] += 1
        else:
            buckets["80+"] += 1
    return {"total": len(needs_review), "by_kind": by_kind, "by_confidence": buckets}


@dataclass(frozen=True)
class DupeReport:
    """The salient bits of one dupefinder JSON report (skips + failures)."""

    report: str
    summary: dict[str, object]
    failure_summary: dict[str, int]
    skips: dict[str, int]
    skip_samples: dict[str, list[str]]

    @property
    def skip_total(self) -> int:
        return sum(self.skips.values())

    @property
    def has_data(self) -> bool:
        return bool(self.skips or self.failure_summary)


def _normalize_reason(reason: str) -> str:
    return _NUM_RE.sub("N", reason).strip()


def _group_skip_reason(group: dict[str, object]) -> str:
    """Find a skip reason in a dupefinder group record (revalidation or discovery)."""
    reval = group.get("revalidation")
    if isinstance(reval, dict):
        reason = reval.get("reason")
        if isinstance(reason, str):
            return reason
    decision = group.get("discovery_decision")
    if isinstance(decision, dict):
        skip_reason = decision.get("skip_reason")
        if isinstance(skip_reason, str):
            return skip_reason
    return ""


def aggregate_skips(groups: list[dict[str, object]]) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Bucket dupefinder groups by normalized skip reason (+ up to 3 sample titles)."""
    buckets: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    for group in groups:
        reason = _group_skip_reason(group)
        if not reason:
            continue
        key = _normalize_reason(reason)
        buckets[key] = buckets.get(key, 0) + 1
        title = group.get("title")
        if isinstance(title, str) and len(samples.setdefault(key, [])) < 3:
            samples[key].append(title)
    return buckets, samples


def _load_organizer(ctx: RunContext) -> tuple[list[dict[str, object]], bool]:
    """Return (needs_review sorted worst-first, plan_exists)."""
    plan = ctx.config.reporting.dir / "organizer" / "plan.json"
    if not plan.is_file():
        return [], False
    try:
        data = json.loads(plan.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], True
    needs_review = _as_list(data.get("needs_review") if isinstance(data, dict) else None)
    needs_review.sort(key=_conf)
    return needs_review, True


def _load_dupefinder(ctx: RunContext) -> DupeReport | None:
    """Parse the most recent dupefinder_report_*.json from the configured dir."""
    raw = ctx.config.integrations.get("analyst", {}).get("dupefinder_reports")
    if not isinstance(raw, str) or not raw:
        return None
    directory = Path(raw)
    if not directory.is_dir():
        return None
    reports = sorted(directory.glob("dupefinder_report_*.json"), key=lambda p: p.stat().st_mtime)
    if not reports:
        return None
    try:
        data = json.loads(reports[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    groups = data.get("groups")
    skips, samples = aggregate_skips(_as_list(groups))
    raw_fs = data.get("failure_summary")
    failure_summary = (
        {
            k: v
            for k, v in raw_fs.items()
            if isinstance(v, int) and not isinstance(v, bool) and v > 0
        }
        if isinstance(raw_fs, dict)
        else {}
    )
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    return DupeReport(
        report=reports[-1].name,
        summary=summary if isinstance(summary, dict) else {},
        failure_summary=failure_summary,
        skips=skips,
        skip_samples=samples,
    )


def _ollama(ctx: RunContext) -> OllamaClient:
    settings = ctx.config.integrations.get("ollama", {})
    base = settings.get("base_url", "http://localhost:11434")
    model = settings.get("model", "qwen3:8b")
    if not isinstance(base, str) or not isinstance(model, str):
        raise ConfigError("integrations.ollama needs string 'base_url' and 'model'")
    return OllamaClient(base_url=base, model=model)


def _max_items(ctx: RunContext) -> int:
    value = ctx.config.integrations.get("analyst", {}).get("max_items", _DEFAULT_MAX_ITEMS)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return _DEFAULT_MAX_ITEMS


def build_prompt(
    items: list[dict[str, object]], org_stats: dict[str, object], dupe: DupeReport | None
) -> str:
    """One Spanish prompt: act as a second person reviewing the whole pipeline."""
    parts = [
        "Eres un asistente que actua como una SEGUNDA PERSONA revisando el estado de "
        "un homelab de medios para ahorrar tiempo al operador. Analiza los datos y "
        "escribe en ESPANOL un informe breve, PRIORIZADO y ACCIONABLE.",
        "",
    ]
    if items:
        lines = [
            f"- [{_conf(i):.0f}%] ({_str(i, 'kind') or 'unknown'}) {_str(i, 'filename')}"
            for i in items
        ]
        parts += [
            "## Organizer: ficheros NO movidos (needs_review)",
            f"Resumen: {json.dumps(org_stats, ensure_ascii=False)}",
            *lines,
            "",
        ]
    if dupe is not None and dupe.has_data:
        parts += [
            "## Dupefinder: duplicados NO procesados",
            f"Resumen: {json.dumps(dupe.summary, ensure_ascii=False)}",
            f"Fallos: {json.dumps(dupe.failure_summary, ensure_ascii=False)}",
            f"Motivos de skip (motivo: n grupos): {json.dumps(dupe.skips, ensure_ascii=False)}",
            "",
        ]
    parts += [
        "Para cada problema agrupa por CAUSA probable y da una ACCION concreta "
        "(bajar el umbral de confianza, anadir patron al parser, refrescar metadata "
        "en Plex, renombrar a mano, revisar permisos, ignorar...). Usa vinetas y "
        "prioriza lo que mas ficheros/duplicados desbloquea. Se conciso.",
    ]
    return "\n".join(parts)


def _write_report(
    ctx: RunContext,
    org_stats: dict[str, object],
    dupe: DupeReport | None,
    summary: str,
    note: str,
) -> None:
    out_dir = ctx.config.reporting.dir / "analyst"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "organizer": org_stats,
                "dupefinder": asdict(dupe) if dupe is not None else None,
                "note": note,
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    lines = [
        "# Analyst — diagnostico IA (segunda opinion)",
        "",
        "## Organizer (needs_review)",
        f"Total: {org_stats.get('total', 0)} | "
        f"tipo: {json.dumps(org_stats.get('by_kind', {}), ensure_ascii=False)} | "
        f"confianza: {json.dumps(org_stats.get('by_confidence', {}), ensure_ascii=False)}",
        "",
    ]
    if dupe is not None:
        lines += [
            "## Dupefinder (duplicados no procesados)",
            f"Reporte: {dupe.report}",
            f"Motivos de skip: {json.dumps(dupe.skips, ensure_ascii=False)}",
            f"Fallos: {json.dumps(dupe.failure_summary, ensure_ascii=False)}",
            "",
        ]
    lines += ["## Analisis IA", summary or "(sin resumen)", ""]
    if note:
        lines += [f"> {note}", ""]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


@register("analyst")
def run(ctx: RunContext) -> ModuleResult:
    result = ModuleResult(module="analyst", run_id=ctx.run_id, mode=ctx.mode)
    needs_review, plan_exists = _load_organizer(ctx)
    org_stats = heuristics(needs_review)
    dupe = _load_dupefinder(ctx)

    if not plan_exists and dupe is None:
        plan = ctx.config.reporting.dir / "organizer" / "plan.json"
        result.add_failure(
            FailureRecord(
                category="config",
                message=(
                    f"nothing to analyze — no organizer plan at {plan} and no dupefinder "
                    "reports configured (integrations.analyst.dupefinder_reports)"
                ),
            )
        )
        return result

    has_data = bool(needs_review) or (dupe is not None and dupe.has_data)
    summary = ""
    note = ""
    if has_data:
        items = needs_review[: _max_items(ctx)]
        try:
            summary = _ollama(ctx).complete(build_prompt(items, org_stats, dupe))
        except (ConfigError, IntegrationError) as exc:
            note = "Ollama no disponible — se guardan los datos sin resumen IA."
            result.add_failure(FailureRecord(category="integration", message=f"ollama: {exc}"))
    else:
        note = "Nada que analizar: el organizador movio todo y no hay skips de duplicados."

    _write_report(ctx, org_stats, dupe, summary, note)
    dupe_skips = dupe.skip_total if dupe is not None else 0
    ctx.logger.info(
        "analyst done",
        needs_review=len(needs_review),
        dupe_skips=dupe_skips,
        summarised=bool(summary),
    )
    result.metrics["needs_review"] = float(len(needs_review))
    result.metrics["dupe_skips"] = float(dupe_skips)
    result.actions = 0
    return result
