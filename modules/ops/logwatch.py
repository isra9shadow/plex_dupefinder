"""Docker log watcher — scan container logs for errors, then have the local LLM
write a human summary (read-only diagnostics, never touches the host or media).

What it does:

  1. SCAN (read-only) — for every running container (optionally filtered) it pulls
     the last few days of logs via ``adapters/docker`` and keeps only the lines
     that look like errors/warnings (case-insensitive patterns). The most recent
     lines win when a cap (``max_error_lines``) is hit.
  2. SUMMARISE (AI, best-effort) — when any error lines were found it asks the
     local Ollama model (``integrations/ollama``) for ONE concise Spanish summary
     grouped by container: recurring patterns, probable root cause, recommended
     action. The raw grouped errors + meta always land in ``plan.json``; the AI
     prose lands in ``summary.md``.

This module is strictly READ-ONLY: it never moves, deletes or quarantines
anything (INVARIANT I1), the only thing it writes is its own report under
``reporting.dir / "logwatch"``. A failure for one container, or an unreachable
Ollama, is recorded via ``result.add_failure`` and does NOT abort the run.

Config (config.json):
  integrations.logwatch : {days, containers, max_error_lines}
    days            : how far back to read logs (default 3)
    containers      : optional list of container names to restrict to
    max_error_lines : cap on total error lines kept across all containers (400)
  integrations.ollama   : {base_url, model, ...}  # reused as the AI backend
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from adapters import docker
from core.errors import ConfigError, IntegrationError
from core.registry import register
from core.types import FailureRecord, ModuleResult, RunContext
from integrations.ollama import OllamaClient

_DEFAULT_DAYS = 3.0
_DEFAULT_MAX_LINES = 400

# Case-insensitive markers of trouble in an application log line. ``5\d\d`` catches
# HTTP 5xx server errors; the rest are the usual error/warning vocabulary.
_ERROR_RE = re.compile(
    r"(?i)("
    r"\berror\b|\berr\b|\bwarn(?:ing)?\b|traceback|exception|\bfatal\b|\bcritical\b"
    r"|\bpanic\b|failed|refused|timeout|permission denied|no such file"
    r"|\b5\d\d\b"
    r")"
)


@dataclass(frozen=True)
class ContainerErrors:
    """Error lines extracted from one container's recent logs."""

    name: str
    lines: list[str]


def extract_errors(logs: str) -> list[str]:
    """Return the log lines that match an error/warning pattern (order preserved)."""
    return [line.rstrip() for line in logs.splitlines() if _ERROR_RE.search(line)]


def _cap_recent(errors: list[ContainerErrors], max_lines: int) -> list[ContainerErrors]:
    """Keep at most ``max_lines`` lines total, dropping the OLDEST first.

    Logs are chronological, so the most recent lines (end of each container's
    list, last containers first) are the ones worth summarising.
    """
    budget = max(0, max_lines)
    capped: list[ContainerErrors] = []
    for entry in reversed(errors):
        if budget <= 0:
            break
        kept = entry.lines[-budget:]
        budget -= len(kept)
        capped.append(ContainerErrors(entry.name, kept))
    capped.reverse()
    return [c for c in capped if c.lines]


def _settings(ctx: RunContext) -> tuple[float, list[str] | None, int]:
    """Read ``integrations.logwatch`` → (days, container filter, max_error_lines)."""
    cfg = ctx.config.integrations.get("logwatch", {})
    days = cfg.get("days", _DEFAULT_DAYS)
    days_val = (
        float(days)
        if isinstance(days, int | float) and not isinstance(days, bool)
        else _DEFAULT_DAYS
    )
    raw_filter = cfg.get("containers")
    container_filter = (
        [c for c in raw_filter if isinstance(c, str)] if isinstance(raw_filter, list) else None
    )
    max_lines = cfg.get("max_error_lines", _DEFAULT_MAX_LINES)
    max_val = (
        max_lines
        if isinstance(max_lines, int) and not isinstance(max_lines, bool)
        else _DEFAULT_MAX_LINES
    )
    return days_val, container_filter, max_val


