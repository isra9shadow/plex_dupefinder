"""MCP protocol core — tool specs + a pure JSON-RPC responder (stdlib, testable).

izumi exposes its read-only doctors and the guard-vetted apply as **MCP tools** so an
external assistant (Claude Desktop, Home Assistant) can query and (with the same
allow-list) act on the homelab. This module is the PURE half: the tool catalogue and
:func:`jsonrpc_response`, which turns one JSON-RPC request into its response using an
injected ``call_tool`` — no I/O, fully unit-tested. ``mcp_server.py`` wraps it in a
stdio loop and wires ``call_tool`` to the real modules/apply.

Tools:
  * ``health_sweep`` (read-only) — run the read-only health modules, return summaries.
  * ``run_doctor`` (read-only) — run one doctor by name, return its summary.
  * ``list_fixes`` (read-only) — the guard-vetted actions currently proposed.
  * ``apply_fix`` (GATED) — apply ONE allow-listed command through aictx.apply.

The action tool stays behind the same positive allow-list as everything else, so the
audited boundary is unchanged even when an external agent drives it.
"""

from __future__ import annotations

from collections.abc import Callable

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "izumi", "version": "1.0.0"}

_READONLY_DOCTORS = (
    "uptime",
    "dbcheck",
    "diskwatch",
    "permsdoctor",
    "backupaudit",
    "netdoctor",
    "certdoctor",
    "capacitydoctor",
    "status",
    "configcheck",
    "shadowcheck",
)

TOOLS: list[dict[str, object]] = [
    {
        "name": "health_sweep",
        "description": "Corre los chequeos de salud read-only (servicios, discos, DB, "
        "permisos, red, certificados, capacidad) y devuelve un resumen consolidado.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_doctor",
        "description": "Corre UN módulo de diagnóstico read-only y devuelve su resumen.",
        "inputSchema": {
            "type": "object",
            "properties": {"doctor": {"type": "string", "enum": list(_READONLY_DOCTORS)}},
            "required": ["doctor"],
        },
    },
    {
        "name": "list_fixes",
        "description": "Lista las acciones seguras propuestas (allow-list: docker "
        "restart/start/stop, chmod, chown, mkdir) que se podrían aplicar.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "apply_fix",
        "description": "Aplica UNA acción de la allow-list (p.ej. 'docker restart sonarr'). "
        "Re-verificada contra el guard antes de ejecutar; nada fuera de la allow-list corre.",
        "inputSchema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]

# call_tool(name, arguments) -> (text, is_error)
CallTool = Callable[[str, "dict[str, object]"], "tuple[str, bool]"]


def _error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _result(request_id: object, result: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_response(
    request: dict[str, object], *, call_tool: CallTool
) -> dict[str, object] | None:
    """Turn one JSON-RPC request into its response (None for notifications).

    Handles the MCP core methods: ``initialize``, ``tools/list``, ``tools/call``,
    ``ping``. A notification (no ``id``) yields None. Unknown methods return a
    JSON-RPC "method not found" error.
    """
    method = request.get("method")
    request_id = request.get("id")
    if request_id is None:  # notification (e.g. notifications/initialized) → no reply
        return None
    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        params = request.get("params")
        params_d = params if isinstance(params, dict) else {}
        name = params_d.get("name")
        args = params_d.get("arguments")
        args_d = args if isinstance(args, dict) else {}
        if not isinstance(name, str) or name not in {str(t["name"]) for t in TOOLS}:
            return _error(request_id, -32602, f"unknown tool: {name!r}")
        text, is_error = call_tool(name, args_d)
        return _result(
            request_id,
            {"content": [{"type": "text", "text": text}], "isError": is_error},
        )
    return _error(request_id, -32601, f"method not found: {method!r}")
