"""IPC Adapter to sibling `scheduler.cli_bridge` with mtime-aware TTL caching."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import SUPERVISOR_ROOT, invalidate_ttl_cache, read_ttl_cache, resolve_python_exe, write_ttl_cache


def _queue_fingerprint(workspace: Path) -> str:
    parts = [str(workspace)]
    for rel in ("results/submit-queue.jsonl", "results/submit-queue-outcome.jsonl", "results/score-snapshot.json"):
        p = workspace / rel
        parts.append(str(p.stat().st_mtime_ns) if p.is_file() else "0")
    st_file = SUPERVISOR_ROOT / "scheduler" / ".state" / "scheduler_state.json"
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
        proc = subprocess.run(cmd, cwd=str(SUPERVISOR_ROOT), capture_output=True, text=True, timeout=3.5)
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
    if len(active_slots) >= 2:
        # Preserve multi-account team totals (e.g. 2x50=100, 3x50=150) instead of overwriting with single-account 50
        per_acc_lim = int(b_quota.get("daily_limit") or 50)
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
                        p_item["wspt_tag"] = "ESCAPE LOCK"
            c_mae = (p_item.get("calibration") or {}).get("mae_pts")
            if c_mae is not None:
                maes.append(float(c_mae))
        if maes:
            payload["contest"]["model_mae_pts"] = round(sum(maes) / len(maes), 2)

    est_by_pair: dict[tuple[str, str], float] = {}
    for sq in bridge.get("submit_queue") or []:
        if sq.get("title") and sq.get("sha") and sq.get("estimated_score") is not None:
            est_by_pair[(sq["title"], sq["sha"])] = round(float(sq["estimated_score"]), 2)

    existing = {(it["title"], it["sha"]) for it in payload["submission_queue"]}
    prob_diff = {p["title"]: p["difficulty"] for p in payload["problems"]}
    badge_norm = {
        "HOLD_NOISE": "HOLD: NOISE",
        "CIRCUIT_OPEN": "CIRCUIT OPEN",
        "RATE_WAIT": "RATE WAIT",
        "CRITICAL_RESTORE": "CRITICAL RESTORE",
    }

    pipeline = sched.get("pipeline") or {}
    inflight_title = pipeline.get("active_title")
    inflight_short = pipeline.get("active_short_sha") or ""

    extra_cands: list[dict[str, Any]] = []
    bt_thresh = float(sched.get("breakthrough_threshold", 5.0))
    for uc in bridge.get("unsubmitted_candidates") or []:
        t, sha = uc.get("title", ""), uc.get("sha", "")
        if pipeline.get("is_busy") and t == inflight_title and inflight_short and sha.startswith(inflight_short):
            continue
        est_obj = uc.get("estimate") or {}
        est_sc = round(float(est_obj["estimated_score"]), 2) if est_obj.get("estimated_score") is not None else None
        cur_sc = round(float(est_obj.get("current_score") or 0.0), 2)
        if est_sc is not None:
            est_by_pair[(t, sha)] = est_sc

        st_map = {s.get("stage"): bool(s.get("passed")) for s in (uc.get("stages") or [])}
        gates = {
            "safety_ok": st_map.get("SAFETY", True),
            "noise_ok": st_map.get("NOISE_FLOOR", True),
            "circuit_ok": st_map.get("CIRCUIT_BREAKER", True),
            "quota_ok": st_map.get("QUOTA_RESERVE", True),
            "all_pass": bool(uc.get("approved", False)),
            "delta_expected": round(float(est_obj.get("delta_expected") or 0.0), 2),
            "wspt_score": round(float(uc.get("priority_score") or 0.0), 2),
        }
        raw_tag = uc.get("status_tag") or "READY"
        badge = badge_norm.get(raw_tag, raw_tag.replace("_", " "))
        note = " · ".join(x for x in (est_obj.get("detail"), uc.get("gate_reason")) if x)
        is_crit_rest = bool(uc.get("is_critical_restore", False))
        cand_bt = round(est_sc - cur_sc, 2) if (est_sc is not None and cur_sc > 0 and not is_crit_rest) else None

        if (t, sha) in existing:
            for item in payload["submission_queue"]:
                if item["title"] == t and item["sha"] == sha and item["kind"] != "completed":
                    if est_sc is not None:
                        item["estimated_score"] = est_sc
                    item["breakthrough_delta"] = cand_bt
                    item["is_breakthrough"] = bool(cand_bt is not None and cand_bt >= bt_thresh)
                    item["gates"] = gates
                    item["wspt_priority"] = gates["wspt_score"]
                    if note:
                        item["note"] = note
        else:
            existing.add((t, sha))
            extra_cands.append(
                {
                    "id": f"bridge-{t}-{sha[:7]}",
                    "kind": "candidate",
                    "title": t,
                    "difficulty": prob_diff.get(t, int(est_obj.get("difficulty") or 2)),
                    "sha": sha,
                    "short_sha": sha[:7],
                    "note": note or "Validated candidate",
                    "detail": uc.get("gate_reason", ""),
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
        if item["kind"] != "completed":
            pair = (item["title"], item["sha"])
            if pair in est_by_pair:
                item["estimated_score"] = est_by_pair[pair]
                if item["current_score"] > 0 and item["badge"] != "CRITICAL RESTORE":
                    cbd = round(item["estimated_score"] - item["current_score"], 2)
                    item["breakthrough_delta"] = cbd
                    item["is_breakthrough"] = cbd >= bt_thresh

    if extra_cands:
        active = [x for x in payload["submission_queue"] if x["kind"] != "completed"] + extra_cands
        active.sort(key=lambda x: (x["badge"] == "CRITICAL RESTORE", x["wspt_priority"]), reverse=True)
        done = [x for x in payload["submission_queue"] if x["kind"] == "completed"]
        payload["submission_queue"] = active + done
        sched["actionable_count"] = len(active)
    sched["breakthrough_count"] = sum(1 for x in payload["submission_queue"] if x.get("is_breakthrough"))
