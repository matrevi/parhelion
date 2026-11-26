"""CLI commands: `opsb status` and `opsb history`."""
from __future__ import annotations

import argparse
import json
import sys

from ..config import ConfigRepository
from ..ipc_bridge import invoke_bridge, merge_bridge_enrichment
from ..services.auth_service import get_auth_status
from ..services.backfill_status_service import probe_backfill_compact
from ..services.board_refresh_service import refresh_if_stale_async
from ..services.npu_service import poll_npu_telemetry
from ..services.queue_service import compute_workspace_snapshot
from ..services.skills_service import get_skills_status
from ..services.sync_reconciler import reconcile_sync_errors


def run_status(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    ws = ConfigRepository.resolve_workspace(args.workspace, cfg)
    if not getattr(args, "offline_npu", False):
        try:
            reconcile_sync_errors(ws, request_timeout=2.5)
        except Exception:
            pass
    payload = compute_workspace_snapshot(ws, cfg)

    bridge = invoke_bridge(ws, "status", ["--offline"], use_cache=True)
    if isinstance(bridge, dict):
        merge_bridge_enrichment(payload, bridge)

    probs_by_title = {p["title"]: p for p in payload["problems"]}
    ssh_host = cfg.get("npu_ssh_host") or payload["contest"].get("npu_ssh_host") or "npu"

    if getattr(args, "offline_npu", False):
        payload["npu"] = {"online": False, "ssh_host": ssh_host, "skipped": True, "die_locks": [], "tasks": []}
    else:
        payload["npu"] = poll_npu_telemetry(ssh_host, probs_by_title)

    # `scheduler.backfill` — passthrough of the B0 auto-backfill lane (scheduler/backfill.py).
    # Hand-assembled here because `build_status()` never sees `SchedulerEngine.summary()`.
    # Hard-capped at 200 ms and cached on the ledger sha: `opsb status` must stay fast, and
    # a missing/slow/broken module degrades to null instead of raising.
    payload["scheduler"]["backfill"] = (
        None if getattr(args, "no_backfill", False) else probe_backfill_compact(ws)
    )
    # 24h drift check: automatically refresh live tbest(t), frontier, board scores & recalibrate
    payload["scheduler"]["board_freshness"] = refresh_if_stale_async(ws)

    payload["auth"] = get_auth_status(use_cache=True)
    payload["skills"] = get_skills_status()
    payload["workspaces"] = cfg.get("workspaces", [str(ws)])
    payload["language"] = getattr(args, "lang", None) or cfg.get("language", "zh")

    if args.json:
        if getattr(args, "compact", False):
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    zh = payload["language"] == "zh"
    c, s, npu = payload["contest"], payload["scheduler"], payload["npu"]
    print(f"=== {c['title']} ({c['workspace']}) ===")
    if zh:
        print(
            f"总分: {c['weighted_score']:.2f} / {c['max_weighted_score']} (加权) | "
            f"原始分: {c['raw_score']:.2f} / {c['max_raw_score']} | 通过算子: {c['passing_count']}/{c['problem_count']}"
        )
    else:
        print(
            f"Score: {c['weighted_score']:.2f} / {c['max_weighted_score']} (weighted) | "
            f"Raw: {c['raw_score']:.2f} / {c['max_raw_score']} | Passing: {c['passing_count']}/{c['problem_count']}"
        )
    if npu.get("online"):
        dev = npu.get("primary_npu") or {}
        cpu = npu.get("cpu") or {}
        mem = npu.get("memory") or {}
        locks = ", ".join(
            f"die{lk['die']}:{lk['state']}" + (f"({lk['problem']}·{lk['stage']})" if lk["state"] == "HELD" else "")
            for lk in npu.get("die_locks", [])
        )
        print(
            f"NPU [{ssh_host}]: {dev.get('soc_name')} · SDK {npu.get('sdk_version')} | "
            f"AICore {dev.get('aicore_pct')}% | HBM {dev.get('hbm_used_mib')}/{dev.get('hbm_total_mib')} MiB | "
            f"{dev.get('power_w')}W {dev.get('temp_c')}°C | CPU {cpu.get('cores')}c load {cpu.get('load_1m')} | "
            f"RAM {mem.get('used_gib')}/{mem.get('total_gib')} GiB | {locks}"
        )
    else:
        print(f"NPU [{ssh_host}]: OFFLINE ({npu.get('error', 'skipped')})")

    print(
        f"Gate & Quota: Cooldown {s['cooldown_remaining_sec']}s/{s['rate_limit_gap_sec']}s | "
        f"24h Used {s['used_today_24h']}/{s['daily_quota_limit']} (Avail {s['available_normal_shots']}, "
        f"Reserve {s['closing_reserve']} shown only) | "
        f"Noise Floor Δ>={s['noise_floor_delta']}"
    )
    if s.get("slot_hint"):
        print(f"Slot hint: {s['slot_hint']}")
    print("\nProblems:")
    for p in payload["problems"]:
        r_str = f"#{p['rank']}" if p["rank"] else "—"
        pm = f"{p['public_mean_us']:.2f}us" if p["public_mean_us"] is not None else "—"
        chev = ("^" * int(p["difficulty"])).ljust(3)
        print(
            f"  [{chev}] {p['title']:<26} {p['status']:<14} "
            f"cur={p['current_score']:>5.2f}  best={p['best_score']:>5.2f}  rank={r_str:<5}  pub={pm:<8}  [{p['wspt_tag']}]"
        )
    return 0


def run_history(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    ws = ConfigRepository.resolve_workspace(args.workspace, cfg)
    payload = compute_workspace_snapshot(ws, cfg)
    probs = payload["problems"]
    if args.problem:
        probs = [p for p in probs if p["title"].lower() == args.problem.lower()]
        if not probs:
            print(f"Unknown problem: {args.problem}", file=sys.stderr)
            return 1

    if args.json:
        print(json.dumps(probs[0] if args.problem else probs, ensure_ascii=False, indent=2))
        return 0

    for p in probs:
        best_s = p["best_sha"][:7] if p["best_sha"] else "—"
        chev = "^" * int(p["difficulty"])
        print(f"=== {p['title']} [{chev}] · Current {p['current_score']:.2f} · Best {p['best_score']:.2f} ({best_s}) ===")
        if p["public_times_us"] and p["public_tbest_us"]:
            print("  Public Cases (tbest / ours):")
            for i, (ours, tb) in enumerate(zip(p["public_times_us"], p["public_tbest_us"])):
                r = tb / ours if ours > 0 else 0.0
                print(f"    Case #{i+1:02d}: ours={ours:6.3f} us | tbest={tb:6.3f} us | r={r:.3f} ({2*r:.2f}/2.00 pts)")
            print(f"  Accounting: Public={p['public_pts']:.2f}/10 + Hidden={p['hidden_pts']:.2f}/90 (implied r_h={p['implied_hidden_ratio']:.3f})")
        print("  Submission Timeline:")
        for h in p["history"][:10]:
            sc_s = f"{h['score']:.2f}" if h.get("score") is not None else "—"
            d_s = f"({h['delta']:+.2f})" if h.get("delta") is not None else ""
            print(f"    {str(h.get('at', ''))[:19]}  {h.get('sha', '')[:7]}  {h.get('status', ''):<18} {sc_s:>6} {d_s:<8}  {h.get('note', '')[:60]}")
    return 0
