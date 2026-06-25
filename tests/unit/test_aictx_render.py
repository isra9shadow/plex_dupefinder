"""Tests for the aictx markdown renderer."""

from __future__ import annotations

from typing import Any

from aictx.render import render_markdown


def _payload() -> dict[str, Any]:
    return {
        "summary": "Resumen del diagnóstico.",
        "findings": [
            {
                "title": "Traefik ACME perms",
                "severity": "warning",
                "confidence": 80,
                "root_cause": "acme.json es legible por todos",
                "evidence": [
                    {"kind": "fact", "detail": "permission denied acme.json"},
                    {"kind": "inference", "detail": "Traefik no puede leer el cert"},
                ],
                "recommended_actions": ["Ajustar permisos de acme.json"],
                "unraid_commands": ["chmod 600 acme.json"],
                "risk": "low",
                "priority": 3,
            }
        ],
    }


def test_produces_all_sections() -> None:
    md = render_markdown(_payload())
    assert "## Resumen" in md
    assert "Resumen del diagnóstico." in md
    assert "### Traefik ACME perms" in md
    assert "Severidad: Advertencia" in md
    assert "Confianza: 80%" in md
    assert "Causa raíz: acme.json es legible por todos" in md
    assert "Evidencias:" in md
    assert "(Hecho) permission denied acme.json" in md
    assert "Acciones recomendadas:" in md
    assert "Ajustar permisos de acme.json" in md
    assert "Comandos Unraid:" in md
    assert "`chmod 600 acme.json`" in md
    assert "Riesgo: Bajo" in md
    assert "Prioridad: 3" in md


def test_defensive_missing_keys() -> None:
    md = render_markdown({"findings": [{"title": "solo título"}]})
    assert "## Resumen" in md
    assert "(sin resumen)" in md
    assert "### solo título" in md
    # No optional sections rendered when their keys are absent.
    assert "Evidencias:" not in md
    assert "Comandos Unraid:" not in md


def test_non_dict_payload_returns_empty() -> None:
    assert render_markdown([]) == ""  # type: ignore[arg-type]


def test_empty_findings() -> None:
    md = render_markdown({"summary": "nada", "findings": []})
    assert "## Resumen" in md
    assert "nada" in md
