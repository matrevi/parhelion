"""MCP tool handlers for status, benchmark diagnosis, fast-feedback cards, test planning & submit gate."""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from typing import Any

from .. import config as _cfg
from ..drivers.base import load_parhelion_config
from ..ipc_bridge import invoke_bridge, merge_bridge_enrichment
from ..services import test_runs
from ..services.queue_service import compute_workspace_snapshot
from ..services.sync_reconciler import has_pending_sync_error_for_commit
from ..services.test_runs import _find_problem, _problem_commit

STATUS_PROBLEM_COLUMNS = ("title", "status", "current_score", "best_score", "leader_score", "rank", "head_sha", "best_sha")
STATUS_QUEUE_KEYS = ("title", "sha7", "status", "estimated_score", "delta_expected")
WORKER_LEVEL_ONLY = "Workers select a level (smoke|standard|final); the platform owns the suite"
EXPERIMENT_ARGS = ("profile", "experiment", "timing", "repeats", "bucket")


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


def _round_score(value: Any) -> Any:
    return round(value, 2) if isinstance(value, float) else value


def _compact_queue_item(item: dict[str, Any]) -> dict[str, Any]:
    est = item.get("estimate") if isinstance(item.get("estimate"), dict) else {}
    out = {
        "title": item.get("title"), "sha7": str(item.get("sha") or "")[:7], "status": item.get("status") or item.get("kind"),
        "estimated_score": item.get("estimated_score", est.get("estimated_score")),
        "delta_expected": item.get("delta_expected", est.get("delta_expected")),
    }
    return {k: _round_score(out[k]) for k in STATUS_QUEUE_KEYS}


