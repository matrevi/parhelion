"""CLI commands: `opsb submit`, `opsb queue ...`, and `opsb test`."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from ..config import CLI_DIR, ConfigRepository, append_jsonl_locked, invalidate_ttl_cache, resolve_python_exe, utc_now
from ..ipc_bridge import invoke_bridge
from ..services.contest_service import resolve_workspace_meta
from ..services.git_fast import resolve_commit_sha
from ..services.npu_service import SSH_CONTROL_OPTS
from ..services.queue_service import check_commit_safety_gate, compute_workspace_snapshot, estimate_score_from_note
from ..services.submit_runner import run_worker_submit_one, spawn_jump_submit


def run_submit(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    ws = ConfigRepository.resolve_workspace(args.workspace, cfg)
    snap = compute_workspace_snapshot(ws, cfg)
    prob = next((p for p in snap["problems"] if p["title"].lower() == args.problem.lower()), None)
    if prob is None:
        raise SystemExit(f"Unknown problem '{args.problem}' in workspace {ws}")

    title = prob["title"]
    repo_dir = ws / prob["path"]
    raw_sha = args.sha or prob.get("head_sha") or ""
    sha = resolve_commit_sha(repo_dir, raw_sha)
    if not sha:
        raise SystemExit(f"Unknown commit '{raw_sha}' in {title}")

    safety_ok = check_commit_safety_gate(ws, title, sha) or (prob["best_sha"] == sha and prob["best_score"] > 0)
    est = estimate_score_from_note(args.note or "", prob["current_score"], prob["best_score"])
    if est is None:
        est = prob["best_score"] if (prob["best_sha"] == sha and prob["best_score"] > 0) else round(prob["current_score"] + 3.0, 2)

    noise_floor = float(snap["scheduler"]["noise_floor_delta"])
    delta_exp = round(est - prob["current_score"], 2)
    is_restore_zero = prob["current_score"] == 0.0 and est > 0
    noise_ok = is_restore_zero or prob["current_score"] == 0.0 or delta_exp >= noise_floor
    circuit_ok = is_restore_zero or not prob["circuit_open"]
    quota_ok = is_restore_zero or snap["scheduler"]["available_normal_shots"] > 0

    verdict = {
        "title": title,
        "sha": sha,
        "current_score": prob["current_score"],
        "estimated_score": est,
        "delta_expected": delta_exp,
        "wspt_priority": round(prob["difficulty"] * max(0.0, delta_exp), 2),
        "gates": {"safety_ok": safety_ok, "noise_ok": noise_ok, "circuit_ok": circuit_ok, "quota_ok": quota_ok},
    }

    if not safety_ok and not args.force:
        verdict.update(enqueued=False, reason="G1 Safety failed: commit lacks passing NPU extended validation (`opsb test`).")
        print(json.dumps(verdict, ensure_ascii=False) if args.json else f"BLOCKED [G1 Safety]: {verdict['reason']}")
        return 2

    if not (noise_ok and circuit_ok and quota_ok) and not args.force:
        reasons = []
        if not noise_ok:
            reasons.append(f"G2 Noise Floor (Δexpected={delta_exp:+.2f} < {noise_floor})")
        if not circuit_ok:
            reasons.append(f"G3 Circuit Open (pressure={prob['pressure_12h']})")
        if not quota_ok:
            reasons.append("G4 Daily Normal Quota Exhausted (closing reserve protected)")
        verdict.update(enqueued=False, reason="; ".join(reasons))
        print(json.dumps(verdict, ensure_ascii=False) if args.json else f"HELD BY GATE: {verdict['reason']} (pass --force to override)")
        return 3

    record = {"title": title, "sha": sha, "note": args.note or "queued via opsb", "queued_at": utc_now().isoformat(), "status": "queued"}
    if args.force:
        record["force"] = True
    append_jsonl_locked(ws / "results" / ".submit-queue.lock", ws / "results" / "submit-queue.jsonl", record)
    invalidate_ttl_cache("scheduler_bridge")
    verdict.update(enqueued=True, queued_record=record)
    print(json.dumps(verdict, ensure_ascii=False) if args.json else f"Enqueued {title} {sha[:7]} (est={est:.2f}, WSPT={verdict['wspt_priority']:.1f})")
    return 0


def run_queue(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    ws = ConfigRepository.resolve_workspace(getattr(args, "workspace", None), cfg)

    if args.q_cmd == "config":
        sched = cfg.setdefault("scheduler", {})
        bridge_extra: list[str] = []
        if args.auto_consumer is not None:
            sched["auto_consumer_enabled"] = args.auto_consumer.lower() in ("1", "true", "on", "yes")
        if args.closing_reserve is not None:
            sched["closing_reserve"] = max(0, min(40, int(args.closing_reserve)))
            bridge_extra.extend(["--reserve", str(sched["closing_reserve"])])
        if args.noise_floor is not None:
            sched["noise_floor_delta"] = round(max(0.0, min(15.0, float(args.noise_floor))), 2)
            bridge_extra.extend(["--noise-floor", str(sched["noise_floor_delta"])])
        if getattr(args, "breakthrough_threshold", None) is not None:
            sched["breakthrough_threshold"] = round(max(0.5, min(50.0, float(args.breakthrough_threshold))), 2)
        ConfigRepository.save(cfg)
        if bridge_extra:
            invoke_bridge(ws, "config", bridge_extra)
        invalidate_ttl_cache("scheduler_bridge")
        print(json.dumps({"ok": True, "scheduler": sched}, ensure_ascii=False))
        return 0

    if args.q_cmd == "skip":
        invoke_bridge(ws, "override", ["--title", args.problem, "--sha", args.sha, "--action", "skip", "--reason", args.reason or "skipped via Supervisor"])
        rec = {
            "title": args.problem,
            "sha": args.sha,
            "note": args.reason or "skipped via Supervisor",
            "queued_at": utc_now().isoformat(),
            "finished_at": utc_now().isoformat(),
            "status": "skipped",
        }
        append_jsonl_locked(ws / "results" / ".submit-queue.lock", ws / "results" / "submit-queue-outcome.jsonl", rec)
        invalidate_ttl_cache("scheduler_bridge")
        print(json.dumps({"ok": True, "skipped": rec}, ensure_ascii=False))
        return 0

    if args.q_cmd == "approve":
        invoke_bridge(ws, "override", ["--title", args.problem, "--sha", args.sha, "--action", "approve", "--reason", args.note or "approved"])
        return run_submit(argparse.Namespace(problem=args.problem, sha=args.sha, note=args.note or "approved via Supervisor", force=True, workspace=str(ws), json=True))

    if args.q_cmd == "restore":
        snap = compute_workspace_snapshot(ws, cfg)
        prob = next((p for p in snap["problems"] if p["title"].lower() == args.problem.lower()), None)
        if not prob or not prob.get("best_sha"):
            raise SystemExit(f"No historical passing SHA found to restore for {args.problem}")
        return run_submit(argparse.Namespace(problem=prob["title"], sha=prob["best_sha"], note=f"restore best validated SHA ({prob['best_score']:.2f})", force=True, workspace=str(ws), json=True))

    if args.q_cmd == "submit-now":
        snap = compute_workspace_snapshot(ws, cfg)
        prob = next((p for p in snap["problems"] if p["title"].lower() == args.problem.lower()), None)
        if not prob:
            raise SystemExit(f"Unknown problem: {args.problem}")
        sha = resolve_commit_sha(ws / prob["path"], args.sha) or args.sha
        res = spawn_jump_submit(CLI_DIR / "opsb", ws, prob["title"], sha, args.note or "jump-queue submit via Parhelion")
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 2

    if args.q_cmd == "_worker-submit-one":
        return run_worker_submit_one(ws, args.problem, args.sha, args.note or "")

    if args.q_cmd == "drain":
        bridge_res = invoke_bridge(ws, "drain")
        invalidate_ttl_cache("scheduler_bridge")
        if bridge_res is not None:
            print(json.dumps(bridge_res, ensure_ascii=False))
            return 0
        legacy_q = ws / "tools" / "submit-queue.py"
        if legacy_q.is_file():
            py_exe = resolve_python_exe(ws)
            proc = subprocess.run([py_exe, str(legacy_q), "run"], cwd=str(ws), capture_output=True, text=True)
            out = {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr).strip()}
            print(json.dumps(out, ensure_ascii=False) if getattr(args, "json", False) else out["output"])
            return proc.returncode
        print(json.dumps({"ok": True, "output": "Queue consumer idle."}))
        return 0
    return 1


def run_test(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    ws = ConfigRepository.resolve_workspace(args.workspace, cfg)
    legacy_npu_test = ws / "tools" / "npu-test.py"
    if legacy_npu_test.is_file():
        venv_py = ws / ".venv" / "bin" / "python"
        py_exe = str(venv_py) if venv_py.is_file() else sys.executable
        cmd = [py_exe, str(legacy_npu_test), args.problem] + (["--sync-only"] if args.sync_only else [])
        env = {**os.environ, **({"OPSB_FAST_LANE": "1"} if args.fast else {})}
        return subprocess.call(cmd, cwd=str(ws), env=env)

    meta = resolve_workspace_meta(ws)
    prob = next((p for p in meta["problems"] if p["title"].lower() == args.problem.lower()), None)
    if not prob:
        raise SystemExit(f"Unknown problem: {args.problem}")
    host = cfg.get("npu_ssh_host") or meta.get("npu_ssh_host") or "npu"
    remote = "flock -w 3600 /srv/work-device0.lock bash -c 'source /opt/toolchain/latest/set_env.sh && echo NPU_DIE_READY'"
    return subprocess.call(["ssh", *SSH_CONTROL_OPTS, host, remote])
