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
from ..services.git_fast import resolve_commit_sha
from ..services.queue_service import enqueue_once, check_commit_safety_gate, compute_workspace_snapshot, estimate_score_from_note
from ..services.sync_reconciler import has_pending_sync_error_for_commit


def _find_problem(problems: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    q = query.strip().lower()
    return next((p for p in problems if str(p.get("title", "")).lower() == q), None)


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
    for p in targets:
        cal = dict(p.get("calibration") or {})
        strata = sorted(cal.get("strata") or [], key=lambda s: float(s.get("headroom_pts") or 0.0), reverse=True)
        diag_items.append({
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

    prob = _find_problem(resolve_workspace_meta(workspace).get("problems") or [], problem)
    title = str(prob["title"]) if prob else problem
    repo_dir = (workspace / prob["path"]) if prob else (workspace / title)
    raw_sha = str(args.get("sha") or "HEAD")
    sha = resolve_commit_sha(repo_dir, raw_sha) or raw_sha

    lane = {
        "build-only": {"phase": "Phase A CPU Compile", "queue": "CPU", "estimated_die_lock_sec": 0, "flag": "--build-only"},
        "fast": {"phase": "Phase A+B+C Fast Lane", "queue": "Q0", "estimated_die_lock_sec": 10, "flag": "--fast"},
        "full": {"phase": "Phase A+B+C Full Adversarial + profiler", "queue": "Q1", "estimated_die_lock_sec": 18, "flag": None},
    }[mode]
    opsb_cmd = [_cfg.resolve_python_exe(workspace), str(_cfg.CLI_DIR / "opsb"), "test", title, "--workspace", str(workspace)]
    if sha and sha != "HEAD":
        opsb_cmd.extend(["--sha", sha])
    if lane["flag"]:
        opsb_cmd.append(str(lane["flag"]))

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

    snap = compute_workspace_snapshot(workspace, load_parhelion_config())
    prob = _find_problem(snap.get("problems") or [], problem)
    if prob is None:
        raise ValueError(f"Unknown problem '{problem}' in workspace {workspace}")

    title, repo_dir = str(prob["title"]), workspace / prob["path"]
    raw_sha = str(args.get("sha") or prob.get("head_sha") or "HEAD")
    sha = resolve_commit_sha(repo_dir, raw_sha) or raw_sha
    if not sha or sha == "HEAD":
        raise ValueError(f"Unable to resolve commit SHA '{raw_sha}' for {title}")

    cal = prob.get("calibration") or {}
    disk_cal = _cfg.read_json(workspace / "results" / title / "calibration.json", {})
    escape_locked = bool(cal.get("escape_lock") or (isinstance(disk_cal, dict) and disk_cal.get("escape_lock")))
    safety_passed = check_commit_safety_gate(workspace, title, sha) or (prob.get("best_sha") == sha and float(prob.get("best_score") or 0) > 0)
    safety_ok = safety_passed and not escape_locked

    cur_sc, best_sc = float(prob.get("current_score") or 0.0), float(prob.get("best_score") or 0.0)
    best_sha = str(prob.get("best_sha") or "")
    est = estimate_score_from_note(note, cur_sc, best_sc)
    if est is None:
        est = best_sc if (best_sha and sha.lower().startswith(best_sha.lower()[:7]) and best_sc > 0) else round(cur_sc + 3.0, 2)

    noise_floor = float(snap["scheduler"]["noise_floor_delta"])
    delta_exp = round(est - cur_sc, 2)
    is_restore_zero = cur_sc == 0.0 and est > 0.0
    is_restore_regime = (not is_restore_zero) and cur_sc > 0.0 and bool(best_sha) and ((best_sc - cur_sc) >= 1.5)
    is_restoring_best = bool(best_sha and sha.lower().startswith(best_sha.lower()[:7]))
    if is_restore_regime and not is_restoring_best:
        noise_ok = (est - best_sc) >= noise_floor
    else:
        noise_ok = is_restore_zero or cur_sc == 0.0 or delta_exp >= noise_floor
    circuit_ok = is_restore_zero or not bool(prob.get("circuit_open"))
    quota_ok = is_restore_zero or int(snap["scheduler"]["available_normal_shots"]) > 0
    all_pass = safety_ok and noise_ok and circuit_ok and quota_ok

    reasons: list[str] = []
    if escape_locked:
        reasons.append("ESCAPE_LOCK active: verify escape reproducer before submitting")
    elif not safety_passed:
        reasons.append("G1 Safety failed: commit lacks passing NPU extended validation")
    if not noise_ok:
        if is_restore_regime and not is_restoring_best:
            reasons.append(f"G2 Noise Floor ([RESTORE]): est={est:.2f} vs best={best_sc:.2f} (Δvs_best={est - best_sc:+.2f} < {noise_floor})")
        else:
            reasons.append(f"G2 Noise Floor: Δexpected={delta_exp:+.2f} < {noise_floor}")
    if not circuit_ok:
        reasons.append(f"G3 Circuit Open: 12h pressure={prob.get('pressure_12h')}")
    if not quota_ok:
        reasons.append("G4 Quota Reserve: daily normal shots exhausted")

    verdict: dict[str, Any] = {
        "title": title, "sha": sha, "current_score": cur_sc, "estimated_score": est,
        "delta_expected": delta_exp, "wspt_priority": round(int(prob.get("difficulty") or 2) * max(0.0, delta_exp), 2),
        "gates": {"safety_ok": safety_ok, "noise_ok": noise_ok, "circuit_ok": circuit_ok, "quota_ok": quota_ok, "all_pass": all_pass},
        "reasons": reasons, "enqueued": False,
    }
    official = invoke_bridge(workspace, "evaluate", ["--title", title, "--sha", sha, "--note", note])
    if official and official.get("stages"):
        stages = {s["stage"]: bool(s["passed"]) for s in official["stages"]}
        all_pass = bool(official.get("approved"))
        est = (official.get("estimate") or {}).get("estimated_score")
        reasons = [s["reason"] for s in official["stages"] if not s["passed"]]
        verdict.update(gates={"safety_ok": stages["SAFETY"], "noise_ok": stages["NOISE_FLOOR"],
                              "circuit_ok": stages["CIRCUIT_BREAKER"], "quota_ok": stages["QUOTA_RESERVE"],
                              "all_pass": all_pass}, estimated_score=est, reasons=reasons,
                       status_tag=official["status_tag"], wspt_priority=official["priority_score"])
    if not enqueue:
        verdict["dry_run"] = True
        return verdict
    if not all_pass and not force:
        verdict["reason"] = "; ".join(reasons)
        return verdict

    q_path, o_path = workspace / "results" / "submit-queue.jsonl", workspace / "results" / "submit-queue-outcome.jsonl"
    o_rows = _cfg.read_jsonl(o_path)
    done_keys = {(o.get("title"), str(o.get("sha") or "")[:7], o.get("queued_at")) for o in o_rows}
    pending = [
        q for q in _cfg.read_jsonl(q_path)
        if q.get("title") == title and str(q.get("sha") or "").startswith(sha[:7])
        and (q.get("title"), str(q.get("sha") or "")[:7], q.get("queued_at")) not in done_keys
    ]
    if pending and not force:
        verdict["reason"] = f"Duplicate in flight: {title} {sha[:7]} already queued"
        return verdict
    if not force:
        sync_hit = has_pending_sync_error_for_commit(o_rows, title, sha)
        if sync_hit is not None:
            sid = str(sync_hit.get("submission_id") or "")
            verdict["duplicate_sync_error"] = True
            verdict["reason"] = f"Duplicate submitted (sync_error): {title} {sha[:7]} already submitted (sid={sid[:8]})"
            return verdict

    record: dict[str, Any] = {
        "title": title, "sha": sha, "note": note or "queued via parhelion-mcp",
        "queued_at": _cfg.utc_now().isoformat(), "status": "queued", "estimated_score": est,
    }
    if force:
        record["force"] = True
    if not enqueue_once(workspace, record):
        verdict["reason"] = "Already queued or previously submitted"
        return verdict
    _cfg.invalidate_ttl_cache("scheduler_bridge")
    verdict.update({"enqueued": True, "queued_record": record})
    return verdict
