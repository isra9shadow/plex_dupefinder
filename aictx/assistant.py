"""Assistant core — route a natural-language question to read-only modules + prompt.

Pure, dependency-light logic for the conversational assistant (the heavy LLM call
and module execution live in the ``assistant.py`` container entrypoint). Two jobs:

  * :func:`route` — map a Spanish question to the READ-ONLY modules whose reports
    answer it (keyword rules; deterministic and unit-tested). No LLM needed to
    decide *what to look at*, so this stays offline and predictable.
  * :func:`build_prompt` — assemble the LLM prompt from the question + the selected
    modules' summaries, with the same evidence discipline the platform already uses
    (label fact/inference/hypothesis, cite the module a claim comes from).

stdlib-only and side-effect-free; safe to import anywhere (host or container).
"""

from __future__ import annotations

from collections.abc import Callable

# (keyword substrings, modules to consult). First match wins per rule; a question
# may hit several rules. All targets are READ-ONLY modules.
_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("servicio", "caido", "caído", "down", "levanta", "arranca", "uptime"), ("uptime",)),
    (("base de datos", "bbdd", " db", "sqlite", "corrupt", "dbcheck"), ("dbcheck",)),
    (("permiso", "chmod", "chown", "error 13", "denied", "owner", "propietario"), ("permsdoctor",)),
    (("red", "dns", "network", "enotfound", "resolve", "getaddrinfo", "conecta"), ("netdoctor",)),
    (("backup", "copia de seguridad", "respaldo"), ("backupaudit",)),
    (("disco", "smart", "temperatura", "espacio", "lleno", "capacidad"), ("diskwatch", "status")),
    (("log", "error", "falla", "fallo", "por que", "por qué", "why", "cuelga"), ("logwatch",)),
    (("estado", "cpu", "ram", "gpu", "carga", "rendimiento", "status"), ("status",)),
    (("config", "falta", "variable", "ajuste", "token", "clave"), ("configcheck",)),
)

# When nothing matches, look at the overall health.
_DEFAULT: tuple[str, ...] = ("status", "uptime")


def route(question: str, *, max_modules: int = 4) -> list[str]:
    """Return the read-only modules to consult for ``question`` (order-preserving)."""
    low = (question or "").lower()
    out: list[str] = []
    for keywords, modules in _RULES:
        if any(kw in low for kw in keywords):
            for m in modules:
                if m not in out:
                    out.append(m)
    if not out:
        out = list(_DEFAULT)
    return out[:max_modules]


def build_prompt(question: str, sections: list[tuple[str, str]]) -> str:
    """Assemble the LLM prompt from the question + per-module report summaries.

    ``sections`` is ``(module, summary_text)``. The instruction enforces the
    platform's evidence discipline and asks for a concise Spanish answer plus,
    when applicable, the concrete next action (which the operator applies through
    the existing confirmed ``/apply`` flow — the assistant never executes).
    """
    parts = [
        "Eres el copiloto de operaciones de un homelab (Unraid). Responde en español, "
        "conciso y accionable. Usa SOLO la evidencia de los informes de abajo; si falta "
        "evidencia, dilo. Etiqueta afirmaciones como (fact)/(inferencia)/(hipótesis) y cita "
        "el módulo del que sale cada fact. Si procede, indica la acción concreta a aplicar "
        "(el operador la confirmará; tú NO ejecutas nada).",
        "",
        f"PREGUNTA: {question.strip()}",
        "",
        "INFORMES DISPONIBLES:",
    ]
    if sections:
        for module, summary in sections:
            body = summary.strip() or "(sin datos)"
            parts.append(f"\n### {module}\n{body}")
    else:
        parts.append("\n(sin informes — no se pudo recopilar contexto)")
    parts.append("\nRESPUESTA:")
    return "\n".join(parts)


def assemble(
    question: str,
    run_and_read: Callable[[str], str],
    *,
    max_modules: int = 4,
) -> tuple[str, list[tuple[str, str]]]:
    """Route the question, run+read each module, and build the prompt.

    ``run_and_read(module)`` runs the (read-only) module and returns its summary
    text. Injected so this stays offline/testable; the container entrypoint wires
    the real module dispatch + report read. Returns ``(prompt, sections)``.
    """
    sections = [
        (module, run_and_read(module)) for module in route(question, max_modules=max_modules)
    ]
    return build_prompt(question, sections), sections


def fallback_answer(sections: list[tuple[str, str]], reason: str) -> str:
    """Answer used when the LLM is unavailable: just surface the raw summaries."""
    parts = [f"(IA no disponible: {reason}) — te muestro los informes en crudo:", ""]
    for module, summary in sections:
        parts.append(f"### {module}\n{summary.strip() or '(sin datos)'}\n")
    return "\n".join(parts).strip()
