"""Docker log watcher — scan container logs for errors, then have the local LLM
write a human summary (read-only diagnostics, never touches the host or media).

What it does:

  1. SCAN (read-only) — for EVERY container (running and stopped/crashed,
     optionally filtered) it pulls the last week of logs via ``adapters/docker``
     (``--tail`` bounded), keeps only error/warning lines, and FOLDS repeats into
     one ``line (xN)`` so a noisy container can't drown the AI. Per-container and
     global caps apply; every scanned container's error count is reported.
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
  integrations.logwatch :
    days                    : how far back to read logs (default 7 = last week)
    containers              : optional list of container names to restrict to
    max_error_lines         : global cap of distinct error lines sent to AI (400)
    max_lines_per_container : per-container cap of distinct error lines (60)
    max_log_lines           : --tail cap per container's logs (20000)
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

_DEFAULT_DAYS = 7.0  # last week
_DEFAULT_MAX_LINES = 400  # global cap of DISTINCT error lines sent to the AI
_DEFAULT_MAX_PER_CONTAINER = 60  # per-container cap of distinct error lines
_DEFAULT_MAX_LOG_LINES = 20000  # --tail cap when pulling each container's week of logs

# Case-insensitive markers of trouble in an application log line. Broad on
# purpose (better to over-collect than miss a real error — duplicates are folded
# away below). Covers plain words, structured logs (``level=error``, ``[ERR]``),
# and HTTP 5xx (``5\d\d``).
_ERROR_RE = re.compile(
    r"(?i)("
    r"\berror\b|\berr\b|\bwarn(?:ing)?\b|traceback|exception|\bfatal\b|\bcritical\b"
    r"|\bpanic\b|\bfail(?:ed|ure|ing)?\b|refused|timeout|timed out|permission denied"
    r"|no such file|not found|cannot\b|unable to|denied|segfault|stack ?trace"
    r"|out of memory|\boom\b|oom-kill|\bkilled\b|unhealthy|abort(?:ed)?|\bdied\b"
    r"|level=(?:error|warn|warning|fatal|critical)|\[(?:err|error|warn|fatal|crit)"
    r"|\b5\d\d\b"
    r")"
)

# Used to fold near-identical repeated errors into one representative + a count,
# so a container that logged the SAME error 5000x does not drown the AI input.
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\S+\s+")  # leading docker --timestamps (RFC3339) token
_NUM_RE = re.compile(r"\d+")
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{6,}\b")


@dataclass(frozen=True)
class ContainerErrors:
    """De-duplicated error lines (``line  (xN)``) from one container's recent logs."""

    name: str
    lines: list[str]


def extract_errors(logs: str) -> list[str]:
    """Return the log lines that match an error/warning pattern (order preserved)."""
    return [line.rstrip() for line in logs.splitlines() if _ERROR_RE.search(line)]


def _norm(line: str) -> str:
    """Normalize a log line so repeats collapse: drop the leading timestamp,
    mask hex blobs and numbers (ids, timestamps, ports), lowercase, clamp."""
    body = _TS_RE.sub("", line.strip())
    return _NUM_RE.sub("N", _HEX_RE.sub("X", body)).lower()[:200]


def dedupe_errors(lines: list[str], *, limit: int) -> list[str]:
    """Collapse repeated errors into ``representative  (xN)``, most frequent first,
    capped at ``limit`` distinct lines (keeps the AI input small AND distinct)."""
    counts: dict[str, int] = {}
    reps: dict[str, str] = {}
    for line in lines:
        key = _norm(line)
        if key not in counts:
            counts[key] = 0
            reps[key] = _TS_RE.sub("", line.strip())  # show the line without its timestamp
        counts[key] += 1
    out: list[str] = []
    for key in sorted(counts, key=lambda k: -counts[k])[: max(0, limit)]:
        n = counts[key]
        out.append(f"{reps[key]}  (x{n})" if n > 1 else reps[key])
    return out


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


