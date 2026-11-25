"""MCP tool handlers for status, benchmark diagnosis, fast-feedback cards, test planning & submit gate."""
from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from typing import Any

from .. import config as _cfg
from ..drivers.base import load_parhelion_config
from ..ipc_bridge import invoke_bridge, merge_bridge_enrichment
from ..services.contest_service import resolve_workspace_meta
from ..services.queue_service import compute_workspace_snapshot
from ..services.sync_reconciler import has_pending_sync_error_for_commit


def _find_problem(problems: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    q = query.strip().lower()
    return next((p for p in problems if str(p.get("title", "")).lower() == q), None)


def _problem_commit(workspace: Path, problem: str, revision: str) -> tuple[str, str]:
    prob = _find_problem(resolve_workspace_meta(workspace).get("problems") or [], problem)
    if not prob:
        raise ValueError(f"Unknown problem: {problem}")
    repo = workspace / prob["path"]
    if not (repo / ".git").exists():
        raise ValueError(f"No local problem repository for {problem}")
    ref = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options",
                          revision + "^{commit}"], capture_output=True, text=True, timeout=3)
    if ref.returncode:
        raise ValueError(f"Cannot resolve {revision} for {problem}")
    return prob["title"], ref.stdout.strip()


def _latest_result_dir(workspace: Path, title: str, sha: str | None = None) -> Path | None:
    res_root = workspace / "results" / title
    if not res_root.is_dir():
        return None
    if sha:
        return next((c for c in res_root.iterdir() if c.is_dir() and c.name.startswith(sha[:7])), None)
    dirs = [d for d in res_root.iterdir() if d.is_dir() and (d / "result.json").is_file()]
    if not dirs:
        return None
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return dirs[0]


