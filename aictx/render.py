"""Render a validated DIAGNOSIS_SCHEMA payload into readable Spanish markdown.

The model's structured output is great for machines but unreadable for an
operator skimming a report. This turns a (already validated + guarded) diagnosis
payload into a human-friendly markdown document. Defensive by design: missing or
malformed keys are skipped rather than raising, so a partial payload still renders.
"""

from __future__ import annotations

from typing import Any

_SEVERITY_LABELS = {
    "info": "Informativo",
    "warning": "Advertencia",
    "error": "Error",
    "critical": "Crítico",
}
_RISK_LABELS = {
    "low": "Bajo",
    "medium": "Medio",
    "high": "Alto",
}
_EVIDENCE_LABELS = {
    "fact": "Hecho",
    "inference": "Inferencia",
    "hypothesis": "Hipótesis",
}


def render_markdown(payload: dict[str, Any]) -> str:
    """Turn a DIAGNOSIS_SCHEMA payload into Spanish markdown."""
    if not isinstance(payload, dict):
        return ""

    lines: list[str] = []

    summary = payload.get("summary")
    lines.append("## Resumen")
    lines.append(str(summary).strip() if isinstance(summary, str) else "(sin resumen)")

    findings = payload.get("findings")
    if isinstance(findings, list):
        for finding in findings:
            section = _render_finding(finding)
            if section:
                lines.append("")
                lines.extend(section)

    return "\n".join(lines).strip() + "\n"


def _render_finding(finding: Any) -> list[str]:
    if not isinstance(finding, dict):
        return []

    lines: list[str] = []

    title = finding.get("title")
    lines.append(f"### {title!s}" if isinstance(title, str) else "### Hallazgo")

    severity = finding.get("severity")
    if isinstance(severity, str):
        label = _SEVERITY_LABELS.get(severity, severity)
        lines.append(f"- Severidad: {label}")

    confidence = finding.get("confidence")
    if isinstance(confidence, int):
        lines.append(f"- Confianza: {confidence}%")

    root_cause = finding.get("root_cause")
    if isinstance(root_cause, str) and root_cause.strip():
        lines.append(f"- Causa raíz: {root_cause.strip()}")

    risk = finding.get("risk")
    if isinstance(risk, str):
        lines.append(f"- Riesgo: {_RISK_LABELS.get(risk, risk)}")

    priority = finding.get("priority")
    if isinstance(priority, int):
        lines.append(f"- Prioridad: {priority}")

    lines.extend(_render_evidence(finding.get("evidence")))
    lines.extend(_render_list("Acciones recomendadas", finding.get("recommended_actions")))
    lines.extend(_render_list("Comandos Unraid", finding.get("unraid_commands"), code=True))

    return lines


def _render_evidence(evidence: Any) -> list[str]:
    if not isinstance(evidence, list) or not evidence:
        return []
    lines = ["", "Evidencias:"]
    for item in evidence:
        if not isinstance(item, dict):
            continue
        detail = item.get("detail")
        if not isinstance(detail, str):
            continue
        kind = item.get("kind")
        label = _EVIDENCE_LABELS.get(kind, kind) if isinstance(kind, str) else None
        prefix = f"({label}) " if label else ""
        lines.append(f"- {prefix}{detail}")
    return lines if len(lines) > 2 else []


def _render_list(heading: str, items: Any, *, code: bool = False) -> list[str]:
    if not isinstance(items, list) or not items:
        return []
    lines = ["", f"{heading}:"]
    for item in items:
        if not isinstance(item, str):
            continue
        lines.append(f"- `{item}`" if code else f"- {item}")
    return lines if len(lines) > 2 else []
