"""Pure Python 3 stdlib MCP JSON-RPC 2.0 server (`stdio` + in-process HTTP handler)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import IO, Any

from .. import config as _cfg
from ..drivers.base import load_parhelion_config
from .tools import TOOL_HANDLERS, call_tool, list_tool_schemas

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "parhelion-mcp", "version": "3.0.2"}


def _resolve_workspace(workspace: Path | str | None) -> Path:
    if isinstance(workspace, Path):
        return workspace.expanduser().resolve()
    cfg = load_parhelion_config()
    return _cfg.ConfigRepository.resolve_workspace(workspace, cfg)


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _jsonrpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def handle_jsonrpc_message(msg: Any, workspace: Path | str | None = None) -> dict[str, Any] | None:
    """Dispatch a single decoded JSON-RPC 2.0 request/notification dict."""
    if not isinstance(msg, dict):
        return _jsonrpc_error(None, -32600, "Invalid Request: expected JSON object")

    req_id = msg.get("id")
    if "jsonrpc" in msg and msg.get("jsonrpc") != "2.0":
        return _jsonrpc_error(req_id, -32600, "Invalid Request: jsonrpc must be '2.0'")

    method = msg.get("method")
    if not isinstance(method, str) or not method.strip():
        return _jsonrpc_error(req_id, -32600, "Invalid Request: missing method")

    # Notifications carry no id (or notifications/*) and must not emit a response
    if req_id is None and method.startswith("notifications/"):
        return None
    if method == "notifications/initialized":
        return None if req_id is None else _jsonrpc_result(req_id, {})

    if method == "ping":
        return _jsonrpc_result(req_id, {}) if req_id is not None else None

    if method == "initialize":
        return _jsonrpc_result(
            req_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": dict(SERVER_INFO),
            },
        )

    if method == "tools/list":
        return _jsonrpc_result(req_id, {"tools": list_tool_schemas()})

    if method == "tools/call":
        params = msg.get("params")
        if not isinstance(params, dict):
            return _jsonrpc_error(req_id, -32602, "Invalid params: expected object")
        tool_name = params.get("name")
        if not isinstance(tool_name, str) or not tool_name.strip():
            return _jsonrpc_error(req_id, -32602, "Invalid params: missing tool name")
        if tool_name not in TOOL_HANDLERS:
            return _jsonrpc_error(req_id, -32602, f"Unknown tool: {tool_name}")

        raw_args = params.get("arguments")
        if raw_args is not None and not isinstance(raw_args, dict):
            return _jsonrpc_error(req_id, -32602, "Invalid params: arguments must be an object")

        ws = _resolve_workspace(workspace)
        try:
            payload = call_tool(tool_name, raw_args or {}, ws)
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            return _jsonrpc_result(
                req_id,
                {"content": [{"type": "text", "text": text}], "isError": False},
            )
        except Exception as exc:
            err_text = json.dumps({"ok": False, "tool": tool_name, "error": str(exc)}, ensure_ascii=False)
            return _jsonrpc_result(
                req_id,
                {"content": [{"type": "text", "text": err_text}], "isError": True},
            )

    if req_id is None:
        return None
    return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")


def handle_mcp_payload(payload: Any, workspace: Path | str | None = None) -> Any:
    """Handle single JSON-RPC object or batch array."""
    if isinstance(payload, list):
        if not payload:
            return _jsonrpc_error(None, -32600, "Invalid Request: empty batch")
        responses = []
        for item in payload:
            resp = handle_jsonrpc_message(item, workspace=workspace)
            if resp is not None:
                responses.append(resp)
        return responses if responses else None
    return handle_jsonrpc_message(payload, workspace=workspace)


def serve_stdio(
    workspace: Path | str | None = None,
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Run line-delimited JSON-RPC 2.0 MCP server over stdio."""
    in_stream = stdin or sys.stdin
    out_stream = stdout or sys.stdout
    err_stream = stderr or sys.stderr
    ws = _resolve_workspace(workspace)

    for raw_line in in_stream:
        line = raw_line.strip()
        if not line:
            continue
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError as exc:
            err_stream.write(f"[parhelion-mcp] parse error: {exc}\n")
            err_stream.flush()
            resp = _jsonrpc_error(None, -32700, f"Parse error: {exc}")
            out_stream.write(json.dumps(resp, ensure_ascii=False, separators=(",", ":")) + "\n")
            out_stream.flush()
            continue

        try:
            resp = handle_mcp_payload(decoded, workspace=ws)
        except Exception as exc:
            err_stream.write(f"[parhelion-mcp] internal error: {exc}\n")
            err_stream.flush()
            req_id = decoded.get("id") if isinstance(decoded, dict) else None
            resp = _jsonrpc_error(req_id, -32603, f"Internal error: {exc}")

        if resp is not None:
            out_stream.write(json.dumps(resp, ensure_ascii=False, separators=(",", ":")) + "\n")
            out_stream.flush()
    return 0


def handle_mcp_http_request(
    body_bytes: bytes,
    workspace: Path | str | None = None,
) -> tuple[int, Any]:
    """In-process HTTP JSON-RPC 2.0 handler ready to mount at `POST /v1/mcp` in `opsb daemon`."""
    if not body_bytes or not body_bytes.strip():
        return 400, _jsonrpc_error(None, -32700, "Parse error: empty request body")
    try:
        decoded = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return 400, _jsonrpc_error(None, -32700, f"Parse error: {exc}")

    ws = _resolve_workspace(workspace)
    resp = handle_mcp_payload(decoded, workspace=ws)
    if resp is None:
        return 202, None
    return 200, resp