def tool_parhelion_status(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    cfg = load_parhelion_config()
    snap = compute_workspace_snapshot(workspace, cfg)
    try:
        bridge = invoke_bridge(workspace, "status", ["--offline"], use_cache=True)
        if isinstance(bridge, dict):
            merge_bridge_enrichment(snap, bridge)
    except Exception:
        pass
    prob_filter = str(args.get("problem") or "").strip().lower()
    include_npu = bool(args.get("include_npu", False))
    include_queue = bool(args.get("include_queue", True))

    problems = snap.get("problems") or []
    if prob_filter:
        problems = [p for p in problems if str(p.get("title", "")).lower() == prob_filter]
    compact_problems = [
        {
            "title": p.get("title"), "difficulty": p.get("difficulty"), "status": p.get("status"),
            "current_score": p.get("current_score"), "best_score": p.get("best_score"),
            "leader_score": p.get("leader_score"), "rank": p.get("rank"), "wspt_tag": p.get("wspt_tag"),
            "head_sha": str(p.get("head_sha") or "")[:12], "best_sha": str(p.get("best_sha") or "")[:12],
            "escape_lock": bool((p.get("calibration") or {}).get("escape_lock")),
        }
        for p in problems
    ]

    npu_state: dict[str, Any] = {"online": False, "skipped": True}
    if include_npu:
        try:
            from ..services.npu_service import poll_npu_telemetry

            ssh_host = cfg.get("npu_ssh_host") or snap["contest"].get("npu_ssh_host") or "npu"
            npu_state = poll_npu_telemetry(ssh_host, {p["title"]: p for p in (snap.get("problems") or [])})
        except Exception as exc:
            npu_state = {"online": False, "error": str(exc)}

    sched = snap.get("scheduler") or {}
    out: dict[str, Any] = {
        "sampled_at": snap.get("sampled_at"),
        "contest": {k: snap["contest"].get(k) for k in ("title", "workspace", "weighted_score", "max_weighted_score", "raw_score", "passing_count", "problem_count")},
        "scheduler": {k: sched.get(k) for k in ("rate_ready", "cooldown_remaining_sec", "used_today_24h", "daily_quota_limit", "closing_reserve", "available_normal_shots", "noise_floor_delta", "actionable_count")},
        "npu": npu_state,
        "problems": compact_problems,
    }
    if include_queue:
        q_items = snap.get("submission_queue") or []
        if prob_filter:
            q_items = [q for q in q_items if str(q.get("title", "")).lower() == prob_filter]
        out["queue_preview"] = q_items[:12]
    return out


def tool_parhelion_bench_diagnose(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    snap = compute_workspace_snapshot(workspace, load_parhelion_config())
    prob_q = str(args.get("problem") or "").strip()
    targets = snap.get("problems") or []
    if prob_q:
        matched = _find_problem(targets, prob_q)
        if matched is None:
            raise ValueError(f"Unknown problem '{prob_q}' in workspace {workspace}")
        targets = [matched]

    diag_items: list[dict[str, Any]] = []
    observations = _cfg.read_json(workspace / "opsb.workspace.json", {}).get("bench_observations", {})
    for p in targets:
        cal = dict(p.get("calibration") or {})
        strata = sorted(cal.get("strata") or [], key=lambda s: float(s.get("headroom_pts") or 0.0), reverse=True)
        diag_items.append({
            "measurement_notes": observations.get(p["title"], []),
            "title": str(p["title"]), "current_score": p.get("current_score"), "best_score": p.get("best_score"),
            "mae_pts": cal.get("mae_pts"), "loocv_mae_pts": cal.get("loocv_mae_pts"),
            "loocv_spearman": cal.get("loocv_spearman"), "calibration_trust": cal.get("calibration_trust"),
            "last_residual_pts": cal.get("last_residual_pts"), "escape_lock": bool(cal.get("escape_lock")),
            "escape_sha": cal.get("escape_sha"), "top_headroom_strata": strata[:3],
            "strong_positive_buckets": cal.get("strong_positive_buckets") or [],
            "misleading_anti_corr_buckets": cal.get("misleading_anti_corr_buckets") or [],
            "high_residual_events": (cal.get("high_residual_events") or [])[-5:],
            "coverage_grid": cal.get("coverage_grid"),
        })
    return diag_items[0] if (prob_q and diag_items) else {"workspace": str(workspace), "problem_count": len(diag_items), "problems": diag_items}


def tool_parhelion_pmu_card(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    problem = str(args.get("problem") or "").strip()
    if not problem:
        raise ValueError("'problem' is required for parhelion_pmu_card")
    sha, top_n = args.get("sha"), int(args.get("top_n") or 5)

    try:
        ff_mod = importlib.import_module("opsb_core.services.fast_feedback_service")
        fn = getattr(ff_mod, "build_pmu_torch_card", None)
        if callable(fn) and isinstance(res := fn(workspace, problem, sha=sha, top_n=top_n), dict):
            return {"available": True, **res}
    except ImportError:
        pass

    r_dir = _latest_result_dir(workspace, problem, sha=sha)
    return {
        "available": False, "title": problem, "sha": sha or (r_dir.name if r_dir else None),
        "dominant_bottleneck": "unknown", "reason": "fast_feedback_service unavailable",
        "local_result": _cfg.read_json(r_dir / "result.json", {}) if r_dir else {},
    }


def tool_parhelion_elf_cold3_check(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    problem = str(args.get("problem") or "").strip()
    if not problem:
        raise ValueError("'problem' is required for parhelion_elf_cold3_check")
    sha = args.get("sha")

    try:
        ff_mod = importlib.import_module("opsb_core.services.fast_feedback_service")
        elf_fn = getattr(ff_mod, "evaluate_elf_guard", None)
        cold_fn = getattr(ff_mod, "evaluate_cold3_probe", None)
        if callable(elf_fn) and callable(cold_fn):
            elf = elf_fn(workspace, problem, sha=sha)
            cold = cold_fn(workspace, problem, sha=sha)
            rank = {"ok": 0, "warn": 1, "redline": 2}
            s_elf, s_cold = str((elf or {}).get("status") or "ok"), str((cold or {}).get("status") or "ok")
            worst = s_elf if rank.get(s_elf, 0) >= rank.get(s_cold, 0) else s_cold
            return {
                "available": True, "title": problem,
                "sha": sha or (elf or {}).get("sha") or (cold or {}).get("sha"),
                "status": worst, "elf_guard": elf, "cold3_probe": cold,
            }
    except ImportError:
        pass

    r_dir = _latest_result_dir(workspace, problem, sha=sha)
    return {
        "available": False, "title": problem, "sha": sha or (r_dir.name if r_dir else None),
        "status": "unknown", "reason": "fast_feedback_service unavailable",
        "local_result": _cfg.read_json(r_dir / "result.json", {}) if r_dir else {},
    }


def tool_parhelion_test_plan(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    problem = str(args.get("problem") or "").strip()
    if not problem:
        raise ValueError("'problem' is required for parhelion_test_plan")
    mode = str(args.get("mode") or "full").strip().lower()
    if mode not in ("build-only", "fast", "full"):
        raise ValueError("Invalid 'mode': must be 'build-only', 'fast', or 'full'.")
    execute = bool(args.get("execute", False))

    title, sha = _problem_commit(workspace, problem, str(args.get("sha") or "HEAD"))

    lane = {
        "build-only": {"phase": "Phase A CPU Compile", "queue": "CPU", "estimated_die_lock_sec": 0, "flag": "--build-only"},
        "fast": {"phase": "Fast validation", "queue": "Q0", "estimated_die_lock_sec": None, "flag": "--fast"},
        "full": {"phase": "Full validation", "queue": "Q1", "estimated_die_lock_sec": None, "flag": None},
    }[mode]
    opsb_cmd = [_cfg.resolve_python_exe(workspace), str(_cfg.CLI_DIR / "opsb"), "test", title, "--workspace", str(workspace)]
    if sha and sha != "HEAD":
        opsb_cmd.extend(["--sha", sha])
    if lane["flag"]:
        opsb_cmd.append(str(lane["flag"]))
    case_ids = args.get("cases", [])
    if not isinstance(case_ids, list) or not all(isinstance(k, str) and k for k in case_ids):
        raise ValueError("cases must contain nonempty case IDs")
    for case_id in case_ids:
        opsb_cmd.extend(["--case", case_id])
    for name in ("bucket", "experiment", "timing", "repeats"):
        if args.get(name) is not None:
            opsb_cmd.extend(["--" + name, str(args[name])])
    if args.get("profile"):
        profile = (workspace / str(args["profile"])).resolve()
        bench_specs = (_cfg.workspace_bench_specs_dir(workspace) / title).resolve()
        if not profile.is_file() or not (profile.is_relative_to(workspace.resolve()) or profile.is_relative_to(bench_specs)):
            raise ValueError("Choose a profile in this contest workspace or this problem's bench specs")
        opsb_cmd.extend(["--profile", str(profile)])

    plan: dict[str, Any] = {"ok": True, "title": title, "sha": sha, "mode": mode, "lane": lane, "executed": execute, "command": opsb_cmd}
    if not execute:
        return plan

    proc = subprocess.run(opsb_cmd, cwd=str(workspace), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300.0)
    plan.update({"exit_code": proc.returncode, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-2000:], "ok": proc.returncode == 0})
    return plan


def tool_parhelion_submit_gate(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    problem = str(args.get("problem") or "").strip()
    if not problem:
        raise ValueError("'problem' is required for parhelion_submit_gate")
    note, enqueue, force = str(args.get("note") or "").strip(), bool(args.get("enqueue", False)), bool(args.get("force", False))

    title, sha = _problem_commit(workspace, problem, str(args.get("sha") or "HEAD"))
    options = ["--title", title, "--sha", sha, "--note", note]
    if force:
        options.append("--force")
    if enqueue and not force:
        rows = _cfg.read_jsonl(workspace / "results/submit-queue-outcome.jsonl")
        if has_pending_sync_error_for_commit(rows, title, sha):
            return {"title": title, "sha": sha, "enqueued": False, "duplicate_sync_error": True,
                    "reason": "Submission already accepted and awaiting reconciliation"}
    if enqueue and args.get("_caller_agent_id"):
        options.extend(["--agent-id", args["_caller_agent_id"]])
    reply = invoke_bridge(workspace, "submit" if enqueue else "evaluate", options) or {}
    verdict = reply.get("verdict", {}) if enqueue else reply
    if not verdict.get("stages"):
        return {"title": title, "sha": sha, "enqueued": False, "gates": {"all_pass": False},
                "reason": "Submission gate unavailable", "estimated_score": None}
    estimate = verdict["estimate"]
    anchored = estimate.get("estimation_method") == "anchor_fallback" or "no perf telemetry" in str(estimate.get("detail", "")).lower()
    if anchored:
        estimate = {**estimate, "estimated_score": None, "delta_expected": None}
    stages = {s["stage"]: s["passed"] for s in verdict["stages"]}
    gates = {key: stages.get(stage, False) for key, stage in (
        ("safety_ok", "SAFETY"), ("noise_ok", "NOISE_FLOOR"),
        ("circuit_ok", "CIRCUIT_BREAKER"), ("quota_ok", "QUOTA_RESERVE"))}
    return {**verdict, "estimate": estimate, "gates": {**gates, "all_pass": verdict["approved"]},
            "estimated_score": estimate.get("estimated_score"), "current_score": estimate.get("current_score"),
            "delta_expected": estimate.get("delta_expected"), "wspt_priority": verdict["priority_score"],
            "reasons": [s["reason"] for s in verdict["stages"] if not s["passed"]],
            "enqueued": bool(reply.get("enqueued")), "dry_run": not enqueue}
