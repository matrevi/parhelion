"""MCP handlers of the experiment ledger: ``parhelion_note``, ``parhelion_ledger``, ``parhelion_recall``.

A worker writes to its own ledger of its own problem; the orchestrator names the problem and writes to
the ``platform`` ledger (or to a worker's ledger with ``worker=``). The role check of ``mcp.tools``
already limits which tools a role may call; this module adds the ownership rule: a bound worker can
neither read nor write another worker's ledger of another problem.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..ledger import auto, budget, digest, model, note, recall, store, view
from .paging import paginate

VIEW_HELP = "open|unexploited|failures|case:<id>|since:<ref>"
ROW_LIMIT = 40
FIELD_KEYS = ("claim", "why", "scope", "sha", "base_sha", "evidence", "confidence", "links", "supersedes",
              "revisit_if", "status", "predicted_effect", "test_plan", "priority", "resolution_note",
              "actionable_next", "est_gain", "cases", "approach", "why_failed", "cost_spent",
              "retry_conditions", "chosen", "rejected", "reason", "text", "rule_source",
              "expected_outcome", "lessons", "avoid_patterns", "try_patterns", "change_summary", "lesson")


def _caller(args: dict[str, Any]) -> tuple[str, str]:
    """``(role, agent_id)`` of the call; a worker call always carries its identity."""
    if args.get("_worker_request"):
        return "worker", str(args.get("_caller_agent_id") or "").strip()
    return "orchestrator", str(args.get("_caller_agent_id") or "").strip()


def problem_of(workspace: Path, agent_id: str) -> str:
    """The problem a worker is staffed on, from the bus (read-only); empty when unknown."""
    import sqlite3
    from ..agents.bus_store import agent_bus_db_path
    if not agent_id:
        return ""
    try:
        db = agent_bus_db_path(Path(workspace))
    except OSError:
        return ""
    if not db.is_file():
        return ""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return ""
    try:
        row = conn.execute("SELECT problem FROM agents WHERE agent_id = ?", (str(agent_id),)).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
    return str(row[0] or "") if row else ""


def owned_ledger(args: dict[str, Any], workspace: Path, *, write: bool) -> tuple[str, str]:
    """``(problem, owner)`` of the ledger this call may touch; raises for a cross-problem worker call."""
    role, agent_id = _caller(args)
    problem = str(args.get("problem") or "").strip()
    asked = str(args.get("worker") or "").strip()
    if role == "worker":
        mine = problem_of(workspace, agent_id)
        if not agent_id:
            raise PermissionError("A bound worker identity is required")
        if problem and mine and problem.lower() != mine.lower():
            raise PermissionError(f"You are staffed on {mine}; a worker cannot use another problem's ledger")
        problem = problem or mine  # 4.3.1: a bound worker need not name its own problem
        owner = agent_id
    else:
        if not problem:
            raise ValueError("'problem' is required for the orchestrator")
        owner = asked or auto.PLATFORM_WORKER
    if not problem:
        raise ValueError("the problem of this call is unknown: pass problem")
    return problem, owner


def _ledger(args: dict[str, Any], workspace: Path, *, write: bool) -> tuple[Any, str, str]:
    problem, owner = owned_ledger(args, workspace, write=write)
    return store.Ledger(workspace, problem, owner), problem, owner


def row_line(row: dict[str, Any], own: str = "") -> str:
    """One line per entry: id, type, claim and the number that matters (#4)."""
    ref = digest.ref(row, own)
    kind = str(row.get("type") or "")
    extra = digest.key_numbers(row)
    if kind == "measurement":
        extra = f" {row.get('value')}{row.get('unit') or ''}" + (f" {row.get('key')}" if row.get("key") else "")
    elif kind == "failure":
        extra = " " + str(row.get("why_failed") or "")[:90]
    elif kind == "hypothesis":
        extra = f" [{row.get('status')} p{row.get('priority', 3)}]" + digest.key_numbers(row)
    sha = str(row.get("sha") or "")[:7]
    head = f"{ref} {kind}" + (f" {sha}" if sha else "")
    claim = str(row.get("claim") or row.get("text") or "")[:200]
    return f"{head}: {claim}{extra}"


def tool_parhelion_ledger(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """A named view of one ledger, one line per entry (#4); offset/cursor walk long views."""
    led, problem, owner = _ledger(args, Path(workspace), write=False)
    rows = led.rows()
    spec = str(args.get("view") or "open").strip() or "open"
    try:
        picked = view.query(rows, spec)
    except Exception as exc:  # noqa: BLE001 - a bad view is a caller error, reported as such
        return {"ok": False, "error": str(exc), "views": VIEW_HELP}
    out = {
        "ok": True, "problem": problem, "worker": owner, "view": spec, "total": len(rows),
        "matched": len(picked), "lines": [row_line(r, owner) for r in picked],
        "warnings": budget.warnings(rows),
    }
    paginate(out, "lines", args, total=len(picked), default=20, maximum=ROW_LIMIT)
    out["shown"] = len(out["lines"])
    return out


def tool_parhelion_note(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Write one model-authored entry (or update one through ``supersedes``/``id``) and return its id."""
    led, problem, owner = _ledger(args, Path(workspace), write=True)
    # Every field the caller passed reaches the validator, so a platform-owned field comes back as an
    # error instead of being dropped silently.
    fields = {k: v for k, v in args.items() if not k.startswith("_") and k not in ("problem", "worker")}
    entry_type = str(fields.pop("type", "") or "").strip()
    target = note.target_of(fields)
    if not entry_type and target:  # an update keeps the stored row's type
        entry_type = str((view.head(led.rows(), target) or {}).get("type") or "")
    if not entry_type:
        types = ("hypothesis", "experiment", "insight", "failure", "decision", "constraint")
        return {"ok": False, "error": "type is required: one of " + ", ".join(types), "written": False}
    try:
        row = note.write(led, {"type": entry_type, **fields}, source="worker")
    except model.LedgerError as exc:
        return {"ok": False, "error": str(exc), "written": False}
    return {"ok": True, "problem": problem, "worker": owner, "id": row["id"], "ref": view.ref(row),
            "type": row.get("type"), "status": row.get("status"), "confidence": row.get("confidence"),
            "line": row_line(row, owner), "written": not row.get("duplicate"),
            "warnings": budget.warnings(led.rows())}


def tool_parhelion_recall(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Search the ledger, the handoffs and this worker's session log: snippets with ``file#line``."""
    role, agent_id = _caller(args)
    problem = str(args.get("problem") or "").strip()
    if role == "worker":
        problem = problem or problem_of(Path(workspace), agent_id)
        if problem and problem_of(Path(workspace), agent_id) and \
                problem.lower() != problem_of(Path(workspace), agent_id).lower():
            raise PermissionError("A worker recalls its own problem's records only")
    if not problem:
        raise ValueError("'problem' is required for the orchestrator")
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("'query' is required")
    scope = agent_id if role == "worker" or not str(args.get("worker") or "").strip() else str(args["worker"]).strip()
    hits = recall.search(Path(workspace), problem, scope, query, limit=args.get("limit") or recall.RESULTS_DEFAULT,
                         refresh=bool(args.get("refresh", False)))
    rows = store.Ledger(Path(workspace), problem, scope or auto.PLATFORM_WORKER).rows()
    return {"ok": True, "problem": problem, "worker": scope, "query": query, "results": hits,
            "count": len(hits), "index": str(recall.cache_dir(Path(workspace))),
            "warnings": budget.warnings(rows) if rows else []}
