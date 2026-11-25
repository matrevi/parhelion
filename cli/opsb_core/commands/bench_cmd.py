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


def _format_diagnose_report(doc: dict[str, Any]) -> str:
    if "problems" in doc and isinstance(doc["problems"], dict):
        lines = [f"=== Parhelion Bench Diagnostics ({doc.get('problem_count', len(doc['problems']))} operators) ==="]
        for t, d in sorted(doc["problems"].items()):
            mae = f"{d['loocv_mae_pts']:.2f}" if isinstance(d.get("loocv_mae_pts"), (int, float)) else "—"
            rho = f"{d['loocv_spearman']:+.2f}" if isinstance(d.get("loocv_spearman"), (int, float)) else "—"
            tr = f"{d['calibration_trust']:.2f}" if isinstance(d.get("calibration_trust"), (int, float)) else "—"
            gam = f"{d['transfer_gamma']:.2f}" if isinstance(d.get("transfer_gamma"), (int, float)) else "—"
            evs = len(d.get("high_residual_events") or [])
            promo = len(d.get("untracked_promote_candidates") or [])
            pos = [x["bucket"] for x in (d.get("strong_positive_buckets") or [])[:2]]
            anti = [x["bucket"] for x in (d.get("misleading_anti_corr_buckets") or [])[:2]]
            lines.append(
                f"  {t:<25} LOOCV_MAE={mae:>5}  rho={rho:>5}  trust={tr}  gamma={gam}  "
                f"spec={d.get('spec_bucket_count', 0):2d}->eff={d.get('effective_bucket_count', 0):2d}  "
                f"hiRes={evs}  evolve+={promo}"
            )
            if pos or anti:
                lines.append(f"    +corr: {pos or ['—']}   -corr(suppressed): {anti or ['—']}")
        return "\n".join(lines)

    lines = [
        f"=== Bench Diagnose: {doc.get('title')} (v{doc.get('model_version', '2.0')}) ===",
        f"  LOOCV MAE={doc.get('loocv_mae_pts')}  Spearman={doc.get('loocv_spearman')}  "
        f"DirAcc={doc.get('loocv_dir_acc')}  Trust={doc.get('calibration_trust')}  "
        f"TransferGamma={doc.get('transfer_gamma')}  AlphaCold={doc.get('alpha_cold')}",
        f"  Buckets: spec={doc.get('spec_bucket_count')}  effective={doc.get('effective_bucket_count')}  "
        f"unpaired_spec={doc.get('unpaired_bucket_count')}  extra_discovered={len(doc.get('extra_discovered_buckets') or [])}",
    ]
    if isinstance(cv := doc.get("commit_validation"), dict):
        lines.append(
            f"  Commit {str(cv.get('resolved_sha') or '')[:7]}: found={cv.get('found')} passed={cv.get('passed')} "
            f"regime={cv.get('profiler_regime')} source={cv.get('perf_source')} buckets={cv.get('measured_bucket_count', 0)}"
        )
    if doc.get("strong_positive_buckets"):
        lines.append("  Strong +Corr Buckets (prioritize):")
        for b in doc["strong_positive_buckets"][:6]:
            tag = "SPEC" if b.get("in_spec") else "EXTR"
            lines.append(f"    [{tag}] {b['bucket']:<28} r={b['corr']:+.2f} (n={b['pairs']}) w={b.get('learned_w')} tau={b.get('tau_active_us')}us")
    if doc.get("misleading_anti_corr_buckets"):
        lines.append("  Anti-Corr / Misleading Buckets (suppressed):")
        for b in doc["misleading_anti_corr_buckets"][:5]:
            tag = "SPEC" if b.get("in_spec") else "EXTR"
            lines.append(f"    [{tag}] {b['bucket']:<28} r={b['corr']:+.2f} (n={b['pairs']}) w={b.get('learned_w')}")
    if doc.get("high_residual_events"):
        lines.append("  High-Residual / Sign-Flip Events (|eps|>=3.0):")
        for ev in doc["high_residual_events"][-5:]:
            sf = " [SIGN_FLIP]" if ev.get("sign_flip") else ""
            lines.append(
                f"    {ev['base_sha']}->{ev['cand_sha']}: act={ev['actual_delta']:+.2f} cv_pred={ev['pred_delta']:+.2f} "
                f"eps={ev['residual_eps']:+.2f}{sf} causes={ev.get('causes')}"
            )
    if doc.get("unexplained_residual_dimensions"):
        lines.append("  Unexplained Blind-Spot Dimensions:")
        for u in doc["unexplained_residual_dimensions"]:
            lines.append(f"    - {u}")
    if doc.get("untracked_promote_candidates"):
        ids = [x["id"] for x in doc["untracked_promote_candidates"][:8]]
        lines.append(f"  Evolve Candidates (append-only ready): {', '.join(ids)}")
    if doc.get("agent_guidance"):
        lines.append(f"  Guidance: {doc['agent_guidance']}")
    if doc.get("feedback_file"):
        lines.append(f"  Feedback persisted: {doc['feedback_file']}")
    return "\n".join(lines)


