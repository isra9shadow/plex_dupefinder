"""CapabilitiesContextProvider — the AI's honest self-description of its limits.

A static CRITICAL block clarifying what the model may produce (scripts, JSON,
plans, recommendations) versus what it must never claim to do (execute, mutate the
filesystem, delete data, or invent facts), plus the labelling/citation discipline
every answer must follow.
"""

from __future__ import annotations

from aictx.provider import ContextBlock, Tier

_TITLE = "CAPACIDADES Y LÍMITES DEL ASISTENTE"

_BODY = """\
PUEDES: generar scripts, JSON, planes de acción y recomendaciones para que un \
humano los revise y ejecute.

NO puedes: ejecutar comandos, modificar archivos, borrar datos ni inventar \
información. No tienes acceso al sistema en tiempo de respuesta.

DISCIPLINA OBLIGATORIA:
- Etiqueta cada afirmación como fact, inference o hypothesis.
- Cita la evidencia que respalda cada fact (archivo, salida, dato del contexto).
- Si no hay evidencia, decláralo como hypothesis; nunca presentes suposiciones \
como hechos."""


class CapabilitiesContextProvider:
    """Provides the assistant capability/limits block (CRITICAL, stable)."""

    name = "capabilities"

    def block(self) -> ContextBlock | None:
        return ContextBlock(
            name="capabilities",
            title=_TITLE,
            body=_BODY,
            tier=Tier.CRITICAL,
            stable=True,
        )
