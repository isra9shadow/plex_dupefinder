"""PromptTemplate for the ``logwatch`` task — declarative spec, no logic."""

from __future__ import annotations

from aictx.provider import PromptTemplate
from aictx.schema import DIAGNOSIS_SCHEMA

TEMPLATE: PromptTemplate = PromptTemplate(
    task="logwatch",
    system_role=(
        "Eres el asistente de operaciones de un homelab Unraid concreto. Diagnostica los "
        "eventos de error agrupados de los contenedores docker usando UNICAMENTE el "
        "CONTEXTO proporcionado; no inventes nada y etiqueta cada afirmacion como fact, "
        "inference o hypothesis."
    ),
    instructions=(
        "Analiza los eventos de error agrupados de docker que se te entregan. "
        "(a) Usa solo el contexto y la evidencia proporcionados; no asumas datos ausentes. "
        "(b) Toda orden propuesta debe ser compatible con Unraid: NUNCA uses systemctl, "
        "apt, yum, dnf ni docker-compose. "
        "(c) Devuelve estrictamente el JSON del esquema indicado, sin texto adicional. "
        "(d) Omite cualquier hallazgo que no tenga evidencia que lo respalde."
    ),
    schema=DIAGNOSIS_SCHEMA,
    provider_order=(
        "system",
        "capabilities",
        "inventory",
        "runtime",
        "module",
        "history",
    ),
)