def _format_evolve_report(doc: dict[str, Any]) -> str:
    if doc.get("scheme")=="reference":
        return (f"{doc['title']} · {doc['receipts']} judge receipts · reproduced {doc['matching_cases']}\n"
                f"Experimental variants: {doc['proposed_variants']}\nProfile: {doc['profile']}")
    if "problems" in doc and isinstance(doc["problems"], dict):
        mode = "APPLIED" if doc.get("applied") else "DRY-RUN"
        lines = [f"=== Parhelion Bench Evolve [{mode}] (total appended: +{doc.get('total_appended_buckets', 0)} buckets) ==="]
        for t, r in sorted(doc["problems"].items()):
            if r.get("scheme")=="reference":
                lines.append(_format_evolve_report(r))
                continue
            ids = r.get("appended_ids") or []
            lines.append(
                f"  {t:<25} spec: {r.get('spec_buckets_before')} -> {r.get('spec_buckets_after')} (+{len(ids)})  "
                f"LOOCV_MAE: {r.get('loocv_mae_before')} -> {r.get('loocv_mae_after')}  "
                f"rho={r.get('loocv_spearman_after')}  trust={r.get('calibration_trust_after')}"
            )
            if ids:
                lines.append(f"    promoted: {', '.join(ids[:8])}" + (f" (+{len(ids)-8} more)" if len(ids) > 8 else ""))
        return "\n".join(lines)

    mode = "APPLIED" if doc.get("applied") else "DRY-RUN"
    ids = doc.get("appended_ids") or []
    lines = [
        f"=== Bench Evolve [{mode}]: {doc.get('title')} ===",
        f"  Spec buckets: {doc.get('spec_buckets_before')} -> {doc.get('spec_buckets_after')} (+{len(ids)}, append-only)",
        f"  LOOCV MAE: {doc.get('loocv_mae_before')} -> {doc.get('loocv_mae_after')}  "
        f"Spearman={doc.get('loocv_spearman_after')}  Trust={doc.get('calibration_trust_after')}",
    ]
    if ids:
        lines.append(f"  Promoted IDs: {', '.join(ids)}")
    if doc.get("skipped_anti_corr"):
        lines.append("  Skipped anti-corr: " + ", ".join(f"{x['id']}({x['corr']:+.2f})" for x in doc["skipped_anti_corr"][:6]))
    if doc.get("feedback_file"):
        lines.append(f"  Feedback file: {doc['feedback_file']}")
    return "\n".join(lines)


def run_bench(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    ws = ConfigRepository.resolve_workspace(getattr(args, "workspace", None), cfg)
    zh = (getattr(args, "lang", None) or cfg.get("language", "zh")) == "zh"
    sub = args.bench_cmd
    if sub in ("cases","compare"):
        cmd = [resolve_python_exe(ws), "-m","scheduler.case_bench",sub,str(ws),args.problem]
        if sub == "compare":
            cmd.extend([args.before,args.after])
        else:
            cmd.extend(["--sha",args.sha])
            if args.profile:
                cmd.extend(["--profile",str(Path(args.profile).expanduser().resolve())])
        if args.json:
            cmd.append("--json")
        return subprocess.call(cmd,cwd=SUPERVISOR_ROOT)

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

    if sub == "evolve":
        extra: list[str] = []
        if getattr(args, "problem", None):
            extra += ["--title", args.problem]
        if getattr(args, "dry_run", False):
            extra.append("--dry-run")
        if getattr(args, "min_pairs", None) is not None:
            extra += ["--min-pairs", str(args.min_pairs)]
        if getattr(args, "min_corr", None) is not None:
            extra += ["--min-corr", str(args.min_corr)]
        rc, parsed, raw_out = _call_bridge_subcmd(ws, "evolve", extra, timeout=180.0)
        invalidate_ttl_cache("scheduler_bridge")
        if args.json and parsed is not None:
            print(json.dumps(parsed, ensure_ascii=False, indent=2))
        elif parsed is not None:
            print(_format_evolve_report(parsed))
        else:
            print(raw_out or json.dumps({"ok": rc == 0}, ensure_ascii=False))
        return rc

    if sub in ("diagnose", "backfill", "verify-escape"):
        extra = ["--title", args.problem] if getattr(args, "problem", None) else []
        if sub == "verify-escape":
            extra += ["--bad-sha", args.bad, "--good-sha", args.good]
            if getattr(args, "review", None):
                extra += ["--review", args.review]
        elif getattr(args, "sha", None):
            extra += ["--sha", args.sha]
        rc, parsed, raw_out = _call_bridge_subcmd(ws, sub, extra, timeout=180.0)
        if sub == "verify-escape" and rc == 0 and parsed and parsed.get("ok"):
            from ..knowledge.bugs import sync_bugs
            sync_bugs(ws)
        invalidate_ttl_cache("scheduler_bridge")
        if rc != 127:
            if args.json and parsed is not None:
                print(json.dumps(parsed, ensure_ascii=False, indent=2))
            elif sub == "diagnose" and parsed is not None:
                print(_format_diagnose_report(parsed))
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
            print(json.dumps({"ok":False,"reason":"Escape verification backend unavailable"}))
            return 1
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
