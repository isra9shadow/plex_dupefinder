"""Results analyst — read the organizer's plan and have the local LLM explain WHY
files were not moved and what to do about them (read-only diagnostics).

The organizer writes ``reports/organizer/plan.json`` split into ``confident`` (will
be / were moved) and ``needs_review`` (low confidence or unresolved → left in
place). This module turns that ``needs_review`` list into:

  1. local heuristics (no AI needed): counts by kind and by confidence bucket, so
     the report is useful even when Ollama is down; and
  2. an Ollama summary (best-effort): the model groups the un-moved files by
     probable reason (plain title with no year, ambiguous, foreign language,
     leet/obfuscated name, home-video timestamp, unidentified episode…) and
     recommends a concrete action per group (lower the threshold, add a parser
     pattern, rename by hand…). Spanish output, since the operator is Spanish.

Strictly READ-ONLY (INVARIANT I1): it only reads the organizer report and writes
its own report under ``reporting.dir / "analyst"``. An unreachable Ollama is
recorded via ``result.add_failure`` and does NOT abort — the heuristics + raw
list are still written.

Config (config.json):
  integrations.analyst : {max_items}   # cap of needs_review items fed to the AI
  integrations.ollama  : {base_url, model, ...}  # reused as the AI backend
"""

from __future__ import annotations

import json

from core.errors import ConfigError, IntegrationError
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext
from integrations.ollama import OllamaClient

_DEFAULT_MAX_ITEMS = 100


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


def build_prompt(items: list[dict[str, object]], stats: dict[str, object]) -> str:
    """One prompt asking for a Spanish root-cause grouping + recommended actions."""
    lines = [
        f"- [{_conf(i):.0f}%] ({_str(i, 'kind') or 'unknown'}) {_str(i, 'filename')}" for i in items
    ]
    return (
        "Eres un asistente que ayuda a organizar una biblioteca de medios. El "
        "modulo 'organizer' identifica peliculas/series con un parser y una IA y "
        "deja en 'needs_review' los ficheros que NO pudo mover con confianza.\n\n"
        f"Resumen: {json.dumps(stats, ensure_ascii=False)}.\n\n"
        "Aqui tienes la lista de ficheros no movidos (confianza, tipo, ruta):\n"
        f"{chr(10).join(lines)}\n\n"
        "Escribe en ESPANOL un analisis breve y accionable: agrupa los ficheros "
        "por CAUSA probable (titulo plano sin anio, nombre ambiguo, idioma "
        "extranjero, nombre ofuscado/leet, video casero con fecha, episodio sin "
        "identificar, basura/no-media...) y, por cada grupo, da una ACCION "
        "concreta (bajar el umbral de confianza, anadir patron al parser, "
        "renombrar a mano, ignorar...). Usa vinetas y se conciso."
    )


def _write_report(ctx: RunContext, stats: dict[str, object], summary: str, note: str) -> None:
    out_dir = ctx.config.reporting.dir / "analyst"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(
        json.dumps({"stats": stats, "note": note}, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    by_kind = stats.get("by_kind", {})
    by_conf = stats.get("by_confidence", {})
    lines = [
        "# Analyst — por que no se movieron ficheros",
        "",
        f"Total needs_review: {stats.get('total', 0)}",
        f"Por tipo: {json.dumps(by_kind, ensure_ascii=False)}",
        f"Por confianza: {json.dumps(by_conf, ensure_ascii=False)}",
        "",
        "## Analisis IA",
        summary or "(sin resumen)",
        "",
    ]
    if note:
        lines += [f"> {note}", ""]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


@register("analyst")
def run(ctx: RunContext) -> ModuleResult:
    result = ModuleResult(module="analyst", run_id=ctx.run_id, mode=ctx.mode)
    plan_path = ctx.config.reporting.dir / "organizer" / "plan.json"
    if not plan_path.is_file():
        result.add_failure(
            FailureRecord(
                category="config",
                message=f"no organizer plan at {plan_path} — run the organizer first",
            )
        )
        return result
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result.add_failure(FailureRecord(category="config", message=f"bad organizer plan: {exc}"))
        return result

    needs_review = _as_list(data.get("needs_review") if isinstance(data, dict) else None)
    # Worst first: the lowest-confidence files are the most informative to explain.
    needs_review.sort(key=_conf)
    stats = heuristics(needs_review)

    summary = ""
    note = ""
    if needs_review:
        items = needs_review[: _max_items(ctx)]
        try:
            summary = _ollama(ctx).complete(build_prompt(items, stats))
        except (ConfigError, IntegrationError) as exc:
            note = "Ollama no disponible — se guardan las heuristicas sin resumen IA."
            result.add_failure(FailureRecord(category="integration", message=f"ollama: {exc}"))
    else:
        note = "No hay ficheros en needs_review — el organizador movio todo con confianza."

    _write_report(ctx, stats, summary, note)
    ctx.logger.info(
        "analyst done",
        needs_review=stats["total"],
        summarised=bool(summary),
    )
    total = stats["total"]
    result.metrics["needs_review"] = float(total if isinstance(total, int) else 0)
    result.actions = 0
    return result
