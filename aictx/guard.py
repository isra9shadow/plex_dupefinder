"""Deterministic Unraid-compatibility guard for model-suggested commands.

Unraid is not a systemd/apt distro: its services are Docker containers and its
package model is plugins, so commands like ``systemctl``, ``apt`` or
``docker-compose`` are wrong (and potentially harmful) on this host. This guard is
a hard, deterministic post-filter applied to the model output — independent of the
prompt — so a hallucinated non-Unraid command never reaches the operator.
"""

from __future__ import annotations

import copy
import re
from typing import Any

# Match a forbidden command as the first token of a command (after optional
# leading whitespace / sudo), or as the "docker-compose" / "docker compose" pair.
FORBIDDEN = re.compile(
    r"(?:^|[;&|]|\bsudo\s+)\s*"
    r"(?:systemctl|service|apt-get|apt|yum|dnf|snap|docker-compose|docker\s+compose)\b",
    re.IGNORECASE,
)


def vet_command(cmd: str) -> bool:
    """Return True when ``cmd`` is allowed (Unraid-safe), False if forbidden."""
    if not isinstance(cmd, str):
        return False
    return FORBIDDEN.search(cmd) is None


def sanitize_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Strip vetoed Unraid commands from every finding.

    Returns a deep copy of ``payload`` whose each finding's ``unraid_commands`` keeps
    only vetted commands, plus the flat list of removed commands. Defensive about
    malformed shapes: anything unexpected is left untouched.
    """
    clean = copy.deepcopy(payload)
    removed: list[str] = []

    findings = clean.get("findings") if isinstance(clean, dict) else None
    if not isinstance(findings, list):
        return clean, removed

    for finding in findings:
        if not isinstance(finding, dict):
            continue
        commands = finding.get("unraid_commands")
        if not isinstance(commands, list):
            continue
        vetted: list[Any] = []
        for cmd in commands:
            if isinstance(cmd, str) and vet_command(cmd):
                vetted.append(cmd)
            else:
                removed.append(cmd)
        finding["unraid_commands"] = vetted

    return clean, removed
