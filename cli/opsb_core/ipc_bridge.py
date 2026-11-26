"""IPC Adapter to sibling `scheduler.cli_bridge` with mtime-aware TTL caching."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import (
    SUPERVISOR_ROOT,
    invalidate_ttl_cache,
    read_jsonl,
    read_ttl_cache,
    resolve_python_exe,
    workspace_state_dir,
    write_ttl_cache,
)
from .services import gate_view
from .services.sync_reconciler import recent_unreconciled_receipt_pairs
from .services.contest_service import escape_advice

APPROVABLE = ("HOLD: LOW GAIN", "CIRCUIT OPEN")


def _safety_badge(stages: list[dict[str, Any]], default: str) -> str:
    """Name the Stage 1 block so a held card says what to do next, not just "rejected"."""
    code = next((str(s.get("code") or "") for s in stages if s.get("stage") == "SAFETY" and not s.get("passed")), "")
    return gate_view.safety_badge(code) if code else default

def _queue_fingerprint(workspace: Path) -> str:
    parts = [str(workspace)]
    for rel in ("results/submit-queue.jsonl", "results/submit-queue-outcome.jsonl", "results/score-snapshot.json",
                "results/scoring-evidence/live-board-tbest.json", "results/scoring-evidence/ranking-captain.json"):
        p = workspace / rel
        parts.append(str(p.stat().st_mtime_ns) if p.is_file() else "0")
    st_file = workspace_state_dir(workspace) / "scheduler_state.json"
    parts.append(str(st_file.stat().st_mtime_ns) if st_file.is_file() else "0")
    return "|".join(parts)


def invoke_bridge(workspace: Path, subcmd: str = "status", extra: list[str] | None = None, use_cache: bool = False) -> dict[str, Any] | None:
    bridge_py = SUPERVISOR_ROOT / "scheduler" / "cli_bridge.py"
    if not bridge_py.is_file():
        return None

    fp = _queue_fingerprint(workspace) if use_cache else ""
    if use_cache:
        cached = read_ttl_cache("scheduler_bridge", ttl_sec=15.0, fingerprint=fp)
        if isinstance(cached, dict):
            return cached

    py_exe = resolve_python_exe(workspace)
    cmd = [py_exe, "-m", "scheduler.cli_bridge", subcmd, "--workspace", str(workspace)]
    if extra:
        cmd.extend(extra)
    try:
        proc = subprocess.run(cmd, cwd=str(SUPERVISOR_ROOT), capture_output=True, text=True, timeout=8.0)
        if proc.returncode == 0 and proc.stdout.strip():
            parsed = json.loads(proc.stdout.strip())
            if use_cache and isinstance(parsed, dict):
                write_ttl_cache("scheduler_bridge", parsed, fingerprint=fp)
            else:
                invalidate_ttl_cache("scheduler_bridge")
            return parsed
    except Exception:
        pass
    return None


def merge_bridge_enrichment(payload: dict[str, Any], bridge: dict[str, Any]) -> None:
    """Enrich snapshot with paired-kperf estimates, 4-stage verdicts, and quota from scheduler.cli_bridge."""
    b_quota = bridge.get("quota") or {}
    b_cfg = bridge.get("config") or {}
    sched = payload["scheduler"]

    active_slots = [
        s for s in ((sched.get("pipeline") or {}).get("account_slots") or [])
        if s.get("enabled") and s.get("logged_in")
    ]
    if "reserved_for_closing" in b_quota:
        sched["closing_reserve"] = int(b_quota["reserved_for_closing"])
    if active_slots:
        # Preserve multi-account team totals (e.g. 2x50=100, 3x50=150) instead of overwriting with single-account 50
        per_acc_lim = int(b_cfg.get("daily_limit") or 50)
        team_limit = sum(int(s.get("daily_limit") or per_acc_lim) for s in active_slots)
        team_used = sum(int(s.get("used_today") or 0) for s in active_slots)
        team_rem = sum(max(0, int(s.get("daily_limit") or per_acc_lim) - int(s.get("used_today") or 0)) for s in active_slots)
        sched["daily_quota_limit"] = team_limit
        sched["used_today_24h"] = team_used
        sched["available_normal_shots"] = max(0, team_rem - int(sched.get("closing_reserve") or 0))
    else:
        if "used_today" in b_quota:
            sched["used_today_24h"] = int(b_quota["used_today"])
        if "daily_limit" in b_quota:
            sched["daily_quota_limit"] = int(b_quota["daily_limit"])
        if "discretionary_remaining" in b_quota:
            sched["available_normal_shots"] = int(b_quota["discretionary_remaining"])
    if "noise_floor_pts" in b_cfg:
        sched["noise_floor_delta"] = float(b_cfg["noise_floor_pts"])

    # Overlay per-problem `calibration` from `scheduler.cli_bridge` when present
    b_probs = bridge.get("problems") or bridge.get("calibrations") or {}
    if isinstance(b_probs, list):
        b_probs = {bp.get("title"): bp for bp in b_probs if isinstance(bp, dict) and bp.get("title")}
    if isinstance(b_probs, dict) and b_probs:
        maes: list[float] = []
        for p_item in payload["problems"]:
            bp = b_probs.get(p_item["title"])
            if isinstance(bp, dict):
                b_cal = bp.get("calibration") if "calibration" in bp else (bp if "strata" in bp else None)
                if isinstance(b_cal, dict):
                    cur_cal = dict(p_item.get("calibration") or {})
                    cur_cal.update({k: v for k, v in b_cal.items() if v is not None})
                    p_item["calibration"] = cur_cal
                    if cur_cal.get("escape_lock"):
                        p_item["wspt_tag"] = escape_advice(p_item.get("latest_status"))
                    elif "board_advice" in p_item:
                        p_item["wspt_tag"] = p_item["board_advice"]
            c_mae = (p_item.get("calibration") or {}).get("mae_pts")
            if c_mae is not None:
                maes.append(float(c_mae))
        if maes:
            payload["contest"]["model_mae_pts"] = round(sum(maes) / len(maes), 2)

    est_by_pair: dict[tuple[str, str], float] = {}
    sq_meta_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for sq in bridge.get("submit_queue") or []:
        if sq.get("title") and sq.get("sha"):
            if sq.get("estimated_score") is not None:
                est_by_pair[(sq["title"], sq["sha"])] = round(float(sq["estimated_score"]), 2)
            sq_meta_by_pair[(sq["title"], sq["sha"])] = sq

    existing = {(it["title"], it["sha"]) for it in payload["submission_queue"]}
    prob_diff = {p["title"]: p["difficulty"] for p in payload["problems"]}

    pipeline = sched.get("pipeline") or {}
    inflight_title = pipeline.get("active_title")
    inflight_short = pipeline.get("active_short_sha") or ""
    inflight_key_set = set(pipeline.get("inflight_keys") or [])
    completed_short_pairs = {
        (it["title"], str(it.get("sha") or "").lower()[:7])
        for it in payload["submission_queue"]
        if it.get("kind") in ("held", "completed") and it.get("title") and len(str(it.get("sha") or "")) >= 7
    }
    skipped_pairs: set[tuple[str, str]] = set()
    skipped_short_pairs: set[tuple[str, str]] = set()
    for sk_key in sched.get("skipped_keys") or []:
        if "|" in str(sk_key):
            st_t, st_s7 = str(sk_key).split("|", 1)
            if st_t and len(st_s7) >= 7:
                skipped_short_pairs.add((st_t, st_s7.lower()[:7]))
    for it in payload["submission_queue"]:
        if it.get("raw_status") == "skipped" and it.get("title") and it.get("sha"):
            s_low = str(it["sha"]).lower()
            skipped_pairs.add((it["title"], s_low))
            if len(s_low) >= 7:
                skipped_short_pairs.add((it["title"], s_low[:7]))
    ws_raw = (payload.get("contest") or {}).get("workspace")
    unreconciled_receipt_pairs: set[tuple[str, str]] = set()
    if ws_raw:
        ws_path = Path(str(ws_raw))
        unreconciled_receipt_pairs = recent_unreconciled_receipt_pairs(ws_path)
        out_file = ws_path / "results" / "submit-queue-outcome.jsonl"
        if out_file.is_file():
            for o in read_jsonl(out_file):
                if o.get("status") == "skipped" and o.get("title") and o.get("sha"):
                    s_low = str(o["sha"]).lower()
                    skipped_pairs.add((str(o["title"]), s_low))
                    if len(s_low) >= 7:
                        skipped_short_pairs.add((str(o["title"]), s_low[:7]))
    inflight_key_lower = {
        (k.split("|", 1)[0], k.split("|", 1)[1].lower()[:7])
        for k in inflight_key_set
        if "|" in str(k)
    }
    for sq in bridge.get("submit_queue") or []:
        if sq.get("status_tag") == "SKIPPED" and sq.get("title") and sq.get("sha"):
            s_low = str(sq["sha"]).lower()
            skipped_pairs.add((str(sq["title"]), s_low))
            if len(s_low) >= 7:
                skipped_short_pairs.add((str(sq["title"]), s_low[:7]))

    extra_cands: list[dict[str, Any]] = []
    bt_thresh = float(sched.get("breakthrough_threshold", 5.0))
    held = {(sq["title"], sq["sha"]): sq for sq in bridge.get("submit_queue") or []
            if sq.get("queue_state") == "HELD"}
    candidates = [uc for uc in bridge.get("unsubmitted_candidates") or []
                  if (uc.get("title"), uc.get("sha")) not in held]
    for sq in bridge.get("submit_queue") or []:
        if sq.get("queue_state") == "HELD" and (sq["title"], sq["sha"]) not in existing:
            candidates.append({**sq, "approved": bool(sq.get("stages")) and all(st["passed"] for st in sq["stages"]),
                               "estimate": sq})
    for uc in candidates:
        t, sha = uc.get("title", ""), uc.get("sha", "")
        sha_low = sha.lower()
        short_s = sha_low[:7]
        raw_tag = str(uc.get("status_tag") or "READY")
        if raw_tag.upper() == "SKIPPED":
            continue
        if (t, sha_low) in skipped_pairs or (len(short_s) >= 7 and (t, short_s) in skipped_short_pairs):
            continue
        if f"{t}|{sha[:7]}" in inflight_key_set or (len(short_s) >= 7 and ((t, short_s) in inflight_key_lower or (t, short_s) in unreconciled_receipt_pairs)):
            continue
        if pipeline.get("is_busy") and t == inflight_title and inflight_short and sha_low.startswith(inflight_short.lower()):
            continue
        is_crit_rest_pre = bool(uc.get("is_critical_restore", False))
        if (t, short_s) in completed_short_pairs and not is_crit_rest_pre:
            continue
        est_obj = uc.get("estimate") or {}
        est_sc = round(float(est_obj["estimated_score"]), 2) if est_obj.get("estimated_score") is not None and "no perf telemetry" not in str(est_obj.get("detail", "")).lower() else None
        cur_sc = round(float(est_obj.get("current_score") or 0.0), 2)
        if est_sc is not None:
            est_by_pair[(t, sha)] = est_sc

        # Lamps, badge and reason come only from the gate verdict (4.1 #9, services.gate_view).
        fields = gate_view.card_fields(uc)
        gates, badge = fields["gates"], fields["badge"]
        note = " · ".join(x for x in (est_obj.get("detail"), fields["detail"]) if x)
        is_crit_rest = bool(uc.get("is_critical_restore", False))
        cand_bt = round(est_sc - cur_sc, 2) if (est_sc is not None and cur_sc > 0 and not is_crit_rest) else None

        if (t, sha) in existing:
            for item in payload["submission_queue"]:
                if item["title"] == t and item["sha"] == sha and item["kind"] not in ("completed", "inflight"):
                    if est_sc is not None:
                        item["estimated_score"] = est_sc
                    item["breakthrough_delta"] = cand_bt
                    item["is_breakthrough"] = bool(cand_bt is not None and cand_bt >= bt_thresh)
                    # A forced queue row takes only its own (forced) verdict from `submit_queue`
                    # below; an unforced candidate verdict for the same SHA does not apply to it.
                    if not item.get("forced"):
                        item.update(gates=gates, badge=badge, detail=fields["detail"], next_step=fields["next_step"],
                                    can_approve=badge in APPROVABLE, wspt_priority=gates["wspt_score"])
                        if note:
                            item["note"] = note
        else:
            existing.add((t, sha))
            extra_cands.append(
                {
                    "id": f"live-{t}-{sha[:7].lower()}" if sha[:7] else f"bridge-{t}",
                    "kind": "candidate",
                    "requested_submission": (t, sha) in held,
                    "agent_id": uc.get("agent_id"),
                    "request_note": uc.get("note"),
                    "speedup": uc.get("speedup"),
                    "title": t,
                    "difficulty": prob_diff.get(t, int(est_obj.get("difficulty") or 2)),
                    "sha": sha,
                    "short_sha": sha[:7],
                    "note": note or "Validated candidate",
                    "detail": fields["detail"],
                    "next_step": fields["next_step"],
                    "submission_id": None,
                    "queued_at": bridge.get("generated_at", payload["sampled_at"]),
                    "finished_at": None,
                    "badge": badge,
                    "raw_status": "validated_candidate",
                    "current_score": cur_sc,
                    "prior_score": cur_sc,
                    "estimated_score": est_sc,
                    "actual_score": None,
                    "delta_score": None,
                    "breakthrough_delta": cand_bt,
                    "is_breakthrough": bool(cand_bt is not None and cand_bt >= bt_thresh),
                    "experience_id": None,
                    "baseline_pass_sha": None,
                    "wspt_priority": gates["wspt_score"],
                    "gates": gates,
                    "can_approve": True,
                    "can_skip": True,
                    "can_restore": is_crit_rest,
                }
            )

    for item in payload["submission_queue"]:
        if item["kind"] not in ("completed", "inflight"):
            pair = (item["title"], item["sha"])
            if pair in est_by_pair:
                item["estimated_score"] = est_by_pair[pair]
                if item["current_score"] > 0 and item["badge"] != "CRITICAL RESTORE":
                    cbd = round(item["estimated_score"] - item["current_score"], 2)
                    item["breakthrough_delta"] = cbd
                    item["is_breakthrough"] = cbd >= bt_thresh
            sq_m = sq_meta_by_pair.get(pair)
            if sq_m and isinstance(item.get("gates"), dict):
                if item["kind"] == "pending" and sq_m.get("stages"):
                    fields = gate_view.card_fields(sq_m)
                    item.update(gates=fields["gates"], badge=fields["badge"], detail=fields["detail"],
                                next_step=fields["next_step"], can_approve=fields["badge"] in APPROVABLE,
                                wspt_priority=fields["gates"]["wspt_score"])
                    if str(sq_m.get("status_tag")) == "PROBE":
                        item["note"] = item["detail"]
                if item["gates"].get("ucb_delta") is None and sq_m.get("ucb_delta") is not None:
                    item["gates"]["ucb_delta"] = round(float(sq_m["ucb_delta"]), 2)
                if item["gates"].get("explore_eligible") is None and sq_m.get("explore_eligible") is not None:
                    item["gates"]["explore_eligible"] = bool(sq_m["explore_eligible"])
                if item["gates"].get("calibration_trust") is None and sq_m.get("calibration_trust") is not None:
                    item["gates"]["calibration_trust"] = round(float(sq_m["calibration_trust"]), 3)

    # Top-K candidate archive per problem (R12): on the problem rows, and its rank on matching cards.
    archive = bridge.get("candidate_archive") if isinstance(bridge.get("candidate_archive"), dict) else {}
    for p_item in payload["problems"]:
        p_item["candidate_archive"] = archive.get(p_item["title"], [])
    ranks = {(t, str(c.get("sha") or "").lower()[:7]): c.get("rank") for t, rows in archive.items() for c in rows}
    for item in payload["submission_queue"] + extra_cands:
        rank = ranks.get((item.get("title"), str(item.get("sha") or "").lower()[:7]))
        if rank is not None and item.get("kind") not in ("completed", "inflight"):
            item["archive_rank"] = rank

    inflight = [x for x in payload["submission_queue"] if x["kind"] == "inflight"]
    active = [x for x in payload["submission_queue"] if x["kind"] not in ("completed", "inflight")] + extra_cands
    active.sort(
        key=lambda x: (
            0 if x["badge"] == "CRITICAL RESTORE" else 1,
            -float(x.get("wspt_priority") or 0.0),
            str(x.get("title") or ""),
            str(x.get("short_sha") or "").lower(),
        )
    )
    done = [x for x in payload["submission_queue"] if x["kind"] == "completed"]
    payload["submission_queue"] = inflight + active + done
    sched["actionable_count"] = len(active)
    sched["breakthrough_count"] = sum(1 for x in payload["submission_queue"] if x["kind"] == "completed" and x.get("is_breakthrough"))
