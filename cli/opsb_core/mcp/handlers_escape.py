"""MCP entry to escape locks (4.1 #12): state, reviewed-trial rebinding and reproducer runs.

A worker may act only on the problem its bus record names; the orchestrator on every problem.
The tools wrap the platform's own rules and change nothing else:

- ``parhelion_escape_status``: every WA/RE receipt of the problem with its reproducer state
  (scheduler.wa_repro: pending / reproduced / fixed), its reviewed-trial binding and whether that
  trial was spent, and the next step of the active lock;
- ``parhelion_escape_rebind``: ``case_feedback.authorize_review_trial`` for the active lock. It
  binds a repair only while the bound one was neither submitted nor queued; a refusal writes nothing;
- ``parhelion_escape_reproducer``: the receipt's variants and the exact ``parhelion_test`` calls.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from .. import config as _cfg
from ..agents.bus_store import AgentBusStore

FAILED = ("Wrong Answer", "Runtime Error")


def _scheduler():
    if str(_cfg.SUPERVISOR_ROOT) not in sys.path:
        sys.path.insert(0, str(_cfg.SUPERVISOR_ROOT))
    from scheduler import case_bench, case_feedback, receipts, wa_repro

    return case_bench, case_feedback, receipts, wa_repro


def _title(workspace: Path, problem: Any) -> str:
    case_bench = _scheduler()[0]
    wanted = str(problem or "").strip()
    if not wanted:
        raise ValueError("'problem' is required")
    for row in case_bench.load_problem_manifest(workspace):
        if str(row.get("title") or "").lower() == wanted.lower():
            return str(row["title"])
    raise ValueError(f"Unknown problem: {wanted}")


def _authorize(args: dict[str, Any], workspace: Path, title: str) -> None:
    if not args.get("_worker_request"):
        return
    agent_id = str(args.get("_caller_agent_id") or "").strip()
    if not agent_id:
        raise PermissionError("A bound worker identity is required")
    agent = AgentBusStore(workspace).get_agent(agent_id) or {}
    owned = str(agent.get("problem") or "")
    if owned.lower() != title.lower():
        raise PermissionError(f"Workers act only on their own problem ({owned or 'none'}), not {title}")


def _escape_state(workspace: Path, title: str) -> dict[str, Any]:
    _scheduler()
    from scheduler.estimator import ScoreEstimator

    return ScoreEstimator(workspace).get_calibrator().inspect_escape_state(title)


def _trial(workspace: Path, title: str, bad_sha: str, sid: str) -> dict[str, Any] | None:
    case_bench, case_feedback, _, _ = _scheduler()
    folder = workspace / "results" / title / bad_sha
    path = case_feedback.trial_path(folder, sid)
    doc = case_bench.read_doc(path) if path.exists() else {}
    good = doc.get("good_sha")
    if not good:
        return None
    return {"bound_sha": good, "spent": bool(case_feedback.trial_spent(folder, good)), "replaces": doc.get("replaces"),
            "review": str(doc.get("review") or "")[:200], "file": str(path)}


def _failed_receipts(workspace: Path, title: str) -> list[dict[str, Any]]:
    receipts = _scheduler()[2]
    return [r for r in receipts.load(workspace, title) if r.get("status") in FAILED and r.get("submission_id")
            and re.fullmatch(r"[0-9a-f]{40}", str(r.get("sha") or ""))]


def tool_parhelion_escape_status(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    title = _title(workspace, args.get("problem"))
    _authorize(args, workspace, title)
    wa_repro = _scheduler()[3]
    state = _escape_state(workspace, title)
    active_sid = state.get("escape_submission_id") if state.get("escape_lock") else None
    locks = []
    for row in _failed_receipts(workspace, title)[-10:]:
        sid, bad = str(row["submission_id"]), str(row["sha"])
        view = wa_repro.status(workspace, title, sid, bad)
        trial = _trial(workspace, title, bad, sid)
        step = wa_repro.next_step(view)
        if view["state"] == "none":
            step = view.get("reason") or f"plan reproducers: opsb bench wa-repro {title} --receipt {sid} --apply"
        if trial and not trial["spent"] and view["state"] != "fixed":
            step += f"; reviewed trial bound to {trial['bound_sha'][:7]} (unused, can be rebound)"
        locks.append({"submission_id": sid, "sha": bad[:7], "status": row.get("status"), "active": sid == active_sid,
                      "repro": {k: view.get(k) for k in ("state", "reproduced_run", "failing_variants")}
                      | {"variants": len(view.get("variants") or [])},
                      "trial": trial, "next_step": step})
    active = next((lock for lock in locks if lock["active"]), None)
    return {"problem": title, "escape_lock": bool(state.get("escape_lock")), "active_submission_id": active_sid,
            "active_sha": str(state.get("escape_sha") or "")[:7] or None,
            "next_step": active["next_step"] if active else ("no active escape lock" if not active_sid else ""),
            "locks": locks}


def tool_parhelion_escape_rebind(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    title = _title(workspace, args.get("problem"))
    _authorize(args, workspace, title)
    case_bench, case_feedback, _, _ = _scheduler()
    receipt, sha = str(args.get("receipt") or "").strip(), str(args.get("sha") or "").strip()
    if not receipt or not sha:
        raise ValueError("'receipt' and 'sha' are required")
    state = _escape_state(workspace, title)
    if not state.get("escape_lock") or state.get("escape_submission_id") != receipt:
        return {"ok": False, "reason": "That receipt is not the problem's active escape lock"}
    from scheduler.estimator import resolve_sha

    repo = case_bench.problem_repo(workspace, title)
    good_sha, bad_sha = resolve_sha(workspace, title, sha), str(state.get("escape_sha") or "")
    folders = {key: workspace / "results" / title / value for key, value in (("bad", bad_sha), ("good", good_sha))}
    evidence = {key: case_bench.formal_evidence(folder, repo, case_bench.read_doc(folder / "result.json"))
                for key, folder in folders.items()}
    if not evidence["good"]:
        return {"ok": False, "reason": f"{good_sha[:7]} has no formal run of the current suite; run parhelion_test first"}
    review = str(args.get("review") or "").strip() or f"rebound via MCP by {args.get('_caller_agent_id') or 'orchestrator'}"
    result = case_feedback.authorize_review_trial(repo, folders["bad"], evidence["bad"], evidence["good"], state, review)
    return {**result, "trial_file": str(case_feedback.trial_path(folders["bad"], receipt))}


def tool_parhelion_escape_reproducer(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    title = _title(workspace, args.get("problem"))
    _authorize(args, workspace, title)
    wa_repro = _scheduler()[3]
    receipt = str(args.get("receipt") or "").strip()
    if not receipt:
        raise ValueError("'receipt' is required")
    row = next((r for r in _failed_receipts(workspace, title) if r["submission_id"] == receipt), None)
    entry = wa_repro.load_manifest(workspace, title)["receipts"].get(receipt) or {}
    bad = str((row or {}).get("sha") or entry.get("sha") or "")
    view = wa_repro.status(workspace, title, receipt, bad)
    if not view["variants"]:
        return {"problem": title, "submission_id": receipt, "state": "none", "variants": [],
                "reason": entry.get("reason") or "no reproducers for this receipt yet",
                "next_step": f"orchestrator: opsb bench wa-repro {title} --receipt {receipt} --apply"}
    return {"problem": title, "submission_id": receipt, "bad_sha": bad[:7], "state": view["state"],
            "variants": view["variants"], "reproduced_run": view.get("reproduced_run"),
            "failing_variants": view.get("failing_variants"),
            "reproduce": {"tool": "parhelion_test", "arguments": {"problem": title, "sha": bad, "level": "final"}},
            "verify_fix": {"tool": "parhelion_test", "arguments": {"problem": title, "sha": "<repair SHA>", "level": "final"}},
            "next_step": wa_repro.next_step(view)}
