"""Apply layer — turn an AI diagnosis into SAFE, operator-confirmed actions.

The diagnosis (``DIAGNOSIS_SCHEMA``) carries, per finding, a list of
``unraid_commands`` already filtered by :mod:`aictx.guard` (no systemctl/apt/...).
This module adds the second half of the "aplicar con confirmacion" feature:

  * :func:`classify` — a POSITIVE allow-list: a command runs only if it matches a
    known-safe shape (docker restart/start/stop, docker logs, chmod, chown,
    mkdir) AND passes the guard AND contains no shell metacharacters. Everything
    else is rejected by default. Destructive file ops (rm/mv/...) are NOT on the
    list — those go through ``core/fs`` quarantine, never raw (INVARIANT I1).
  * :func:`extract_actions` / :func:`load_actions_from_file` — pull the applicable
    actions out of a diagnosis (or a module's ``plan.json``).
  * :func:`apply_action` — re-classify (defense in depth) then run via an injected
    runner; :func:`default_runner` uses the single ``adapters/command`` subprocess
    boundary. The model NEVER auto-executes: the operator confirms each action in
    the front-end (menu ``y/N`` or Telegram inline button).

Pure + host-safe: imports only stdlib + :mod:`aictx.guard` + ``adapters/command``
(no ``jsonschema``), so the host launchers (menu.py / bot.py) can import it.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adapters import command as command_adapter

from aictx import guard

# Mask variable numbers so a finding's id is stable across recurrences
# ("error 13" / "error 5" -> same id). Kept identical to the modules that RECORD
# incidents (analyst/logwatch) so resolve_incident() targets the right row.
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def finding_fingerprint(title: str) -> str:
    """Stable 16-char id for a finding title (numbers masked, lowercased)."""
    norm = _NUM_RE.sub("N", title).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


# Reject anything that could chain commands, redirect, expand, glob or quote —
# applied commands must be a single, literal, list-arg invocation.
_METACHARS = (
    ";",
    "&",
    "|",
    "<",
    ">",
    "$",
    "`",
    "\n",
    "\r",
    "*",
    "?",
    "(",
    ")",
    "{",
    "}",
    "\\",
    '"',
    "'",
)

_NAME = r"[A-Za-z0-9][\w.-]*"  # docker container name
_PATH = r"/[^\s]+"  # absolute path, no spaces (quoted/space paths are not auto-appliable)

# POSITIVE allow-list: (pattern, category). A command must match exactly one.
_ALLOWLIST: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"^docker\s+(?:restart|start|stop)\s+{_NAME}$"), "docker-lifecycle"),
    (
        re.compile(
            rf"^docker\s+logs"
            rf"(?:\s+(?:--tail|--since|--until|-n)\s+\S+|\s+(?:-t|--timestamps|--details))*"
            rf"\s+{_NAME}$"
        ),
        "docker-logs",
    ),
    (re.compile(rf"^chmod\s+[0-7]{{3,4}}\s+{_PATH}$"), "chmod"),
    (re.compile(rf"^chown\s+{_NAME}(?::{_NAME})?\s+{_PATH}$"), "chown"),
    (re.compile(rf"^mkdir(?:\s+-p)?\s+{_PATH}$"), "mkdir"),
)


@dataclass(frozen=True)
class Classification:
    """Whether a command may be applied, its category, and (if not) why not."""

    allowed: bool
    category: str
    reason: str


def classify(command: str) -> Classification:
    """Decide if ``command`` is safe to apply: guard + no metachars + allow-list."""
    if not isinstance(command, str):
        return Classification(False, "", "no es una cadena")
    cmd = command.strip()
    if not cmd:
        return Classification(False, "", "comando vacio")
    if any(ch in cmd for ch in _METACHARS):
        return Classification(False, "", "contiene metacaracteres de shell")
    if not guard.vet_command(cmd):
        return Classification(False, "", "vetado por el guard (no compatible con Unraid)")
    for pattern, category in _ALLOWLIST:
        if pattern.match(cmd):
            return Classification(True, category, "")
    return Classification(False, "", "no esta en la allow-list de acciones seguras")


@dataclass(frozen=True)
class ApplyAction:
    """One safe, applicable command tied to the finding that proposed it."""

    command: str
    category: str
    finding_title: str
    fingerprint: str
    severity: str


def extract_actions(diagnosis: object) -> list[ApplyAction]:
    """Return the de-duplicated, allow-listed actions from a diagnosis payload."""
    out: list[ApplyAction] = []
    seen: set[str] = set()
    findings = diagnosis.get("findings") if isinstance(diagnosis, dict) else None
    if not isinstance(findings, list):
        return out
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        title = finding.get("title")
        title_s = title if isinstance(title, str) else "?"
        severity = finding.get("severity")
        severity_s = severity if isinstance(severity, str) else "info"
        commands = finding.get("unraid_commands")
        if not isinstance(commands, list):
            continue
        for raw in commands:
            if not isinstance(raw, str):
                continue
            verdict = classify(raw)
            if not verdict.allowed:
                continue
            cmd = raw.strip()
            if cmd in seen:
                continue
            seen.add(cmd)
            out.append(
                ApplyAction(
                    command=cmd,
                    category=verdict.category,
                    finding_title=title_s,
                    fingerprint=finding_fingerprint(title_s),
                    severity=severity_s,
                )
            )
    return out


def diagnosis_from_plan(plan: object) -> dict[str, Any] | None:
    """Pull the validated ``diagnosis`` object out of a module ``plan.json`` blob."""
    if isinstance(plan, dict):
        diagnosis = plan.get("diagnosis")
        if isinstance(diagnosis, dict):
            return diagnosis
    return None


def actions_from_serialized(items: object) -> list[ApplyAction]:
    """Re-hydrate + RE-CLASSIFY pre-serialized actions (e.g. ``autoheal``'s plan).

    Modules like :mod:`modules.ops.autoheal` already store fully-formed actions as
    ``plan.json`` ``actions[]`` (``{command, category, finding_title, severity}``)
    instead of the ``diagnosis.findings[].unraid_commands`` shape. This re-classifies
    each command (defense in depth — only allow-listed, metachar-free, guard-passing
    commands survive) and recomputes the fingerprint from the title so the applied
    incident resolves the same row the proposing module recorded. De-duplicated.
    """
    out: list[ApplyAction] = []
    seen: set[str] = set()
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        command = item.get("command")
        if not isinstance(command, str):
            continue
        verdict = classify(command)
        if not verdict.allowed:
            continue
        cmd = command.strip()
        if cmd in seen:
            continue
        seen.add(cmd)
        title = item.get("finding_title")
        title_s = title if isinstance(title, str) else "?"
        severity = item.get("severity")
        severity_s = severity if isinstance(severity, str) else "info"
        out.append(
            ApplyAction(
                command=cmd,
                category=verdict.category,
                finding_title=title_s,
                fingerprint=finding_fingerprint(title_s),
                severity=severity_s,
            )
        )
    return out


def load_actions_from_file(path: Path) -> list[ApplyAction]:
    """Read a module's ``plan.json`` and return its applicable actions ([] on miss).

    Supports BOTH plan shapes: an AI ``diagnosis`` (logwatch/analyst) and a
    pre-serialized ``actions[]`` list (autoheal). The diagnosis takes precedence;
    the ``actions[]`` fallback lets operator-confirmed ``/apply`` also surface the
    restarts autoheal proposed.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    diagnosis = diagnosis_from_plan(data)
    if diagnosis is not None:
        return extract_actions(diagnosis)
    if isinstance(data, dict):
        return actions_from_serialized(data.get("actions"))
    return []


def collect_actions(plan_paths: Iterable[Path]) -> list[ApplyAction]:
    """Gather de-duplicated actions across several module ``plan.json`` files.

    Order-preserving union by command, so the same fix proposed by both the
    logwatch and analyst diagnoses is offered to the operator only once.
    """
    out: list[ApplyAction] = []
    seen: set[str] = set()
    for path in plan_paths:
        for action in load_actions_from_file(path):
            if action.command not in seen:
                seen.add(action.command)
                out.append(action)
    return out


Runner = Callable[[str], tuple[int, str]]


@dataclass(frozen=True)
class ApplyOutcome:
    """Result of (maybe) applying one action."""

    action: ApplyAction
    ran: bool
    returncode: int
    output: str
    error: str

    @property
    def ok(self) -> bool:
        return self.ran and self.returncode == 0


def default_runner(command: str, *, timeout: float = 120.0) -> tuple[int, str]:
    """Run an already-classified command via the single ``adapters/command`` boundary."""
    argv = shlex.split(command)
    result = command_adapter.run(argv, timeout=timeout)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def apply_action(action: ApplyAction, *, runner: Runner = default_runner) -> ApplyOutcome:
    """Re-classify (defense in depth) then run the action through ``runner``."""
    verdict = classify(action.command)
    if not verdict.allowed:
        return ApplyOutcome(action, ran=False, returncode=1, output="", error=verdict.reason)
    returncode, output = runner(action.command)
    return ApplyOutcome(action, ran=True, returncode=returncode, output=output, error="")