def status_rev(out: dict[str, Any], args: dict[str, Any]) -> str:
    """Content revision of a status reply: same arguments and same data give the same rev.

    The sample time and the ticking cooldown counter are left out (``rate_ready`` still flips),
    so an unchanged contest answers ``if_rev`` with ``not_modified``.
    """
    body = {k: v for k, v in out.items() if k != "sampled_at"}
    if isinstance(body.get("scheduler"), dict):
        body["scheduler"] = {k: v for k, v in body["scheduler"].items() if k != "cooldown_remaining_sec"}
    scope = {k: args.get(k) for k in ("problem", "include_npu", "include_queue", "verbose", "detail")}
    raw = json.dumps([scope, body], ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "s1:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def tool_parhelion_status(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    out = _status_payload(args, workspace)
    rev = status_rev(out, args)
    if str(args.get("if_rev") or "") == rev:
        return {"not_modified": True, "rev": rev}
    out["rev"] = rev
    return out


def _case_detail(workspace: Path, problems: list[dict[str, Any]], agent_id: str | None = None,
                 limit: int = 3) -> list[dict[str, Any]]:
    """The extended state card of up to ``limit`` problems, as text plus a compact per-case table."""
    from ..services import state_card_ext
    out = []
    for problem in problems[:limit]:
        title = str(problem.get("title") or "")
        if not title:
            continue
        doc = state_card_ext.data(workspace, title, agent_id)
        out.append({
            "title": title, "card": state_card_ext.render(doc),
            "columns": ["case", "speedup", "confidence", "ours_us", "tbest_us", "gap_pct", "points"],
            "rows": [[c.get("case"), c.get("speedup"), c.get("confidence"), c.get("ours_us"), c.get("tbest_us"),
                      c.get("gap_pct"), c.get("points")] for c in doc.get("cases") or []],
            "targets": doc.get("targets") or [], "receipts": doc.get("receipts") or [],
            "worktree": doc.get("worktree"),
        })
    return out


def _status_payload(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
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
    verbose = bool(args.get("verbose", False))
    detail = bool(args.get("detail", False))
    include_queue = bool(args.get("include_queue", verbose))

    problems = snap.get("problems") or []
    if prob_filter:
        problems = [p for p in problems if str(p.get("title", "")).lower() == prob_filter]

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
    }
    q_items = snap.get("submission_queue") or []
    if prob_filter:
        q_items = [q for q in q_items if str(q.get("title", "")).lower() == prob_filter]

    if verbose:
        out["problems"] = [
            {
                "title": p.get("title"), "difficulty": p.get("difficulty"), "status": p.get("status"),
                "current_score": p.get("current_score"), "best_score": p.get("best_score"),
                "leader_score": p.get("leader_score"), "rank": p.get("rank"), "wspt_tag": p.get("wspt_tag"),
                "head_sha": str(p.get("head_sha") or "")[:12], "best_sha": str(p.get("best_sha") or "")[:12],
                "escape_lock": bool((p.get("calibration") or {}).get("escape_lock")),
            }
            for p in problems
        ]
        if include_queue:
            out["queue_preview"] = q_items[:12]
        if detail:
            out["case_detail"] = _case_detail(workspace, problems, args.get("_caller_agent_id"))
        return out

    # Compact default: one row per problem under shared column names keeps 14+ problems under 2KB.
    rows = []
    for p in problems:
        row = {**p, "head_sha": str(p.get("head_sha") or "")[:7] or None, "best_sha": str(p.get("best_sha") or "")[:7] or None}
        rows.append([_round_score(row.get(col)) for col in STATUS_PROBLEM_COLUMNS])
    out["problems"] = {"columns": list(STATUS_PROBLEM_COLUMNS), "rows": rows}
    if include_queue:
        out["queue_preview"] = [_compact_queue_item(q) for q in q_items[:5] if isinstance(q, dict)]
    if detail:  # 4.1 #14: the orchestrator gets the per-case table instead of computing the ratios itself
        out["case_detail"] = _case_detail(workspace, problems, args.get("_caller_agent_id"))
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

    verbose = bool(args.get("verbose", False))
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
            "high_residual_events": (cal.get("high_residual_events") or [])[-3:],
            **({"coverage_grid": cal.get("coverage_grid")} if verbose else {}),
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


def _caller(args: dict[str, Any]) -> tuple[str, str | None]:
    """(role, agent_id) for the calling session; orchestrator calls carry no agent_id."""
    if args.get("_worker_request"):
        return "worker", (str(args.get("_caller_agent_id") or "").strip() or None)
    return "orchestrator", None


def _plan_level(args: dict[str, Any], mode: str) -> str:
    if args.get("level") is not None:
        level = str(args["level"]).strip().lower()
        if level not in test_runs.LEVELS:
            raise ValueError("level must be smoke, standard or final")
        return level
    if args.get("suite") is not None:
        suite = str(args["suite"]).strip().lower()
        if suite not in ("adaptive", "all"):
            raise ValueError("suite must be adaptive or all")
        return {"adaptive": "smoke", "all": "standard"}[suite]
    return {"build-only": "standard", "fast": "smoke", "full": "standard"}[mode]


def _ledger_warnings(workspace: Path, title: str, agent_id: str | None) -> list[dict[str, Any]]:
    """Research R5/R8 notes for a tool reply: repeated dead ends and a plateau (warns, never blocks)."""
    from ..ledger import auto, budget
    try:
        rows = auto._ledger(workspace, title, agent_id).rows()
        return budget.warnings(rows)
    except Exception:
        return []


def _retest_reply(workspace: Path, title: str, sha: str, level: str, args: dict[str, Any],
                  agent_id: str | None) -> dict[str, Any] | None:
    """The reply of a retest or a near duplicate, or None to spend the device time (4.2 #7, R7)."""
    from ..ledger import auto, retest
    if args.get("build_only"):
        return None
    reason = str(args.get("reason") or "").strip()
    force = bool(args.get("force_rerun") or args.get("remeasure"))
    hit = retest.intercept(workspace, title, sha, level, cases=args.get("cases") or [],
                           force_rerun=force, reason=reason, agent_id=agent_id or "")
    if hit is not None:
        return {**hit, "warnings": _ledger_warnings(workspace, title, agent_id)}
    if force and reason:
        return None
    try:
        rows = auto._ledger(workspace, title, agent_id).rows()
        found = retest.duplicate_note(retest.near_duplicate(workspace, title, sha, rows, agent_id or ""))
    except Exception:
        found = None
    if found and not reason:
        return {"ok": True, "sha7": sha[:7], "problem": title, "queued": False, "intercepted": True,
                **found, "warnings": _ledger_warnings(workspace, title, agent_id)}
    return None


def tool_parhelion_test(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    problem = str(args.get("problem") or "").strip()
    if not problem:
        raise ValueError("'problem' is required for parhelion_test")
    role, agent_id = _caller(args)
    level = str(args.get("level") or "standard")
    title, sha = _problem_commit(workspace, problem, str(args.get("sha") or "HEAD"))
    reused = _retest_reply(workspace, title, sha, level, args, agent_id)
    if reused is not None:
        return reused
    view = test_runs.submit(
        workspace, problem=problem, sha=str(args.get("sha") or "HEAD"), level=level,
        cases=args.get("cases") or [], remeasure=bool(args.get("remeasure", False)),
        build_only=bool(args.get("build_only", False)), agent_id=agent_id, role=role,
    )
    warnings = _ledger_warnings(workspace, title, agent_id)
    return {**view, "warnings": warnings} if warnings else view


def tool_parhelion_test_wait(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    request_id = str(args.get("request_id") or "").strip()
    if not request_id:
        raise ValueError("'request_id' is required for parhelion_test_wait")
    wait_sec = 30 if args.get("wait_sec") is None else args.get("wait_sec")
    from .handlers_agents import _proxy_daemon, daemon_hub
    hub = daemon_hub(workspace)
    if hub is not None:  # inside the daemon: sleep on the run's state changes
        return test_runs.wait(workspace, request_id, wait_sec, block=hub.test_blocker(request_id))
    try:
        budget = min(float(test_runs.WAIT_MAX_SEC), max(1.0, float(wait_sec)))
    except (TypeError, ValueError):
        budget = 30.0
    proxied = _proxy_daemon("POST", "/v1/events/test_wait", workspace, {"request_id": request_id, "wait_sec": budget},
                            timeout=budget + 6.0, missing_ok=True)
    return proxied if proxied is not None else test_runs.wait(workspace, request_id, wait_sec)


def tool_parhelion_test_cancel(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    request_id = str(args.get("request_id") or "").strip()
    if not request_id:
        raise ValueError("'request_id' is required for parhelion_test_cancel")
    role, agent_id = _caller(args)
    if role == "worker":
        owner = test_runs.refresh(workspace, request_id).get("agent_id")
        if not agent_id or owner != agent_id:
            raise PermissionError("Workers can cancel only their own test requests")
    return test_runs.cancel(workspace, request_id)


def tool_parhelion_test_plan(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    problem = str(args.get("problem") or "").strip()
    if not problem:
        raise ValueError("'problem' is required for parhelion_test_plan")
    mode = str(args.get("mode") or "full").strip().lower()
    if mode not in ("build-only", "fast", "full"):
        raise ValueError("Invalid 'mode': must be 'build-only', 'fast', or 'full'.")
    execute = bool(args.get("execute", False))
    role, agent_id = _caller(args)
    experiment_args = [name for name in EXPERIMENT_ARGS if args.get(name) not in (None, "")]
    if role == "worker" and experiment_args:
        raise ValueError(WORKER_LEVEL_ONLY)
    if execute and experiment_args:
        raise ValueError("Experiment parameters are preview-only through MCP; execute=true selects a level only")

    title, sha = _problem_commit(workspace, problem, str(args.get("sha") or "HEAD"))
    level = _plan_level(args, mode)
    build_only = mode == "build-only"
    case_ids = args.get("cases") or []
    if not isinstance(case_ids, list) or not all(isinstance(k, str) and k for k in case_ids):
        raise ValueError("cases must contain nonempty case IDs")

    if execute:
        view = test_runs.submit(workspace, problem=title, sha=sha, level=level, cases=case_ids,
                                remeasure=bool(args.get("remeasure")), build_only=build_only,
                                agent_id=agent_id, role=role)
        flags = {k: True for k in ("joined", "reused") if view.get(k)}
        if view.get("status") not in test_runs.TERMINAL:
            view = test_runs.wait(workspace, view["request_id"], 40)
        return {**view, **flags, "deprecated": "use parhelion_test + parhelion_test_wait"}

    lane = {
        "build-only": {"phase": "Phase A CPU Compile", "queue": "CPU", "estimated_die_lock_sec": 0, "flag": "--build-only"},
        "fast": {"phase": "Fast validation", "queue": "Q0", "estimated_die_lock_sec": None, "flag": None},
        "full": {"phase": "Full validation", "queue": "Q1", "estimated_die_lock_sec": None, "flag": None},
    }[mode]
    opsb_cmd = [_cfg.resolve_python_exe(workspace), str(_cfg.CLI_DIR / "opsb"), "test", title, "--workspace", str(workspace)]
    if sha and sha != "HEAD":
        opsb_cmd.extend(["--sha", sha])
    opsb_cmd.extend(["--level", level])
    if lane["flag"]:
        opsb_cmd.append(str(lane["flag"]))
    if args.get("remeasure"):
        opsb_cmd.append("--remeasure")
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

    return {"ok": True, "title": title, "sha": sha, "mode": mode, "level": level, "lane": lane,
            "executed": False, "command": opsb_cmd}


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
    result = {**verdict, "estimate": estimate, "gates": {**gates, "all_pass": verdict["approved"]},
              "estimated_score": estimate.get("estimated_score"), "current_score": estimate.get("current_score"),
              "delta_expected": estimate.get("delta_expected"), "wspt_priority": verdict["priority_score"],
              "reasons": [s["reason"] for s in verdict["stages"] if not s["passed"]],
              "enqueued": bool(reply.get("enqueued")), "dry_run": not enqueue}
    # A request, not a preview: it names its hypothesis, or the platform generates the one P05 wrote from
    # the submission note and says which id it created (4.2 C #3). A ledger fault never fails a submission.
    from ..ledger import auto, budget
    link = auto.guard(auto.on_gate_link, workspace, title, sha, args.get("_caller_agent_id"), note=note,
                      estimate=estimate, enqueued=result["enqueued"], write=enqueue,
                      hypothesis_id=str(args.get("hypothesis_id") or "").strip(),
                      predicted_effect=args.get("predicted_effect"))
    if isinstance(link, dict):
        result["ledger"] = link
    result["warnings"] = _ledger_warnings(workspace, title, args.get("_caller_agent_id"))
    return result
