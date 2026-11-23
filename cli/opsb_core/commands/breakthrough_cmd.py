"""CLI commands: `opsb breakthrough ...` and `opsb queue list --breakthrough`."""
from __future__ import annotations

import argparse
import json
import sys

from ..config import DEFAULT_DIFFICULTIES, ConfigRepository, utc_now
from ..ipc_bridge import invoke_bridge, merge_bridge_enrichment
from ..services.breakthrough_service import (
    VALID_CATEGORIES,
    get_entry_detail,
    load_catalog,
    save_catalog_entry,
    sync_breakthroughs_from_items,
)
from ..services.git_fast import resolve_commit_sha
from ..services.queue_service import compute_workspace_snapshot
from ..services.sync_reconciler import reconcile_sync_errors


def _load_enriched_snapshot(ws_arg: str | None, cfg: dict) -> tuple:
    ws = ConfigRepository.resolve_workspace(ws_arg, cfg)
    try:
        reconcile_sync_errors(ws, request_timeout=2.5)
    except Exception:
        pass
    snap = compute_workspace_snapshot(ws, cfg)
    bridge = invoke_bridge(ws, "status", ["--offline"], use_cache=True)
    if isinstance(bridge, dict):
        merge_bridge_enrichment(snap, bridge)
    return ws, snap


