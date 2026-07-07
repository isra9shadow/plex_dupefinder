"""Tests for aictx.mcp_tools (MCP JSON-RPC core) and mcp_server.serve loop."""

from __future__ import annotations

import io
import json

from aictx import mcp_tools


def _fake_call(name: str, args: dict) -> tuple[str, bool]:
    return f"ran {name} with {sorted(args)}", False


def test_initialize_reports_protocol_and_server() -> None:
    resp = mcp_tools.jsonrpc_response(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, call_tool=_fake_call
    )
    assert resp is not None
    result = resp["result"]
    assert result["protocolVersion"] == mcp_tools.PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "izumi"
    assert "tools" in result["capabilities"]


def test_tools_list_returns_catalogue() -> None:
    resp = mcp_tools.jsonrpc_response(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, call_tool=_fake_call
    )
    names = {t["name"] for t in resp["result"]["tools"]}  # type: ignore[index]
    assert {"health_sweep", "run_doctor", "list_fixes", "apply_fix"} <= names


def test_tools_call_dispatches_to_handler() -> None:
    resp = mcp_tools.jsonrpc_response(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "run_doctor", "arguments": {"doctor": "uptime"}},
        },
        call_tool=_fake_call,
    )
    content = resp["result"]["content"]  # type: ignore[index]
    assert content[0]["type"] == "text"
    assert "ran run_doctor" in content[0]["text"]
    assert resp["result"]["isError"] is False  # type: ignore[index]


def test_tools_call_unknown_tool_is_error() -> None:
    resp = mcp_tools.jsonrpc_response(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}},
        call_tool=_fake_call,
    )
    assert resp["error"]["code"] == -32602  # type: ignore[index]


def test_notification_yields_no_response() -> None:
    resp = mcp_tools.jsonrpc_response(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}, call_tool=_fake_call
    )
    assert resp is None


def test_unknown_method_is_method_not_found() -> None:
    resp = mcp_tools.jsonrpc_response(
        {"jsonrpc": "2.0", "id": 5, "method": "frobnicate"}, call_tool=_fake_call
    )
    assert resp["error"]["code"] == -32601  # type: ignore[index]


def test_ping() -> None:
    resp = mcp_tools.jsonrpc_response(
        {"jsonrpc": "2.0", "id": 6, "method": "ping"}, call_tool=_fake_call
    )
    assert resp["result"] == {}  # type: ignore[index]


def test_serve_loop_reads_and_writes_ndjson() -> None:
    import mcp_server

    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})  # no reply
        + "\n"
    )
    stdout = io.StringIO()
    mcp_server.serve(stdin, stdout, _fake_call)
    lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1  # only the tools/list request got a response
    assert json.loads(lines[0])["id"] == 1
