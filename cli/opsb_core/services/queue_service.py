"""4-Stage Submission Gate engine, queue state builder, and locked mutations."""
from __future__ import annotations

import fcntl
import re
from pathlib import Path
from typing import Any

from ..config import DEFAULT_CONFIG, DEFAULT_DIFFICULTIES, read_json, read_jsonl, utc_now
from .contest_service import build_problem_models, index_submission_history, resolve_workspace_meta


def estimate_score_from_note(note: str, base_score: float, best_score: float) -> float | None:
    if not note:
        return None
    low = note.lower()
    m_mean = re.search(r"mean\s*~?\s*(\d+(?:\.\d+)?)", low)
    if m_mean:
        return round(float(m_mean.group(1)), 2)
    m_best = re.search(r"(?:restore|best online|best)\s*:?\s*(\d+(?:\.\d+)?)", low)
    if m_best:
        return round(float(m_best.group(1)), 2)
    mults = [float(x) for x in re.findall(r"(\d+\.\d+)x\b", low) if 0.5 <= float(x) <= 8.0]
    if mults and base_score > 0:
        avg_mult = sum(mults) / len(mults)
        return round(min(96.0, base_score * (1.0 + 0.30 * (avg_mult - 1.0))), 2)
    pcts = [float(x) for x in re.findall(r"-(\d+(?:\.\d+)?)%", low) if 1.0 <= float(x) <= 80.0]
    if pcts and base_score > 0:
        avg_pct = (sum(pcts) / len(pcts)) / 100.0
        return round(min(96.0, base_score * (1.0 + 0.35 * (avg_pct / max(0.2, 1.0 - avg_pct)))), 2)
    if best_score > base_score and ("restore" in low or "rollback" in low):
        return round(best_score, 2)
    return None


def _probe_queue_lock(lock_file: Path) -> bool:
    if not lock_file.exists():
        return False
    try:
        with lock_file.open("r") as lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lf, fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True
    except Exception:
        return False


