"""CLI commands: `opsb submit`, `opsb queue ...`, and `opsb test`."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from ..config import CLI_DIR, ConfigRepository, append_jsonl_locked, invalidate_ttl_cache, resolve_python_exe, utc_now
from ..device_env import device_text
from ..ipc_bridge import invoke_bridge, merge_bridge_enrichment
from ..services.contest_service import resolve_workspace_meta
from ..services.git_fast import resolve_commit_sha
from ..services.npu_service import SSH_CONTROL_OPTS
from ..services.queue_service import enqueue_once, check_commit_safety_gate, compute_workspace_snapshot, estimate_score_from_note
from ..services.submit_runner import (
    cancel_waiting_cooldown_for_commit,
    find_active_inflight_for_title,
    run_worker_submit_one,
    spawn_jump_submit,
)
from ..services.sync_reconciler import has_pending_sync_error_for_commit


def run_submit(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    ws = ConfigRepository.resolve_workspace(args.workspace, cfg)
    snap = compute_workspace_snapshot(ws, cfg)
    bridge = invoke_bridge(ws, "status", ["--offline"], use_cache=False)
    if isinstance(bridge, dict):
        merge_bridge_enrichment(snap, bridge)

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
    matched_cand = next(
        (it for it in snap["submission_queue"] if it["title"] == title and it["sha"].startswith(sha[:7]) and it["kind"] != "completed"),
        None,
    )
    bridge_eval = invoke_bridge(ws, "evaluate", ["--title", title, "--sha", sha, "--note", args.note or ""], use_cache=False)

    bridge_est = (bridge_eval or {}).get("estimate") or {}
    est = (
        (matched_cand or {}).get("estimated_score")
        or (round(float(bridge_est["estimated_score"]), 2) if bridge_est.get("estimated_score") is not None else None)
        or estimate_score_from_note(args.note or "", prob["current_score"], prob["best_score"])
    )
    if est is None:
        est = prob["best_score"] if (prob["best_sha"] == sha and prob["best_score"] > 0) else round(prob["current_score"] + 3.0, 2)

    noise_floor = float(snap["scheduler"]["noise_floor_delta"])
    delta_exp = round(est - prob["current_score"], 2)
    is_restore_zero = prob["current_score"] == 0.0 and est > 0
    best_sc = float(prob.get("best_score") or 0.0)
    best_sha = str(prob.get("best_sha") or "")
    is_restore_regime = (not is_restore_zero) and prob["current_score"] > 0.0 and bool(best_sha) and ((best_sc - prob["current_score"]) >= 1.5)
    is_restoring_best = bool(best_sha and sha.lower().startswith(best_sha.lower()[:7]))
    explore_ok = bool(
        ((matched_cand or {}).get("gates") or {}).get("explore_eligible")
        or (matched_cand or {}).get("badge") == "EXPLORE"
        or bridge_est.get("explore_eligible")
        or (bridge_eval or {}).get("status_tag") == "EXPLORE"
    )
    bridge_tag = str((bridge_eval or {}).get("status_tag") or "")
    bridge_stages = {
        s.get("stage"): s for s in ((bridge_eval or {}).get("stages") or []) if isinstance(s, dict)
    }
    bridge_s2 = bridge_stages.get("NOISE_FLOOR")
    if bridge_tag == "SKIPPED":
        noise_ok = False
    elif bridge_s2 is not None and not is_restore_zero:
        noise_ok = bool(bridge_s2.get("passed"))
    elif is_restore_regime and not is_restoring_best:
        cand_noise_ok = (matched_cand or {}).get("gates", {}).get("noise_ok")
        if matched_cand is not None and cand_noise_ok is not None:
            noise_ok = bool(cand_noise_ok)
        else:
            noise_ok = ((est - best_sc) >= noise_floor) or (explore_ok and (est - best_sc) > 0.0)
    else:
        noise_ok = is_restore_zero or prob["current_score"] == 0.0 or delta_exp >= noise_floor or explore_ok
    circuit_ok = is_restore_zero or not prob["circuit_open"]
    quota_ok = is_restore_zero or snap["scheduler"]["available_normal_shots"] > 0

    if bridge_stages:
        safety_ok, noise_ok, circuit_ok, quota_ok = (bool(bridge_stages.get(stage, {}).get("passed")) for stage in
            ("SAFETY", "NOISE_FLOOR", "CIRCUIT_BREAKER", "QUOTA_RESERVE"))
    if bridge_tag == "PROBE":
        safety_ok = noise_ok = circuit_ok = True
        quota_ok = snap["scheduler"]["used_today_24h"] < snap["scheduler"]["daily_quota_limit"]

    verdict = {
        "title": title,
        "sha": sha,
        "current_score": prob["current_score"],
        "estimated_score": est,
        "delta_expected": delta_exp,
        "explore_eligible": explore_ok,
        "explore_reason": str(bridge_est.get("explore_reason") or ((matched_cand or {}).get("detail") if explore_ok else "")),
        "wspt_priority": round(prob["difficulty"] * max(0.0, delta_exp), 2),
        "gates": {"safety_ok": safety_ok, "noise_ok": noise_ok, "circuit_ok": circuit_ok, "quota_ok": quota_ok, "explore_ok": explore_ok},
    }

    if not safety_ok and not args.force:
        verdict.update(enqueued=False, reason="G1 Safety failed: commit lacks passing NPU extended validation (`opsb test`).")
        print(json.dumps(verdict, ensure_ascii=False) if args.json else f"BLOCKED [G1 Safety]: {verdict['reason']}")
        return 2

    if not (noise_ok and circuit_ok and quota_ok) and not args.force:
        reasons = []
        if not noise_ok:
            if bridge_tag == "SKIPPED":
                reasons.append(str((bridge_eval or {}).get("gate_reason") or "Skipped by operator override"))
            elif is_restore_regime and not is_restoring_best:
                reasons.append(f"G2 Noise Floor ([RESTORE]: est={est:.2f} vs best={best_sc:.2f}, Δvs_best={est - best_sc:+.2f} < {noise_floor})")
            else:
                reasons.append(f"G2 Noise Floor (Δexpected={delta_exp:+.2f} < {noise_floor})")
        if not circuit_ok:
            reasons.append(f"G3 Circuit Open (pressure={prob['pressure_12h']})")
        if not quota_ok:
            reasons.append("G4 Daily Normal Quota Exhausted (closing reserve protected)")
        verdict.update(enqueued=False, reason="; ".join(reasons))
        print(json.dumps(verdict, ensure_ascii=False) if args.json else f"HELD BY GATE: {verdict['reason']} (pass --force to override)")
        return 3

    # Return a specific diagnostic before the final atomic enqueue check.
    try:
        from ..services.queue_service import read_jsonl as _read_jsonl

        _q = _read_jsonl(ws / "results" / "submit-queue.jsonl")
        _o = _read_jsonl(ws / "results" / "submit-queue-outcome.jsonl")
        _done = {(o.get("title"), (o.get("sha") or "")[:7], o.get("queued_at")) for o in _o}
        _pending = [
            q for q in _q
            if q.get("title") == title
            and (q.get("sha") or "").startswith(sha[:7])
            and (q.get("title"), (q.get("sha") or "")[:7], q.get("queued_at")) not in _done
        ]
        if _pending and not args.force:
            verdict.update(
                enqueued=False,
                reason=f"duplicate in flight: {title} {sha[:7]} already queued at {_pending[-1].get('queued_at')}",
            )
            print(json.dumps(verdict, ensure_ascii=False) if args.json else f"DUPLICATE: {verdict['reason']} (pass --force to override)")
            return 4
        _sync_err = has_pending_sync_error_for_commit(_o, title, sha) if not args.force else None
        if _sync_err is not None:
            _sid = str(_sync_err.get("submission_id") or "")
            verdict.update(
                enqueued=False,
                duplicate_sync_error=True,
                reason=f"duplicate submitted (sync_error): {title} {sha[:7]} already submitted (sid={_sid[:8]}); awaiting reconciliation",
            )
            print(json.dumps(verdict, ensure_ascii=False) if args.json else f"DUPLICATE: {verdict['reason']} (pass --force to override)")
            return 4
    except Exception:
        pass

    record = {
        "title": title,
        "sha": sha,
        "note": args.note or "queued via opsb",
        "queued_at": utc_now().isoformat(),
        "status": "queued",
        "estimated_score": None if "no perf telemetry" in str(bridge_est.get("detail", "")).lower() else est,
    }
    if args.force:
        record["force"] = True
    explicit_now = bool(getattr(args, "now", False))
    inserted = enqueue_once(ws, record)
    if not inserted and not (explicit_now and args.force):
        verdict.update(enqueued=False, reason="Already queued or previously submitted")
        print(json.dumps(verdict, ensure_ascii=False))
        return 4
    invalidate_ttl_cache("scheduler_bridge")
    verdict.update(enqueued=True, queued_record=record)

    if explicit_now:
        verdict["dispatched"] = spawn_jump_submit(
            CLI_DIR / "opsb", ws, title, sha, record["note"], force=bool(args.force),
            enforce_operator_mutex=not args.force,
        )

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
        if getattr(args, "lang", None) in ("zh", "en"):
            cfg["language"] = args.lang
        ConfigRepository.save(cfg)
        if bridge_extra:
            invoke_bridge(ws, "config", bridge_extra)
        invalidate_ttl_cache("scheduler_bridge")
        snap = compute_workspace_snapshot(ws, cfg)
        bridge = invoke_bridge(ws, "status", ["--offline"], use_cache=False)
        if isinstance(bridge, dict):
            merge_bridge_enrichment(snap, bridge)
        s_snap = snap["scheduler"]
        out_sched = {
            **sched,
            "per_account_daily_quota_limit": int(sched.get("daily_quota_limit", 50)),
            "configured_closing_reserve": int(sched.get("closing_reserve", 2)),
            "daily_quota_limit": int(s_snap["daily_quota_limit"]),
            "used_today_24h": int(s_snap["used_today_24h"]),
            "closing_reserve": int(s_snap["closing_reserve"]),
            "available_normal_shots": int(s_snap["available_normal_shots"]),
            "noise_floor_delta": float(s_snap["noise_floor_delta"]),
        }
        print(json.dumps({"ok": True, "scheduler": out_sched}, ensure_ascii=False))
        return 0

    if args.q_cmd == "skip":
        reason = args.reason or "skipped via Supervisor"
        meta = resolve_workspace_meta(ws)
        prob = next(
            (p for p in meta.get("problems", []) if str(p.get("title", "")).lower() == args.problem.lower()),
            None,
        )
        title = str(prob["title"]) if prob else args.problem
        canonical_sha = (resolve_commit_sha(ws / prob["path"], args.sha) if prob else "") or args.sha
        cancel_waiting_cooldown_for_commit(title, canonical_sha)
        bridge_res = invoke_bridge(
            ws,
            "override",
            ["--title", title, "--sha", canonical_sha, "--action", "skip", "--reason", reason],
        )
        now_iso = utc_now().isoformat()
        rec = {
            "title": title,
            "sha": canonical_sha,
            "note": reason,
            "queued_at": now_iso,
            "finished_at": now_iso,
            "status": "skipped",
        }
        already_appended = (
            isinstance(bridge_res, dict)
            and bridge_res.get("action") == "skip"
            and bridge_res.get("status") == "skipped"
            and not bridge_res.get("dry_run")
        )
        if not already_appended:
            append_jsonl_locked(ws / "results" / ".submit-queue-io.lock", ws / "results" / "submit-queue-outcome.jsonl", rec)
        elif bridge_res.get("sha"):
            rec["sha"] = str(bridge_res["sha"])
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
        prob = next((p for p in resolve_workspace_meta(ws)["problems"] if p["title"].lower() == args.problem.lower()), None)
        if not prob:
            raise SystemExit(f"Unknown problem: {args.problem}")
        sha = resolve_commit_sha(ws / prob["path"], args.sha) or args.sha
        res = spawn_jump_submit(
            CLI_DIR / "opsb",
            ws,
            prob["title"],
            sha,
            args.note or "jump-queue submit via Parhelion",
            account_id=getattr(args, "account_id", None),
            enforce_operator_mutex=not getattr(args, "force", False),
            force=bool(getattr(args, "force", False)),
            confirm_start=True,
        )
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 2

    if args.q_cmd == "_worker-submit-one":
        return run_worker_submit_one(ws, args.problem, args.sha, args.note or "", account_id=getattr(args, "account_id", "primary") or "primary", force=bool(getattr(args, "force", False)))

    if args.q_cmd == "drain":
        bridge_res = invoke_bridge(ws, "drain")
        invalidate_ttl_cache("scheduler_bridge")
        if bridge_res is not None:
            print(json.dumps(bridge_res, ensure_ascii=False))
            return 0
        print(json.dumps({"ok": False, "error": "Scheduler unavailable; queue preserved."}))
        return 2
    return 1



def _remote_problem_dir(workspace: Path, title: str) -> str:
    """Remote checkout path for a problem: `<npu_remote_root>/<Title>` (legacy default unchanged)."""
    try:
        manifest = json.loads((workspace / "opsb.workspace.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    root = ""
    if isinstance(manifest, dict):
        root = str(manifest.get("npu_remote_root") or "").strip()
        if not root:
            contest = str(manifest.get("contest_id") or "").strip()
            if contest:
                root = f"/home/developer/work/{contest}"
    if not root:
        root = device_text("work_root").rstrip("/") + "/" + device_text("work_prefix")
    return f"{root.rstrip('/')}/{title}"

def run_test(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    ws = ConfigRepository.resolve_workspace(args.workspace, cfg)
    host = cfg.get("npu_ssh_host") or "npu"
    py_exe = resolve_python_exe(ws)
    bridge_py = CLI_DIR.parent / "scheduler" / "cli_bridge.py"

    # Sync-only mode stays compatible with `npu-test.py --sync-only`
    if getattr(args, "sync_only", False):
        npu_runner = CLI_DIR.parent / "runtime" / "npu-test.py"
        if not npu_runner.is_file():
            npu_runner = ws / "tools" / "npu-test.py"
        if npu_runner.is_file():
            return subprocess.call([py_exe, str(npu_runner), args.problem, "--workspace", str(ws), "--sync-only"], cwd=str(ws))

    # Route 3-Phase Pipeline (`--build-only` Phase A / `--fast` Q0 / full Q1) through `scheduler.cli_bridge schedule-test`
    if bridge_py.is_file():
        help_out = subprocess.run(
            [py_exe, "-m", "scheduler.cli_bridge", "schedule-test", "--help"],
            cwd=str(CLI_DIR.parent),
            capture_output=True,
            text=True,
        ).stdout
        b_cmd = [
            py_exe, "-m", "scheduler.cli_bridge", "schedule-test",
            "--workspace", str(ws),
            "--npu-host", host,
            "--title", args.problem,
        ]
        if getattr(args, "sha", None):
            b_cmd += ["--sha", args.sha]
        for cid in getattr(args, "case_ids", None) or []:
            b_cmd += ["--case", cid]
        if getattr(args, "remeasure", False):
            b_cmd.append("--remeasure")
        if getattr(args, "suite", None):
            b_cmd.extend(["--suite", args.suite])
        if getattr(args, "level", None):
            b_cmd.extend(["--level", args.level])
        if getattr(args,"official_only",False):
            b_cmd.append("--official-only")
        for option in ("bucket","profile","experiment","timing","repeats","runner"):
            value = getattr(args, option, None)
            if value is not None:
                if option in ("profile","runner"):
                    value = str(Path(value).expanduser().resolve())
                b_cmd += ["--"+option,str(value)]
        if getattr(args, "fast", False):
            b_cmd.append("--fast")
        if getattr(args, "pmu", False) and "--pmu" in help_out:
            b_cmd.append("--pmu")
        if getattr(args, "dry_run", False):
            b_cmd.append("--dry-run")
        if getattr(args, "build_only", False):
            if "--build-only" in help_out:
                b_cmd.append("--build-only")
            else:
                # Direct lock-free Phase A 32-core CPU build fallback (0s NPU lock)
                meta = resolve_workspace_meta(ws)
                prob = next((p for p in meta["problems"] if p["title"].lower() == args.problem.lower()), None)
                if not prob:
                    raise SystemExit(f"Unknown problem: {args.problem}")
                sha = resolve_commit_sha(ws / prob["path"], getattr(args, "sha", None) or "HEAD") or ""
                toolchain_dir = device_text("toolchain_dir")
                soc = device_text("soc")
                remote_sh = (
                    f"set -e; source {toolchain_dir}/set_env.sh; "
                    f"cd {_remote_problem_dir(ws, prob['title'])}; "
                    f"git fetch origin --prune && git checkout --detach --force {sha}; "
                    f"NCPU=$(nproc 2>/dev/null || echo 16); "
                    f"LIVE=$(ls ~/.opsb/cpu_builds/*.json 2>/dev/null | wc -l); [ \"$LIVE\" -lt 1 ] && LIVE=1; "
                    f"USABLE=$(( NCPU - (NCPU / 10 > 2 ? NCPU / 10 : 2) )); "
                    f"JOBS=$(( USABLE / LIVE )); [ \"$JOBS\" -lt 4 ] && JOBS=4; [ \"$JOBS\" -gt 16 ] && JOBS=16; "
                    f"sed 's/COMPUTE_UNIT=.*/COMPUTE_UNIT=\"{soc}\"/' build.sh | bash -s -- -j\"$JOBS\""
                )
                return subprocess.call(["ssh", *SSH_CONTROL_OPTS, host, remote_sh])
        return subprocess.call(b_cmd, cwd=str(CLI_DIR.parent))

    npu_runner = CLI_DIR.parent / "runtime" / "npu-test.py"
    if not npu_runner.is_file():
        npu_runner = ws / "tools" / "npu-test.py"
    if npu_runner.is_file():
        cmd = [py_exe, str(npu_runner), args.problem, "--workspace", str(ws)]
        env = {
            **os.environ,
            **({"OPSB_FAST_LANE": "1"} if args.fast else {}),
            **({"OPSB_PMU": "1"} if getattr(args, "pmu", False) else {}),
        }
        return subprocess.call(cmd, cwd=str(ws), env=env)
    return 1