@dataclass(frozen=True)
class _Settings:
    days: float
    containers: list[str] | None
    max_error_lines: int  # global cap of distinct error lines fed to the AI
    max_per_container: int  # per-container cap of distinct error lines
    max_log_lines: int  # --tail cap when reading each container's logs


def _int(cfg: dict[str, object], key: str, default: int) -> int:
    value = cfg.get(key, default)
    return (
        value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default
    )


def _settings(ctx: RunContext) -> _Settings:
    """Read ``integrations.logwatch`` into a typed settings object."""
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
    return _Settings(
        days=days_val,
        containers=container_filter,
        max_error_lines=_int(cfg, "max_error_lines", _DEFAULT_MAX_LINES),
        max_per_container=_int(cfg, "max_lines_per_container", _DEFAULT_MAX_PER_CONTAINER),
        max_log_lines=_int(cfg, "max_log_lines", _DEFAULT_MAX_LOG_LINES),
    )


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
    counts: dict[str, int],
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
                "containers_scanned": len(counts),
                "containers_with_errors": len(errors),
                "error_lines": total,
                "note": note,
                "counts": counts,  # every scanned container -> its error-line count
                "errors": {e.name: e.lines for e in errors},
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    # List EVERY container scanned with its count so the operator can see the full
    # coverage (which dockers were checked), worst first.
    per_container = [
        f"- {name}: {count}"
        for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    lines = [
        "# Logwatch summary",
        "",
        f"Window: last {days:.0f} day(s)",
        f"Containers scanned: {len(counts)}",
        f"Containers with errors: {len(errors)}",
        f"Error lines: {total}",
        "",
        f"## Per container ({len(counts)})",
        *(per_container or ["(ninguno — ¿docker accesible? socket montado?)"]),
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
    settings = _settings(ctx)

    # Scan ALL containers (running AND stopped — a crashed container is exactly
    # where the errors are), so the report covers every docker on the host.
    names = docker.container_names(include_stopped=True)
    if settings.containers is not None:
        wanted = set(settings.containers)
        names = [n for n in names if n in wanted]

    counts: dict[str, int] = {}
    errors: list[ContainerErrors] = []
    for name in names:
        try:
            # --tail bounds a week of huge logs; we then keep only error lines.
            raw = docker.logs(name, since_days=settings.days, tail=settings.max_log_lines)
        except Exception as exc:  # one container must not abort the whole run
            result.add_failure(
                FailureRecord(category="integration", message=f"{name}: logs failed: {exc}")
            )
            counts[name] = 0
            continue
        found = extract_errors(raw)
        counts[name] = len(found)  # raw match volume, recorded for EVERY container
        if found:
            # Fold repeats so 5000 identical errors become one "line (x5000)".
            errors.append(
                ContainerErrors(name, dedupe_errors(found, limit=settings.max_per_container))
            )

    errors = _cap_recent(errors, settings.max_error_lines)
    total = sum(len(e.lines) for e in errors)

    summary = ""
    note = ""
    if not names:
        # Distinguish "docker unreachable" from "genuinely no containers" so the
        # operator knows whether it is a setup problem (stale image / socket).
        if docker.probe():
            note = "Docker reachable but no containers listed."
        else:
            note = (
                "Docker NOT reachable from the container: the docker client is "
                "missing from the image or /var/run/docker.sock is not mounted. "
                "Rebuild the image (the menu rebuilds it when the Dockerfile "
                "changes; or run: docker rmi izumi-organizer:local)."
            )
            result.add_failure(FailureRecord(category="integration", message=note))
    elif errors:
        try:
            summary = _ollama(ctx).complete(build_prompt(errors, settings.days))
        except (ConfigError, IntegrationError) as exc:
            note = "Ollama unavailable — raw errors saved without an AI summary."
            result.add_failure(FailureRecord(category="integration", message=f"ollama: {exc}"))
    else:
        note = "No error lines found in the scanned window."

    _write_report(ctx, errors, counts, settings.days, summary, note)
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
