"""Parhelion Local MCP Server (`stdio` + `/v1/mcp` loopback HTTP handler)."""
from __future__ import annotations

from .server import (
    MCP_PROTOCOL_VERSION,
    SERVER_INFO,
    handle_jsonrpc_message,
    handle_mcp_http_request,
    handle_mcp_payload,
    serve_stdio,
)
from .tools import TOOL_DEFINITIONS, TOOL_HANDLERS, call_tool, list_tool_schemas

__all__ = [
    "MCP_PROTOCOL_VERSION",
    "SERVER_INFO",
    "TOOL_DEFINITIONS",
    "TOOL_HANDLERS",
    "call_tool",
    "handle_jsonrpc_message",
    "handle_mcp_http_request",
    "handle_mcp_payload",
    "list_tool_schemas",
    "serve_stdio",
]
