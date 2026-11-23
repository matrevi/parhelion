"""Focused 9-tool MCP registry assembling schemas and bench/KB handlers."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .handlers_bench import (
    tool_parhelion_bench_diagnose,
    tool_parhelion_elf_cold3_check,
    tool_parhelion_pmu_card,
    tool_parhelion_status,
    tool_parhelion_submit_gate,
    tool_parhelion_test_plan,
)
from .handlers_kb import (
    tool_parhelion_kb_feedback,
    tool_parhelion_kb_query,
    tool_parhelion_kb_record,
)
from .schemas import TOOL_SCHEMAS

ToolHandler = Callable[[dict[str, Any], Path], dict[str, Any]]

TOOL_HANDLERS: dict[str, ToolHandler] = {
    "parhelion_status": tool_parhelion_status,
    "parhelion_bench_diagnose": tool_parhelion_bench_diagnose,
    "parhelion_pmu_card": tool_parhelion_pmu_card,
    "parhelion_elf_cold3_check": tool_parhelion_elf_cold3_check,
    "parhelion_kb_query": tool_parhelion_kb_query,
    "parhelion_kb_record": tool_parhelion_kb_record,
    "parhelion_kb_feedback": tool_parhelion_kb_feedback,
    "parhelion_test_plan": tool_parhelion_test_plan,
    "parhelion_submit_gate": tool_parhelion_submit_gate,
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {**schema, "handler": TOOL_HANDLERS[schema["name"]]}
    for schema in TOOL_SCHEMAS
]


def list_tool_schemas() -> list[dict[str, Any]]:
    return [
        {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
        for t in TOOL_DEFINITIONS
    ]


def call_tool(name: str, arguments: dict[str, Any] | None, workspace: Path) -> dict[str, Any]:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        raise KeyError(f"Unknown tool: {name}")
    if arguments is not None and not isinstance(arguments, dict):
        raise ValueError("Tool 'arguments' must be a JSON object")
    return handler(arguments or {}, workspace.expanduser().resolve())


__all__ = [
    "TOOL_DEFINITIONS",
    "TOOL_HANDLERS",
    "TOOL_SCHEMAS",
    "ToolHandler",
    "call_tool",
    "list_tool_schemas",
    "tool_parhelion_bench_diagnose",
    "tool_parhelion_elf_cold3_check",
    "tool_parhelion_kb_feedback",
    "tool_parhelion_kb_query",
    "tool_parhelion_kb_record",
    "tool_parhelion_pmu_card",
    "tool_parhelion_status",
    "tool_parhelion_submit_gate",
    "tool_parhelion_test_plan",
]