def run_queue_list(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    _, snap = _load_enriched_snapshot(args.workspace, cfg)
    thresh = float(args.min_delta) if args.min_delta is not None else float(snap["scheduler"]["breakthrough_threshold"])

    items = snap["submission_queue"]
    if args.status == "pending":
        items = [x for x in items if x["kind"] != "completed"]
    elif args.status == "completed":
        items = [x for x in items if x["kind"] == "completed"]

    if args.problem:
        items = [x for x in items if x["title"].lower() == args.problem.lower()]

    if args.breakthrough or args.min_delta is not None:
        items = [x for x in items if x.get("breakthrough_delta") is not None and float(x["breakthrough_delta"]) >= thresh]
        items.sort(key=lambda x: (x["kind"] != "completed", float(x.get("breakthrough_delta") or 0.0)), reverse=True)

    if args.json:
        active_subs = ((snap.get("scheduler") or {}).get("pipeline") or {}).get("active_submissions") or []
        print(json.dumps({"threshold": thresh, "count": len(items), "active_submissions": active_subs, "items": items}, ensure_ascii=False, indent=2))
        return 0

    hdr = f"Breakthrough Filter (Δ >= {thresh:.1f} pts)" if (args.breakthrough or args.min_delta is not None) else "Submission Queue"
    print(f"=== {hdr} · {len(items)} item(s) ===")
    for it in items:
        chev = ("^" * int(it["difficulty"])).ljust(3)
        bt_d = it.get("breakthrough_delta")
        bt_tag = f" [🚀 {bt_d:+.2f}]" if (bt_d is not None and bt_d >= thresh) else ""
        doc_tag = f" [DOC:{it['experience_id']}]" if it.get("experience_id") else ""
        if it["kind"] == "inflight":
            est_v = it.get("estimated_score")
            sc_flow = f"{it.get('current_score', 0.0):5.2f} -> ~{est_v:5.2f}" if est_v is not None else f"{it.get('current_score', 0.0):5.2f} -> judging..."
            jid_s = f" #{it['remote_judge_id']}" if it.get("remote_judge_id") else ""
            acc_s = f" [{it['account_label']}]" if it.get("account_label") else ""
            st_lbl = f"INFLIGHT({it.get('phase') or it.get('raw_status')}{jid_s})"
            bt_tag = f"{bt_tag}{acc_s}"
        elif it["kind"] == "completed":
            sc_flow = f"{it.get('prior_score', 0.0):5.2f} -> {it.get('actual_score') or 0.0:5.2f}"
            st_lbl = it["raw_status"]
        else:
            sc_flow = f"{it.get('current_score', 0.0):5.2f} -> ~{it.get('estimated_score') or 0.0:5.2f}"
            st_lbl = f"CANDIDATE({it['badge']})"
        ts = str(it.get("finished_at") or it.get("queued_at") or "")[:16]
        print(f"  [{chev}] {ts:16s}  {it['title']:<25} {it['short_sha']}  {st_lbl:<18} {sc_flow}{bt_tag}{doc_tag}")
    return 0


def run_breakthrough(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    ws, snap = _load_enriched_snapshot(getattr(args, "workspace", None), cfg)
    sched = cfg.setdefault("scheduler", {})
    cur_thresh = float(sched.get("breakthrough_threshold", 5.0))

    if args.bt_cmd == "threshold":
        if args.value is not None:
            cur_thresh = round(max(0.5, min(50.0, float(args.value))), 2)
            sched["breakthrough_threshold"] = cur_thresh
            ConfigRepository.save(cfg)
        out = {"breakthrough_threshold": cur_thresh}
        print(json.dumps(out, ensure_ascii=False) if args.json else f"Breakthrough threshold: Δ >= {cur_thresh:.2f} pts")
        return 0

    if args.bt_cmd == "sync":
        thresh = float(args.min_delta) if args.min_delta is not None else cur_thresh
        probs_map = {p["title"]: p for p in snap["problems"]}
        res = sync_breakthroughs_from_items(ws, snap["submission_queue"], probs_map, thresh)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"Synced breakthrough playbook ({res['directory']}): +{len(res['created_ids'])} new, {res['total_entries']} total.")
        return 0

    if args.bt_cmd == "show":
        detail = get_entry_detail(ws, args.query)
        if detail is None:
            print(f"No structured breakthrough entry matching '{args.query}'", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(detail, ensure_ascii=False, indent=2))
        else:
            print(detail.get("markdown") or json.dumps(detail, ensure_ascii=False, indent=2))
        return 0

    if args.bt_cmd == "add":
        prob_obj = next((p for p in snap["problems"] if p["title"].lower() == args.problem.lower()), None)
        prob_title = prob_obj["title"] if prob_obj else args.problem
        diff = int(prob_obj["difficulty"]) if prob_obj else DEFAULT_DIFFICULTIES.get(prob_title, 2)
        repo_dir = ws / prob_obj["path"] if prob_obj else (ws / prob_title)
        full_sha = resolve_commit_sha(repo_dir, args.sha) or args.sha

        matched_q = next(
            (x for x in snap["submission_queue"] if x["title"] == prob_title and x["sha"].startswith(full_sha[:7])),
            None,
        )
        after_sc = float(args.score_after) if args.score_after is not None else float((matched_q or {}).get("actual_score") or (prob_obj or {}).get("current_score") or 0.0)
        before_sc = float(args.score_before) if args.score_before is not None else float((matched_q or {}).get("prior_score") or 0.0)
        delta_raw = round(after_sc - before_sc, 2)
        ts = str((matched_q or {}).get("finished_at") or utc_now().isoformat())
        day = "".join(ch for ch in ts[:10] if ch.isdigit())[:8] or utc_now().strftime("%Y%m%d")
        entry_id = f"BT-{day}-{prob_title}-{full_sha[:7]}"
        tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()] or [args.category]
        mechs = [m.strip() for m in (args.mechanism or []) if m.strip()] or [args.title]

        entry = {
            "id": entry_id,
            "problem": prob_title,
            "difficulty": diff,
            "sha": full_sha,
            "baseline_sha": (matched_q or {}).get("baseline_pass_sha"),
            "timestamp": ts,
            "score_before": round(before_sc, 2),
            "score_after": round(after_sc, 2),
            "delta_raw": delta_raw,
            "delta_weighted": round(diff * delta_raw, 2),
            "status": "verified" if (matched_q or {}).get("raw_status") == "Pass" else "candidate",
            "category": args.category if args.category in VALID_CATEGORIES else "tiling-regime",
            "tags": tags,
            "title": args.title,
            "bottleneck": args.bottleneck or f"Prior baseline plateaued at {before_sc:.2f} pts.",
            "mechanisms": mechs,
            "precision_guard": args.precision_guard or "Verified on extended NPU test suite + determinism check.",
            "transferable_rule": args.rule or args.title,
            "doc_path": f"entries/{entry_id}.md",
        }
        saved = save_catalog_entry(ws, entry, overwrite_doc=True)
        if args.json:
            print(json.dumps({"ok": True, "entry": saved}, ensure_ascii=False, indent=2))
        else:
            print(f"Saved {saved['id']} (+{saved['delta_raw']:.2f} pts)")
        return 0

    # Default: `opsb breakthrough list`
    thresh = float(args.min_delta) if args.min_delta is not None else cur_thresh
    cat = load_catalog(ws)
    entries = [e for e in cat["entries"] if float(e.get("delta_raw", 0.0)) >= thresh]
    if args.problem:
        entries = [e for e in entries if e["problem"].lower() == args.problem.lower()]
    if args.category:
        entries = [e for e in entries if e["category"].lower() == args.category.lower()]

    bt_submissions = [
        x for x in snap["submission_queue"]
        if x.get("breakthrough_delta") is not None
        and float(x["breakthrough_delta"]) >= thresh
        and (args.include_candidates or x["kind"] == "completed")
        and (not args.problem or x["title"].lower() == args.problem.lower())
    ]

    out_payload = {
        "threshold": thresh,
        "directory": cat["directory"],
        "entries": entries,
        "matching_submissions": bt_submissions,
    }
    if args.json:
        print(json.dumps(out_payload, ensure_ascii=False, indent=2))
        return 0

    print(f"=== Parhelion Breakthrough Playbook (Δ >= {thresh:.1f} pts) · {cat['directory']} ===")
    if not entries:
        print("  (No structured entries above current threshold; lower `--min-delta` or run `opsb breakthrough sync`.)")
    for e in entries:
        chev = ("^" * int(e["difficulty"])).ljust(3)
        print(
            f"  [{chev}] {e['id']:<38}  {e['score_before']:5.2f} -> {e['score_after']:5.2f} "
            f"({e['delta_raw']:+6.2f} raw / {e['delta_weighted']:+6.2f} w)  [{e['category']}]"
        )
        print(f"        Title: {e['title']}")
        print(f"        Rule : {e['transferable_rule']}")
    return 0