def _ollama(ctx: RunContext) -> OllamaClient:
    """Build the Ollama client from ``integrations.ollama`` (modeled on organizer)."""
    settings = ctx.config.integrations.get("ollama", {})
    base = settings.get("base_url", "http://localhost:11434")
    model = settings.get("model", "qwen3:8b")
    if not isinstance(base, str) or not isinstance(model, str):
        raise ConfigError("integrations.ollama needs string 'base_url' and 'model'")
    return OllamaClient(base_url=base, model=model)


def build_prompt(errors: list[ContainerErrors], days: float) -> str:
    """One prompt asking for a concise Spanish summary grouped by container."""
    blocks: list[str] = []
    for entry in errors:
        body = "\n".join(entry.lines)
        blocks.append(f"### Contenedor: {entry.name}\n{body}")
    joined = "\n\n".join(blocks)
    return (
        "Eres un asistente de operaciones de un homelab. A continuacion tienes "
        f"lineas de error y advertencia extraidas de los logs de los ultimos "
        f"{days:.0f} dias de varios contenedores Docker.\n\n"
        "Escribe en ESPAÑOL un resumen breve y accionable que, agrupado por "
        "contenedor, indique: (1) los patrones de error recurrentes, (2) la causa "
        "raiz probable y (3) una accion recomendada. Se conciso; usa vinetas.\n\n"
        f"{joined}"
    )


def _write_report(
    ctx: RunContext,
    errors: list[ContainerErrors],
    days: float,
    summary: str,
    note: str,
) -> None:
    out_dir = ctx.config.reporting.dir / "logwatch"
    out_dir.mkdir(parents=True, exist_ok=True)
    total = sum(len(e.lines) for e in errors)
    (out_dir / "plan.json").write_text(
        json.dumps(
            {
                "days": days,
                "containers_with_errors": len(errors),
                "error_lines": total,
                "note": note,
                "errors": {e.name: e.lines for e in errors},
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    lines = [
        "# Logwatch summary",
        "",
        f"Window: last {days:.0f} day(s)",
        f"Containers with errors: {len(errors)}",
        f"Error lines: {total}",
        "",
        "## AI summary",
        summary or "(no summary)",
        "",
    ]
    if note:
        lines += [f"> {note}", ""]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


@register("logwatch")
def run(ctx: RunContext) -> ModuleResult:
    result = ModuleResult(module="logwatch", run_id=ctx.run_id, mode=ctx.mode)
    days, container_filter, max_lines = _settings(ctx)

    names = docker.container_names()
    if container_filter is not None:
        wanted = set(container_filter)
        names = [n for n in names if n in wanted]

    errors: list[ContainerErrors] = []
    for name in names:
        try:
            raw = docker.logs(name, since_days=days)
        except Exception as exc:  # one container must not abort the whole run
            result.add_failure(
                FailureRecord(category="integration", message=f"{name}: logs failed: {exc}")
            )
            continue
        found = extract_errors(raw)
        if found:
            errors.append(ContainerErrors(name, found))

    errors = _cap_recent(errors, max_lines)
    total = sum(len(e.lines) for e in errors)

    summary = ""
    note = ""
    if errors:
        try:
            summary = _ollama(ctx).complete(build_prompt(errors, days))
        except (ConfigError, IntegrationError) as exc:
            note = "Ollama unavailable — raw errors saved without an AI summary."
            result.add_failure(FailureRecord(category="integration", message=f"ollama: {exc}"))
    else:
        note = "No error lines found in the scanned window."

    _write_report(ctx, errors, days, summary, note)
    ctx.logger.info(
        "logwatch done",
        containers=len(names),
        containers_with_errors=len(errors),
        error_lines=total,
        summarised=bool(summary),
    )
    result.metrics["containers"] = float(len(names))
    result.metrics["error_lines"] = float(total)
    result.actions = 0
    return result
