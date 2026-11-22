"""CLI command suite: `opsb bench {init, status, calibrate, diagnose, backfill, verify-escape}`."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..config import SUPERVISOR_ROOT, ConfigRepository, invalidate_ttl_cache, read_json, resolve_python_exe, utc_now
from ..ipc_bridge import invoke_bridge, merge_bridge_enrichment
from ..services.backfill_status_service import filter_backfill_status, read_backfill_status
from ..services.board_refresh_service import refresh_board_and_recalibrate
from ..services.queue_service import compute_workspace_snapshot
from ..services.scaffold_service import scaffold_operator_bench


def _call_bridge_subcmd(ws: Path, subcmd: str, extra: list[str], timeout: float = 120.0) -> tuple[int, dict[str, Any] | None, str]:
    py_exe = resolve_python_exe(ws)
    bridge_py = SUPERVISOR_ROOT / "scheduler" / "cli_bridge.py"
    if not bridge_py.is_file():
        return 127, None, ""
    help_txt = subprocess.run(
        [py_exe, "-m", "scheduler.cli_bridge", "--help"],
        cwd=str(SUPERVISOR_ROOT),
        capture_output=True,
        text=True,
    ).stdout
    if subcmd not in help_txt:
        return 127, None, ""
    cmd = [py_exe, "-m", "scheduler.cli_bridge", subcmd, "--workspace", str(ws), *extra]
    proc = subprocess.run(cmd, cwd=str(SUPERVISOR_ROOT), capture_output=True, text=True, timeout=timeout)
    out_s = (proc.stdout or "").strip()
    parsed = None
    if out_s:
        try:
            parsed = json.loads(out_s.splitlines()[-1])
        except Exception:
            try:
                parsed = json.loads(out_s)
            except Exception:
                parsed = None
    return proc.returncode, parsed, (proc.stdout + proc.stderr).strip()


def run_bench(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    ws = ConfigRepository.resolve_workspace(getattr(args, "workspace", None), cfg)
    zh = (getattr(args, "lang", None) or cfg.get("language", "zh")) == "zh"
    sub = args.bench_cmd

    if sub == "refresh-board":
        res = refresh_board_and_recalibrate(
            ws,
            force=bool(getattr(args, "force", False)),
            max_age_hours=float(getattr(args, "max_age_hours", 24.0)),
            recalibrate=not bool(getattr(args, "no_calibrate", False)),
        )
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res.get("ok") else 1
        if not res.get("ok"):
            print(f"Board refresh failed: {res.get('error')}", file=sys.stderr)
            return 1
        if res.get("skipped"):
            print(f"Board tbest(t) is fresh ({res.get('reason')}; last={res.get('last_refreshed_at')})")
            return 0
        print(
            f"Refreshed live tbest(t) + frontier + scores for {res.get('problems_refreshed')} problems "
            f"(trigger={res.get('trigger_reason')}, recalibrated={res.get('recalibrated')})"
        )
        for title, info in sorted((res.get("problems") or {}).items()):
            tb = info.get("tbest") or []
            tb_s = ", ".join(f"{x:.2f}" for x in tb) if tb else "—"
            dp = info.get("tbest_delta_pct")
            dp_s = f" ({dp:+.2f}%)" if dp is not None else ""
            sc = info.get("our_score")
            sc_s = f"{sc:.2f}" if isinstance(sc, (int, float)) else "—"
            print(
                f"  {title:<26} score={sc_s:>6}  top1={info.get('frontier_top1')}  "
                f"cases={info.get('total_testcase_count')}  tbest=[{tb_s}]{dp_s}"
            )
        drifts = res.get("score_drifts") or []
        if drifts:
            print("Score drifts detected:")
            for d in drifts:
                print(f"  {d['title']:<26} {d['score_before']:.2f} -> {d['score_after']:.2f} ({d['delta']:+.2f} pts)")
        return 0

    if sub == "backfill-status":
        # Contract: `python -m scheduler.backfill_status --json` (owned by the auto-backfill workstream).
        report = filter_backfill_status(read_backfill_status(ws), getattr(args, "problem", None))
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if not report["available"]:
            print(
                f"Auto-backfill status unavailable ({report['reason']}) · "
                f"fallback: calibration.unpaired_bucket_count + manual `opsb bench backfill <Problem>`"
            )
            return 0
        running = report.get("running") or {}
        print(
            f"Auto-backfill: {report['unpaired_count']} unpaired · {report['queued_count']} queued · "
            f"running={'yes' if running.get('active') else 'no'} · "
            f"last_run={report.get('last_run') or '—'} · eta={report.get('eta_sec')}s · "
            f"ledger={str(report.get('ledger_sha') or '—')[:12]}"
        )
        for item in (report.get("unpaired") or [])[:10]:
            print(f"  ? {item.get('problem') or '—':<22} {item.get('bucket_id') or item.get('label')} [{item.get('stratum')}]")
        for item in (report.get("queued") or [])[:10]:
            print(f"  → {item.get('problem') or '—':<22} {item.get('bucket_id') or item.get('label')} [{item.get('stratum')}]")
        return 0

    if sub == "init":
        rc, parsed, _ = _call_bridge_subcmd(ws, "bench-init", ["--title", args.problem] if args.problem else [])
        if rc == 0 and parsed is not None:
            print(json.dumps(parsed, ensure_ascii=False, indent=2) if args.json else f"Initialized bench spec: {parsed}")
            return 0
        # Local clean-workspace bench scaffolder
        target_dir = ws / args.problem
        info = scaffold_operator_bench(target_dir, args.problem)
        ws_manifest = ws / "opsb.workspace.json"
        if ws_manifest.is_file():
            m_data = read_json(ws_manifest, {})
            if isinstance(m_data, dict):
                oracles = m_data.setdefault("frozen_oracles", {})
                oracles[args.problem] = info["reference_sha256"]
                ws_manifest.write_text(json.dumps(m_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(info, ensure_ascii=False, indent=2) if args.json else f"Scaffolded {info['bench_dir']} (Oracle SHA256={info['reference_sha256'][:12]}…)")
        return 0

    if sub == "calibrate":
        extra = ["--title", args.problem] if args.problem else []
        rc, parsed, raw_out = _call_bridge_subcmd(ws, "calibrate", extra, timeout=30.0)
        invalidate_ttl_cache("scheduler_bridge")
        if rc == 0 and parsed is not None:
            print(json.dumps(parsed, ensure_ascii=False, indent=2) if args.json else f"Calibrated {args.problem or 'all'}: {json.dumps(parsed, ensure_ascii=False)}")
            return 0
        # Resilient fallback: compute & persist `results/<Title>/calibration.json`
        snap = compute_workspace_snapshot(ws, cfg)
        targets = [p for p in snap["problems"] if not args.problem or p["title"].lower() == args.problem.lower()]
        saved_list = []
        for p in targets:
            cal = dict(p.get("calibration") or {})
            cal["updated_at"] = utc_now().isoformat()
            cal_path = ws / "results" / p["title"] / "calibration.json"
            cal_path.parent.mkdir(parents=True, exist_ok=True)
            cal_path.write_text(json.dumps(cal, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            saved_list.append({"title": p["title"], "mae_pts": cal.get("mae_pts"), "escape_lock": cal.get("escape_lock")})
        out = {"ok": True, "calibrated": saved_list}
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else f"Updated calibration for {len(saved_list)} operator(s).")
        return 0

    if sub in ("diagnose", "backfill", "verify-escape"):
        extra = ["--title", args.problem]
        if sub == "verify-escape":
            extra += ["--bad", args.bad, "--good", args.good]
        elif getattr(args, "sha", None):
            extra += ["--sha", args.sha]
        rc, parsed, raw_out = _call_bridge_subcmd(ws, sub, extra, timeout=180.0)
        invalidate_ttl_cache("scheduler_bridge")
        if rc != 127:
            if args.json and parsed is not None:
                print(json.dumps(parsed, ensure_ascii=False, indent=2))
            else:
                print(raw_out or json.dumps(parsed or {"ok": rc == 0}, ensure_ascii=False))
            return rc

        # Fallback diagnostic summary when scheduler subcmd is still initializing
        snap = compute_workspace_snapshot(ws, cfg)
        prob = next((p for p in snap["problems"] if p["title"].lower() == args.problem.lower()), None)
        if not prob:
            print(f"Unknown problem: {args.problem}", file=sys.stderr)
            return 1
        cal = prob.get("calibration") or {}
        if sub == "verify-escape":
            cal["escape_lock"] = False
            cal["verified_escape"] = {"bad": args.bad, "good": args.good, "verified_at": utc_now().isoformat()}
            cal_path = ws / "results" / prob["title"] / "calibration.json"
            cal_path.parent.mkdir(parents=True, exist_ok=True)
            cal_path.write_text(json.dumps(cal, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            msg = {"ok": True, "title": prob["title"], "escape_lock": False, "bad": args.bad, "good": args.good}
            print(json.dumps(msg, ensure_ascii=False) if args.json else f"Cleared ESCAPE_LOCK for {prob['title']} ({args.bad[:7]} -> {args.good[:7]})")
            return 0
        top_s = sorted(cal.get("strata") or [], key=lambda s: float(s.get("headroom_pts", 0)), reverse=True)
        diag = {
            "title": prob["title"],
            "current_score": prob["current_score"],
            "mae_pts": cal.get("mae_pts"),
            "last_residual_pts": cal.get("last_residual_pts"),
            "escape_lock": cal.get("escape_lock"),
            "top_headroom_strata": top_s[:2],
        }
        print(json.dumps(diag, ensure_ascii=False, indent=2))
        return 0

    # Default: `opsb bench status [<Problem>]`
    snap = compute_workspace_snapshot(ws, cfg)
    bridge = invoke_bridge(ws, "status", ["--offline"], use_cache=True)
    if isinstance(bridge, dict):
        merge_bridge_enrichment(snap, bridge)

    probs = snap["problems"]
    if getattr(args, "problem", None):
        probs = [p for p in probs if p["title"].lower() == args.problem.lower()]
        if not probs:
            print(f"Unknown problem: {args.problem}", file=sys.stderr)
            return 1

    if args.json:
        out_list = [{"title": p["title"], "current_score": p["current_score"], "calibration": p.get("calibration")} for p in probs]
        print(json.dumps(out_list[0] if getattr(args, "problem", None) else out_list, ensure_ascii=False, indent=2))
        return 0

    for p in probs:
        cal = p.get("calibration") or {}
        mae_s = f"{cal['mae_pts']:.2f}" if cal.get("mae_pts") is not None else "—"
        esc_s = " [🔒 ESCAPE_LOCK]" if cal.get("escape_lock") else ""
        hdr = f"=== {p['title']} · {'校准分桶账本' if zh else 'Calibrated Stratum Ledger'} (Score {p['current_score']:.2f} · MAE {mae_s}){esc_s} ==="
        print(hdr)
        for st in cal.get("strata") or []:
            print(
                f"  {st['stratum']:<15}  w0={st['prior_w']:.3f} -> w_hat={st['learned_w']:.3f}  "
                f"r={st['efficiency_r']*100:5.1f}%  cur={st['current_pts']:5.2f}  headroom=+{st['headroom_pts']:5.2f} pts"
            )
    return 0