def compute_workspace_snapshot(workspace: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    sched_cfg = cfg.get("scheduler", DEFAULT_CONFIG["scheduler"])
    noise_floor = float(sched_cfg.get("noise_floor_delta", 2.7))
    daily_limit = int(sched_cfg.get("daily_quota_limit", 50))
    closing_reserve = int(sched_cfg.get("closing_reserve", 12))
    gap_sec = int(sched_cfg.get("rate_limit_gap_sec", 190))
    cb_limit = int(sched_cfg.get("circuit_breaker_limit", 6))

    now = utc_now()
    meta = resolve_workspace_meta(workspace)
    hist_idx = index_submission_history(workspace)
    problems_out, contest_summary = build_problem_models(workspace, meta, hist_idx, noise_floor, cb_limit)
    prob_map = {p["title"]: p for p in problems_out}

    last_sub_dt = hist_idx["last_submitted_dt"]
    elapsed_sub = (now - last_sub_dt).total_seconds() if last_sub_dt else 999999.0
    cooldown_sec = max(0, int(round(gap_sec - elapsed_sub)))
    avail_shots = max(0, daily_limit - hist_idx["used_24h"] - closing_reserve)

    results_dir = workspace / "results"
    queue_lock_held = _probe_queue_lock(results_dir / ".submit-queue.lock")
    queue_rows = read_jsonl(results_dir / "submit-queue.jsonl")
    outcome_rows = hist_idx["outcome_rows"]
    sha_pass_scores = hist_idx["sha_pass_scores"]

    outcomes_exact = {(o.get("title", ""), o.get("sha", ""), o.get("queued_at", "")): o for o in outcome_rows}
    outcomes_pair = {(o.get("title", ""), o.get("sha", "")): o for o in outcome_rows}

    def eval_gates(title: str, est_sc: float | None, is_restore_zero: bool, safety_ok: bool = True) -> dict[str, Any]:
        p_obj = prob_map.get(title, {})
        cur_sc = float(p_obj.get("current_score", 0.0))
        diff = int(p_obj.get("difficulty", 2))
        delta_exp = (est_sc - cur_sc) if est_sc is not None else 3.0
        noise_ok = is_restore_zero or cur_sc == 0.0 or (delta_exp >= noise_floor)
        quota_ok = is_restore_zero or (avail_shots > 0)
        circuit_ok = is_restore_zero or not bool(p_obj.get("circuit_open", False))
        return {
            "safety_ok": safety_ok,
            "noise_ok": noise_ok,
            "circuit_ok": circuit_ok,
            "quota_ok": quota_ok,
            "all_pass": safety_ok and noise_ok and circuit_ok and quota_ok,
            "delta_expected": round(delta_exp, 2),
            "wspt_score": round(diff * max(0.0, delta_exp), 2),
        }

    advisory_items: list[dict[str, Any]] = []
    queued_pairs = {
        (q.get("title", ""), q.get("sha", ""))
        for q in queue_rows
        if (q.get("title", ""), q.get("sha", ""), q.get("queued_at", "")) not in outcomes_exact
    }

    for p_obj in problems_out:
        t = p_obj["title"]
        if p_obj["current_score"] == 0.0 and p_obj["best_score"] > 0.0 and p_obj["best_sha"]:
            b_sha = p_obj["best_sha"]
            if (t, b_sha) not in queued_pairs:
                priors = sha_pass_scores.get((t, b_sha), [p_obj["best_score"]])
                est = round(sum(priors) / len(priors), 2)
                gates = eval_gates(t, est, is_restore_zero=True)
                advisory_items.append(
                    {
                        "id": f"advisory-restore-{t}-{b_sha[:7]}",
                        "kind": "advisory",
                        "title": t,
                        "difficulty": p_obj["difficulty"],
                        "sha": b_sha,
                        "short_sha": b_sha[:7],
                        "note": f"Board is 0 ({p_obj['status']}); validated SHA scored {p_obj['best_score']:.2f}",
                        "detail": "",
                        "submission_id": None,
                        "queued_at": now.isoformat(),
                        "finished_at": None,
                        "badge": "CRITICAL RESTORE",
                        "raw_status": "advisory_restore",
                        "current_score": 0.0,
                        "prior_score": 0.0,
                        "estimated_score": est,
                        "actual_score": None,
                        "delta_score": None,
                        "wspt_priority": gates["wspt_score"],
                        "gates": gates,
                        "can_approve": True,
                        "can_skip": False,
                        "can_restore": True,
                    }
                )

    prior_board: dict[str, float] = {}
    chron_items: list[dict[str, Any]] = []
    for idx, q in enumerate(queue_rows):
        t, sha, q_at = q.get("title", ""), q.get("sha", ""), q.get("queued_at", "")
        note = q.get("note", "")
        forced = bool(q.get("force", False))
        p_obj = prob_map.get(t, {"difficulty": DEFAULT_DIFFICULTIES.get(t, 2), "current_score": 0.0, "best_score": 0.0})

        out = outcomes_exact.get((t, sha, q_at)) or (None if forced else outcomes_pair.get((t, sha)))
        prior_sc = prior_board.get(t, 0.0)
        cur_sc = float(p_obj.get("current_score", 0.0))

        priors = sha_pass_scores.get((t, sha), [])
        est = estimate_score_from_note(note, prior_sc or cur_sc, float(p_obj.get("best_score", 0.0)))
        if est is None:
            est = round(sum(priors) / len(priors), 2) if priors else (round((prior_sc or cur_sc) + 3.0, 2) if (prior_sc or cur_sc) > 0 else 40.0)

        if out is None:
            is_rest = forced and (cur_sc == 0.0 or "restore" in note.lower())
            gates = eval_gates(t, est, is_restore_zero=is_rest)
            if queue_lock_held and idx == len(queue_rows) - 1:
                badge = "JUDGING"
            elif is_rest and cur_sc == 0.0:
                badge = "CRITICAL RESTORE"
            elif not gates["circuit_ok"] and not forced:
                badge = "CIRCUIT OPEN"
            elif not gates["noise_ok"] and not forced:
                badge = "HOLD: NOISE"
            elif cooldown_sec > 0:
                badge = "RATE WAIT"
            else:
                badge = "READY"
            chron_items.append(
                {
                    "id": f"queued-{idx}-{t}-{sha[:7]}",
                    "kind": "pending",
                    "title": t,
                    "difficulty": int(p_obj.get("difficulty", 2)),
                    "sha": sha,
                    "short_sha": sha[:7],
                    "note": note,
                    "detail": "",
                    "submission_id": None,
                    "queued_at": q_at,
                    "finished_at": None,
                    "badge": badge,
                    "raw_status": "queued",
                    "current_score": cur_sc,
                    "prior_score": round(prior_sc, 2),
                    "estimated_score": est,
                    "actual_score": None,
                    "delta_score": None,
                    "wspt_priority": gates["wspt_score"],
                    "gates": gates,
                    "can_approve": badge in ("HOLD: NOISE", "CIRCUIT OPEN"),
                    "can_skip": True,
                    "can_restore": False,
                }
            )
        else:
            st = out.get("status", "done")
            actual = float(out["score"]) if out.get("score") is not None else None
            delta = round(actual - prior_sc, 2) if (actual is not None and prior_sc > 0) else (round(actual, 2) if actual is not None else None)
            if st == "Pass":
                badge = "PASS ↑" if (delta is None or delta >= 0) else "PASS ↓"
                if actual is not None:
                    prior_board[t] = actual
            elif st == "skipped":
                badge = "HOLD: NOISE"
            elif st in ("Running", "Queued", "Pending"):
                badge = "JUDGING"
            else:
                badge = "REJECTED"
                if st in ("Wrong Answer", "Time Limit Exceeded", "Runtime Error", "Compile Error"):
                    prior_board[t] = 0.0
            chron_items.append(
                {
                    "id": f"done-{idx}-{t}-{sha[:7]}",
                    "kind": "completed",
                    "title": t,
                    "difficulty": int(p_obj.get("difficulty", 2)),
                    "sha": sha,
                    "short_sha": sha[:7],
                    "note": out.get("note") or note,
                    "detail": out.get("detail", ""),
                    "submission_id": out.get("submission_id"),
                    "queued_at": q_at,
                    "finished_at": out.get("finished_at") or out.get("submitted_at"),
                    "badge": badge,
                    "raw_status": st,
                    "current_score": cur_sc,
                    "prior_score": round(prior_sc, 2),
                    "estimated_score": est,
                    "actual_score": round(actual, 2) if actual is not None else None,
                    "delta_score": delta,
                    "wspt_priority": round(int(p_obj.get("difficulty", 2)) * max(0.0, est - prior_sc), 2),
                    "gates": {
                        "safety_ok": st not in ("rejected", "Compile Error", "Runtime Error", "Wrong Answer"),
                        "noise_ok": st != "skipped",
                        "circuit_ok": True,
                        "quota_ok": True,
                        "all_pass": st == "Pass",
                        "delta_expected": round(est - prior_sc, 2),
                        "wspt_score": round(int(p_obj.get("difficulty", 2)) * max(0.0, est - prior_sc), 2),
                    },
                    "can_approve": False,
                    "can_skip": False,
                    "can_restore": False,
                }
            )

    active_q = [x for x in (advisory_items + chron_items) if x["kind"] != "completed"]
    active_q.sort(key=lambda x: (x["badge"] == "CRITICAL RESTORE", x["wspt_priority"]), reverse=True)
    done_q = [x for x in chron_items if x["kind"] == "completed"]
    done_q.sort(key=lambda x: x.get("finished_at") or x.get("queued_at") or "", reverse=True)

    return {
        "sampled_at": now.isoformat(),
        "contest": contest_summary,
        "scheduler": {
            "auto_consumer_enabled": bool(sched_cfg.get("auto_consumer_enabled", False)),
            "rate_limit_gap_sec": gap_sec,
            "cooldown_remaining_sec": cooldown_sec,
            "rate_ready": cooldown_sec == 0,
            "last_submitted_at": last_sub_dt.isoformat() if last_sub_dt else None,
            "daily_quota_limit": daily_limit,
            "used_today_24h": hist_idx["used_24h"],
            "closing_reserve": closing_reserve,
            "available_normal_shots": avail_shots,
            "noise_floor_delta": noise_floor,
            "circuit_breaker_limit": cb_limit,
            "queue_lock_held": queue_lock_held,
            "actionable_count": len(active_q),
        },
        "problems": problems_out,
        "submission_queue": active_q + done_q,
    }


def check_commit_safety_gate(workspace: Path, title: str, sha: str) -> bool:
    res_json = workspace / "results" / title / sha / "result.json"
    val_log = workspace / "results" / title / sha / "validation.log"
    if not res_json.is_file() or not val_log.is_file():
        return False
    r_data = read_json(res_json, {})
    if r_data.get("exit_code") != 0:
        return False
    txt = val_log.read_text(encoding="utf-8", errors="replace")
    return ("EXTENDED_PASSED" in txt or "EXTENDED_RC=0" in txt) and "EXTENDED_FAILED" not in txt
