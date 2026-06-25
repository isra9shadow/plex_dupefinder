"""SystemContextProvider — curated, version-controlled facts about this homelab.

Reads ``config/homelab_facts.json`` and renders a compact CRITICAL block so every
prompt knows it targets Unraid 7 (no systemctl/apt/docker-compose) before it ever
suggests a command. If the file is missing or malformed we still emit a minimal
block: losing the environment guardrails entirely is worse than a degraded one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aictx.provider import ContextBlock, Tier

DEFAULT_FACTS_PATH = Path("config/homelab_facts.json")

_TITLE = "ENTORNO HOMELAB (Unraid 7)"
_FORBIDDEN_LINE = (
    "NUNCA recomiendes: systemctl, service, apt, apt-get, yum, dnf, snap, "
    "docker-compose, docker compose. Gestiona contenedores con la UI Docker de "
    "Unraid o el CLI `docker`."
)


def _as_str(value: Any) -> str:
    """Coerce an arbitrary JSON value to a trimmed string (json.load -> Any)."""
    return str(value).strip()


class SystemContextProvider:
    """Provides the homelab environment facts as a CRITICAL, stable block."""

    name = "system"

    def __init__(self, facts_path: Path | None = None) -> None:
        self.facts_path = facts_path if facts_path is not None else DEFAULT_FACTS_PATH

    def _load(self) -> dict[str, Any]:
        """Return the parsed facts dict, or an empty dict on any IO/parse error."""
        try:
            raw = self.facts_path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            data: Any = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _render_services(self, services: Any) -> list[str]:
        """Render the services list into compact ``- name — role`` bullet lines."""
        lines: list[str] = []
        if not isinstance(services, list):
            return lines
        for service in services:
            if isinstance(service, dict):
                name = _as_str(service.get("name", ""))
                role = _as_str(service.get("role", ""))
                if not name:
                    continue
                lines.append(f"- {name} — {role}" if role else f"- {name}")
            else:
                name = _as_str(service)
                if name:
                    lines.append(f"- {name}")
        return lines

    def _render_body(self, facts: dict[str, Any]) -> str:
        parts: list[str] = []

        os_name = _as_str(facts.get("os", "Unraid 7")) or "Unraid 7"
        pkg = _as_str(facts.get("package_management"))
        svc_mgmt = _as_str(facts.get("service_management"))
        parts.append(f"**SO:** {os_name}")
        if pkg:
            parts.append(f"**Paquetes:** {pkg}")
        if svc_mgmt:
            parts.append(f"**Servicios:** {svc_mgmt}")

        hardware = facts.get("hardware")
        if isinstance(hardware, dict):
            hw_bits: list[str] = []
            gpu = _as_str(hardware.get("gpu"))
            if gpu:
                hw_bits.append(f"GPU {gpu}")
            ram = hardware.get("ram_gb")
            if ram is not None:
                hw_bits.append(f"RAM {_as_str(ram)} GB")
            role = _as_str(hardware.get("role"))
            if role:
                hw_bits.append(role)
            if hw_bits:
                parts.append("**Hardware:** " + ", ".join(hw_bits))

        paths = facts.get("paths")
        if isinstance(paths, dict) and paths:
            path_bits = [f"{_as_str(k)}={_as_str(v)}" for k, v in paths.items()]
            parts.append("**Rutas:** " + ", ".join(path_bits))

        service_lines = self._render_services(facts.get("services"))
        if service_lines:
            parts.append("**Stack:**\n" + "\n".join(service_lines))

        constraints = facts.get("constraints")
        if isinstance(constraints, list) and constraints:
            constraint_lines = [f"- {_as_str(c)}" for c in constraints if _as_str(c)]
            if constraint_lines:
                parts.append("**Restricciones:**\n" + "\n".join(constraint_lines))

        parts.append(_FORBIDDEN_LINE)
        return "\n\n".join(parts)

    def block(self) -> ContextBlock | None:
        facts = self._load()
        if not facts:
            body = "**SO:** Unraid 7\n\n" + _FORBIDDEN_LINE
        else:
            body = self._render_body(facts)
        return ContextBlock(
            name="system",
            title=_TITLE,
            body=body,
            tier=Tier.CRITICAL,
            stable=True,
        )
