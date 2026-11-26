"""Role-scoped MCP tool registry assembling bench, KB, and Multi-Harness Agent Mesh tools."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .handlers_agents import (
    tool_parhelion_agent_inbox,
    tool_parhelion_agent_messages,
    tool_parhelion_agent_peek,
    tool_parhelion_agent_report,
    tool_parhelion_agent_send,
    tool_parhelion_agent_spawn,
    tool_parhelion_agent_stop,
    tool_parhelion_agent_wait,
    tool_parhelion_harness_pool,
    tool_parhelion_sprint_start,
    tool_parhelion_squad,
    tool_parhelion_wait,
)
from .handlers_bench import (
    tool_parhelion_bench_diagnose,
    tool_parhelion_elf_cold3_check,
    tool_parhelion_pmu_card,
    tool_parhelion_status,
    tool_parhelion_submit_gate,
    tool_parhelion_test,
    tool_parhelion_test_cancel,
    tool_parhelion_test_plan,
    tool_parhelion_test_wait,
)
from .handlers_ledger import (
    tool_parhelion_ledger,
    tool_parhelion_note,
    tool_parhelion_recall,
)
from .handlers_report import tool_parhelion_report
from .spill_read import tool_parhelion_spill_read
from .handlers_escape import (
    tool_parhelion_escape_rebind,
    tool_parhelion_escape_reproducer,
    tool_parhelion_escape_status,
)
from .handlers_kb import (
    tool_parhelion_kb_feedback,
    tool_parhelion_kb_query,
    tool_parhelion_kb_record,
)
from .schemas import TOOL_SCHEMAS

ToolHandler = Callable[[dict[str, Any], Path], dict[str, Any]]
VALID_ROLES = ("orchestrator", "worker")
#: Worker subroles (4.3 P26 item 3). They narrow the worker table and are the only permission boundary:
#: every ``parhelion_*`` call passes through this registry, whatever the driver.
WORKER_SUBROLES = ("recon", "knowledge")
WORKER_ROLES = ("worker",) + WORKER_SUBROLES
#: Tools a subrole does not get at all. ``recon`` keeps ``parhelion_submit_gate`` for previews only.
SUBROLE_DENIED: dict[str, frozenset[str]] = {
    "recon": frozenset({
        "parhelion_test", "parhelion_test_wait", "parhelion_test_cancel", "parhelion_test_plan",
        "parhelion_escape_rebind", "parhelion_escape_reproducer",
    }),
    "knowledge": frozenset({
        "parhelion_test", "parhelion_test_wait", "parhelion_test_cancel", "parhelion_test_plan",
        "parhelion_escape_rebind", "parhelion_escape_reproducer", "parhelion_submit_gate",
        "parhelion_bench_diagnose", "parhelion_pmu_card", "parhelion_elf_cold3_check",
    }),
}
#: Tools a subrole may call only in their preview form (never to enqueue or change state).
SUBROLE_PREVIEW_ONLY = {"recon": ("parhelion_submit_gate",)}
WORKER_SCOPED_TEST_TOOLS = ("parhelion_test", "parhelion_test_wait", "parhelion_test_cancel", "parhelion_test_plan")
# Tools whose handler needs to know whether a worker or the orchestrator is calling.
ROLE_AWARE_TOOLS = WORKER_SCOPED_TEST_TOOLS + ("parhelion_wait", "parhelion_note", "parhelion_ledger",
                                               "parhelion_recall", "parhelion_spill_read", "parhelion_squad")
# Tools that check a worker acts on the problem its bus record names (escape locks, report views).
WORKER_SCOPED_PROBLEM_TOOLS = ("parhelion_escape_status", "parhelion_escape_rebind", "parhelion_escape_reproducer",
                               "parhelion_report")

TOOL_HANDLERS: dict[str, ToolHandler] = {
    "parhelion_status": tool_parhelion_status,
    "parhelion_report": tool_parhelion_report,
    "parhelion_spill_read": tool_parhelion_spill_read,
    "parhelion_bench_diagnose": tool_parhelion_bench_diagnose,
    "parhelion_pmu_card": tool_parhelion_pmu_card,
    "parhelion_elf_cold3_check": tool_parhelion_elf_cold3_check,
    "parhelion_kb_query": tool_parhelion_kb_query,
    "parhelion_kb_record": tool_parhelion_kb_record,
    "parhelion_kb_feedback": tool_parhelion_kb_feedback,
    "parhelion_test": tool_parhelion_test,
    "parhelion_test_wait": tool_parhelion_test_wait,
    "parhelion_test_cancel": tool_parhelion_test_cancel,
    "parhelion_test_plan": tool_parhelion_test_plan,
    "parhelion_submit_gate": tool_parhelion_submit_gate,
    "parhelion_note": tool_parhelion_note,
    "parhelion_ledger": tool_parhelion_ledger,
    "parhelion_recall": tool_parhelion_recall,
    "parhelion_escape_status": tool_parhelion_escape_status,
    "parhelion_escape_rebind": tool_parhelion_escape_rebind,
    "parhelion_escape_reproducer": tool_parhelion_escape_reproducer,
    "parhelion_harness_pool": tool_parhelion_harness_pool,
    "parhelion_sprint_start": tool_parhelion_sprint_start,
    "parhelion_agent_spawn": tool_parhelion_agent_spawn,
    "parhelion_agent_send": tool_parhelion_agent_send,
    "parhelion_agent_wait": tool_parhelion_agent_wait,
    "parhelion_agent_messages": tool_parhelion_agent_messages,
    "parhelion_agent_stop": tool_parhelion_agent_stop,
    "parhelion_agent_report": tool_parhelion_agent_report,
    "parhelion_agent_peek": tool_parhelion_agent_peek,
    "parhelion_agent_inbox": tool_parhelion_agent_inbox,
    "parhelion_squad": tool_parhelion_squad,
    "parhelion_wait": tool_parhelion_wait,
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {**schema, "roles": tuple(schema.get("roles") or ("orchestrator", "worker")), "handler": TOOL_HANDLERS[schema["name"]]}
    for schema in TOOL_SCHEMAS
]

_TOOL_ROLES: dict[str, tuple[str, ...]] = {t["name"]: t["roles"] for t in TOOL_DEFINITIONS}


def normalize_role(role: str | None) -> str:
    return "orchestrator" if role is None else str(role).strip().lower()


def tool_allowed(name: str, role: str | None = None) -> bool:
    """Whether a normalized role sees ``name``; a subrole sees the worker table minus its deny list."""
    roles = _TOOL_ROLES.get(name)
    if roles is None:
        return False
    norm = normalize_role(role)
    if norm in WORKER_SUBROLES:
        return "worker" in roles and name not in SUBROLE_DENIED[norm]
    return norm in roles


def is_tool_allowed_for_role(name: str, role: str | None = None) -> bool:
    return tool_allowed(name, role)


def list_tool_schemas(role: str | None = None) -> list[dict[str, Any]]:
    norm = normalize_role(role)
    return [
        {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
        for t in TOOL_DEFINITIONS
        if tool_allowed(t["name"], norm)
    ]


_SUBROLE_CACHE: dict[tuple[str, str], str] = {}


def worker_subrole(workspace: Path | None, agent_id: str | None) -> str:
    """The subrole of a worker seat, read from its bus record's role label ("" when plain or unknown).

    A seat is spawned with its role label (``recon``, ``knowledge``, ``integrator``, ...), so the bus
    record is what makes the filter effective without threading a role through every driver.
    """
    if workspace is None or not agent_id:
        return ""
    key = (str(Path(workspace).expanduser().resolve()), str(agent_id))
    if key in _SUBROLE_CACHE:
        return _SUBROLE_CACHE[key]
    role = ""
    try:
        from ..agents.bus_store import AgentBusStore

        record = AgentBusStore(Path(key[0])).get_agent(key[1]) or {}
        label = str(record.get("role_label") or "").strip().lower()
        role = label if label in WORKER_SUBROLES else ""
    except Exception:
        role = ""
    _SUBROLE_CACHE[key] = role
    return role


def call_tool(
    name: str,
    arguments: dict[str, Any] | None,
    workspace: Path,
    *,
    role: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        raise KeyError(f"Unknown tool: {name}")
    norm_role = normalize_role(role)
    if not tool_allowed(name, norm_role):
        raise PermissionError(f"Tool '{name}' is disabled for MCP role '{norm_role}'")
    if arguments is not None and not isinstance(arguments, dict):
        raise ValueError("Tool 'arguments' must be a JSON object")
    call_args = dict(arguments or {})
    call_args.pop("_caller_agent_id", None)
    if name in ROLE_AWARE_TOOLS or name in WORKER_SCOPED_PROBLEM_TOOLS:
        call_args["_worker_request"] = norm_role in WORKER_ROLES
    if norm_role in WORKER_ROLES and name == "parhelion_submit_gate" and call_args.get("force"):
        raise PermissionError("Worker submissions must pass the platform gate")
    if name in SUBROLE_PREVIEW_ONLY.get(norm_role, ()) and (call_args.get("enqueue") or call_args.get("force")):
        raise PermissionError(f"A {norm_role} seat may preview {name} but cannot enqueue a submission")
    if agent_id:
        call_args["_caller_agent_id"] = agent_id
    return handler(call_args, workspace.expanduser().resolve())


__all__ = [
    "SUBROLE_DENIED",
    "SUBROLE_PREVIEW_ONLY",
    "TOOL_DEFINITIONS",
    "TOOL_HANDLERS",
    "TOOL_SCHEMAS",
    "ToolHandler",
    "VALID_ROLES",
    "call_tool",
    "is_tool_allowed_for_role",
    "list_tool_schemas",
    "normalize_role",
    "tool_allowed",
    "tool_parhelion_agent_inbox",
    "tool_parhelion_agent_messages",
    "tool_parhelion_agent_peek",
    "tool_parhelion_agent_report",
    "tool_parhelion_agent_send",
    "tool_parhelion_agent_spawn",
    "tool_parhelion_agent_stop",
    "tool_parhelion_agent_wait",
    "tool_parhelion_bench_diagnose",
    "tool_parhelion_elf_cold3_check",
    "tool_parhelion_harness_pool",
    "tool_parhelion_kb_feedback",
    "tool_parhelion_kb_query",
    "tool_parhelion_kb_record",
    "tool_parhelion_ledger",
    "tool_parhelion_note",
    "tool_parhelion_pmu_card",
    "tool_parhelion_recall",
    "tool_parhelion_report",
    "tool_parhelion_spill_read",
    "tool_parhelion_status",
    "tool_parhelion_submit_gate",
    "tool_parhelion_test",
    "tool_parhelion_test_cancel",
    "tool_parhelion_test_plan",
    "tool_parhelion_test_wait",
    "tool_parhelion_wait",
    "WORKER_ROLES",
    "WORKER_SUBROLES",
    "worker_subrole",
]
