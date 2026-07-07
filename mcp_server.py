#!/usr/bin/env python3
"""izumi MCP server (stdio) — let an external assistant operate the homelab.

Exposes izumi's READ-ONLY doctors and the guard-vetted apply as MCP tools over a
newline-delimited JSON-RPC stdio transport (stdlib only — no `mcp` dependency). An
MCP client (Claude Desktop, Home Assistant) launches it, e.g.:

    docker run --rm -i -v /var/run/docker.sock:/var/run/docker.sock \
      -v "$REPO:/app" -v /mnt/user:/mnt/user -v /mnt/cache:/mnt/cache -w /app \
      izumi-organizer:local python mcp_server.py

The protocol core (tool specs + request routing) lives in aictx/mcp_tools.py and is
unit-tested; this file wires the tools to the real modules + apply and runs the loop.
``apply_fix`` re-vets the command against the SAME allow-list, so an external agent
can never run anything outside it.
"""

from __future__ import annotations

import json
import sys
from typing import TextIO

from aictx import mcp_tools
from aictx.apply import ApplyAction, apply_action, classify, collect_actions, finding_fingerprint
from core import config as config_mod
from core import registry
from core.types import RunContext
from run import build_context

_HEALTH_MODULES = (
    "uptime",
    "dbcheck",
    "permsdoctor",
    "backupaudit",
    "netdoctor",
    "certdoctor",
    "capacitydoctor",
)
_FIX_SOURCES = ("logwatch", "analyst", "autoheal", "permsdoctor")


def _read_summary(ctx: RunContext, module: str) -> str:
    path = ctx.config.reporting.dir / module / "summary.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return "(sin datos)"


def _run_doctor(ctx: RunContext, doctor: str) -> str:
    fn = registry.get(doctor)
    if fn is None:
        return f"módulo desconocido: {doctor}"
    try:
        fn(ctx)
    except Exception as exc:  # a broken module must not crash the server
        return f"{doctor} falló: {exc}"
    return _read_summary(ctx, doctor)


def _health_sweep(ctx: RunContext) -> str:
    parts = []
    for module in _HEALTH_MODULES:
        parts.append(f"### {module}\n{_run_doctor(ctx, module)}")
    return "\n\n".join(parts)


def _list_fixes(ctx: RunContext) -> str:
    paths = [ctx.config.reporting.dir / sub / "plan.json" for sub in _FIX_SOURCES]
    actions = collect_actions(paths)
    if not actions:
        return "No hay acciones aplicables (lanza antes analyst/autoheal/permsdoctor)."
    return "\n".join(f"- [{a.severity}] {a.command}  ({a.finding_title})" for a in actions)


def _apply_fix(ctx: RunContext, command: str) -> tuple[str, bool]:
    verdict = classify(command)
    if not verdict.allowed:
        return f"rechazado (no en la allow-list): {verdict.reason}", True
    action = ApplyAction(
        command=command,
        category=verdict.category,
        finding_title="mcp apply",
        fingerprint=finding_fingerprint(command),
        severity="info",
    )
    outcome = apply_action(action)
    if outcome.ok:
        return f"aplicado: {command}\n{outcome.output}".strip(), False
    return f"falló: {command}\n{outcome.output or outcome.error}".strip(), True


def make_call_tool(ctx: RunContext) -> mcp_tools.CallTool:
    """Bind the MCP tool names to izumi's real capabilities."""

    def call_tool(name: str, arguments: dict[str, object]) -> tuple[str, bool]:
        try:
            if name == "health_sweep":
                return _health_sweep(ctx), False
            if name == "run_doctor":
                doctor = arguments.get("doctor")
                if not isinstance(doctor, str):
                    return "falta 'doctor'", True
                return _run_doctor(ctx, doctor), False
            if name == "list_fixes":
                return _list_fixes(ctx), False
            if name == "apply_fix":
                command = arguments.get("command")
                if not isinstance(command, str) or not command.strip():
                    return "falta 'command'", True
                return _apply_fix(ctx, command.strip())
            return f"tool desconocida: {name}", True
        except Exception as exc:  # never let a tool crash the server
            return f"error interno: {exc}", True

    return call_tool


def serve(stdin: TextIO, stdout: TextIO, call_tool: mcp_tools.CallTool) -> None:
    """Newline-delimited JSON-RPC stdio loop (one message per line)."""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue
        response = mcp_tools.jsonrpc_response(request, call_tool=call_tool)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    cfg = config_mod.load()
    registry.discover()
    ctx = build_context(cfg)
    serve(sys.stdin, sys.stdout, make_call_tool(ctx))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
