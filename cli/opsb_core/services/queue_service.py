"""4-Stage Submission Gate engine, queue state builder, and locked mutations."""
from __future__ import annotations

import fcntl
import json
import re
import sys
from pathlib import Path
from typing import Any

from ..config import DEFAULT_CONFIG, DEFAULT_DIFFICULTIES, SUPERVISOR_ROOT, read_json, read_jsonl, utc_now
from .breakthrough_service import index_experience_by_commit
from .contest_service import build_problem_models, index_submission_history, resolve_workspace_meta
from .judge_score_fetch import TERMINAL_JUDGE_STATUSES
from .judge_sync import compute_synchronized_pipeline
from .quota_helpers import build_inflight_queue_items, compute_effective_closing_reserve
from .sync_reconciler import recent_unreconciled_receipt_pairs, reconcile_sync_errors_async


def enqueue_once(workspace: Path, record: dict[str, Any]) -> bool:
    results = workspace / "results"
    results.mkdir(parents=True, exist_ok=True)
    queue = results / "submit-queue.jsonl"
    with (results / ".submit-queue-io.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        key = (record["title"], record["sha"][:7])
        outcomes = [r for r in read_jsonl(results / "submit-queue-outcome.jsonl")
                    if (r.get("title"), (r.get("sha") or "")[:7]) == key]
        done = {r.get("queued_at") for r in outcomes}
        if not record.get("force") and any(r.get("submission_id") or r.get("status") in
                                             ("blocked_compliance", "skipped") for r in outcomes):
            return False
        if any((r.get("title"), (r.get("sha") or "")[:7]) == key and r.get("queued_at") not in done
               for r in read_jsonl(queue)):
            return False
        with queue.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True


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


def _outcome_precedence(o: dict[str, Any]) -> tuple[int, int, int, str]:
    """Ensure real judged outcomes WITH `submission_id` always outrank `skipped` rows without one."""
    sid = str(o.get("submission_id") or "").strip()
    st = str(o.get("status") or "").strip()
    has_verdict = 1 if (sid and st in TERMINAL_JUDGE_STATUSES) else 0
    has_score = 1 if (o.get("score") is not None) else 0
    has_sid = 1 if sid else 0
    ts = str(o.get("finished_at") or o.get("submitted_at") or o.get("queued_at") or "")
    return (has_verdict, has_score, has_sid, ts)


def _prefer_outcome(table: dict[Any, dict[str, Any]], key: Any, row: dict[str, Any]) -> None:
    prev = table.get(key)
    if prev is None or _outcome_precedence(row) >= _outcome_precedence(prev):
        table[key] = row


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
    bt_threshold = float(sched_cfg.get("breakthrough_threshold", 5.0))
    daily_limit = int(sched_cfg.get("daily_quota_limit", 50))
    closing_reserve = int(sched_cfg.get("closing_reserve", 12))
    gap_sec = 0
    cb_limit = int(sched_cfg.get("circuit_breaker_limit", 6))

    now = utc_now()
    reconcile_sync_errors_async(workspace)
    meta = resolve_workspace_meta(workspace)
    exp_map = index_experience_by_commit(workspace)
    hist_idx = index_submission_history(workspace)
    problems_out, contest_summary = build_problem_models(workspace, meta, hist_idx, noise_floor, cb_limit)
    prob_map = {p["title"]: p for p in problems_out}

    results_dir = workspace / "results"
    queue_lock_held = _probe_queue_lock(results_dir / ".submit-queue.lock")
    queue_rows = read_jsonl(results_dir / "submit-queue.jsonl")
    outcome_rows = hist_idx["outcome_rows"]
    sha_pass_scores = hist_idx["sha_pass_scores"]

    outcomes_exact: dict[tuple[str, str, str], dict[str, Any]] = {}
    outcomes_pair: dict[tuple[str, str], dict[str, Any]] = {}
    outcomes_short: dict[tuple[str, str], dict[str, Any]] = {}
    for o in outcome_rows:
        ot = str(o.get("title") or "")
        os_low = str(o.get("sha") or "").lower()
        oq = str(o.get("queued_at") or "")
        _prefer_outcome(outcomes_exact, (ot, os_low, oq), o)
        _prefer_outcome(outcomes_pair, (ot, os_low), o)
        if ot and len(os_low) >= 7:
            _prefer_outcome(outcomes_short, (ot, os_low[:7]), o)

    unreconciled_pairs = recent_unreconciled_receipt_pairs(workspace, outcome_rows)

    skipped_exact: dict[tuple[str, str], dict[str, Any]] = {}
    skipped_short: dict[tuple[str, str], dict[str, Any]] = {}
    for o in outcome_rows:
        if o.get("status") == "skipped":
            ot = str(o.get("title") or "")
            os_low = str(o.get("sha") or "").lower()
            if ot and os_low:
                skipped_exact[(ot, os_low)] = o
                if len(os_low) >= 7:
                    skipped_short[(ot, os_low[:7])] = o

    def _find_skip_outcome(t_name: str, sha_str: str) -> dict[str, Any] | None:
        s_low = sha_str.lower()
        hit = skipped_exact.get((t_name, s_low))
        if hit is not None:
            return hit
        if len(s_low) >= 7:
            return skipped_short.get((t_name, s_low[:7]))
        return None

    def _resolve_queue_outcome(t_name: str, sha_str: str, q_at_str: str, is_forced: bool) -> dict[str, Any] | None:
        s_low = str(sha_str or "").lower()
        exact = outcomes_exact.get((t_name, s_low, q_at_str))
        if exact is not None:
            return exact
        skip_o = _find_skip_outcome(t_name, s_low)
        skip_ts = str((skip_o or {}).get("finished_at") or (skip_o or {}).get("queued_at") or "")
        pair_o = outcomes_pair.get((t_name, s_low)) or (outcomes_short.get((t_name, s_low[:7])) if len(s_low) >= 7 else None)
        if is_forced:
            if pair_o is not None and str(pair_o.get("submission_id") or "").strip():
                out_ts = str(pair_o.get("submitted_at") or pair_o.get("finished_at") or "")
                if skip_o is None or out_ts >= str(q_at_str or "") or pair_o.get("reconciled_from") == "orphan_receipt":
                    return pair_o
            if skip_o is not None and skip_ts >= str(q_at_str or ""):
                return skip_o
            return None
        return pair_o or skip_o

    first_pend: tuple[str, str] | None = None
    for q in queue_rows:
        qt, qs, qq = str(q.get("title", "")), str(q.get("sha", "")), str(q.get("queued_at", ""))
        qs_low = qs.lower()
        if len(qs_low) >= 7 and (qt, qs_low[:7]) in unreconciled_pairs:
            continue
        if _resolve_queue_outcome(qt, qs_low, qq, bool(q.get("force"))) is None:
            first_pend = (qt, qs)
            break

    pipeline = compute_synchronized_pipeline(workspace, outcome_rows, queue_lock_held, gap_sec=gap_sec, first_pending=first_pend)
    cooldown_sec = int(pipeline["cooldown_remaining_sec"])
    eff_closing_reserve = compute_effective_closing_reserve(problems_out, closing_reserve, noise_floor)
    active_slots = [s for s in (pipeline.get("account_slots") or []) if s.get("enabled") and s.get("logged_in")]
    if active_slots:
        team_daily_limit = sum(int(s.get("daily_limit") or daily_limit) for s in active_slots)
        team_used_today = sum(int(s.get("used_today") or 0) for s in active_slots)
        team_remaining = sum(max(0, int(s.get("daily_limit") or daily_limit) - int(s.get("used_today") or 0)) for s in active_slots)
        avail_shots = max(0, team_remaining - eff_closing_reserve)
    else:
        team_daily_limit = daily_limit
        team_used_today = hist_idx["used_24h"]
        team_remaining = max(0, daily_limit - hist_idx["used_24h"])
        avail_shots = max(0, team_remaining - eff_closing_reserve)

    def eval_gates(
        title: str,
        est_sc: float | None,
        is_restore_zero: bool,
        safety_ok: bool = True,
        forced: bool = False,
        sha: str = "",
    ) -> dict[str, Any]:
        p_obj = prob_map.get(title, {})
        cur_sc = float(p_obj.get("current_score", 0.0))
        best_sc = float(p_obj.get("best_score") or p_obj.get("raw_best_score") or 0.0)
        best_sha = str(p_obj.get("best_sha") or "")
        diff = int(p_obj.get("difficulty", 2))
        delta_exp = (est_sc - cur_sc) if est_sc is not None else 3.0
        is_restore_regime = (not is_restore_zero) and cur_sc > 0.0 and bool(best_sha) and ((best_sc - cur_sc) >= 1.5)
        is_restoring_best = bool(best_sha and sha and sha.lower().startswith(best_sha.lower()[:7]))
        if is_restore_regime and not is_restoring_best and not forced:
            noise_ok = (est_sc is not None) and ((est_sc - best_sc) >= noise_floor)
        else:
            noise_ok = is_restore_zero or forced or cur_sc == 0.0 or (delta_exp >= noise_floor)
        quota_ok = is_restore_zero or (avail_shots > 0)
        circuit_ok = is_restore_zero or forced or not bool(p_obj.get("circuit_open", False))
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
        (str(q.get("title", "")), str(q.get("sha", "")).lower())
        for q in queue_rows
        if (str(q.get("title", "")), str(q.get("sha", "")).lower(), str(q.get("queued_at", ""))) not in outcomes_exact
    }

    for p_obj in problems_out:
        t = p_obj["title"]
        if p_obj["current_score"] == 0.0 and p_obj["best_score"] > 0.0 and p_obj["best_sha"]:
            b_sha = p_obj["best_sha"]
            if (
                (t, b_sha.lower()) not in queued_pairs
                and _find_skip_outcome(t, b_sha) is None
                and (t, b_sha[:7].lower()) not in unreconciled_pairs
            ):
                priors = sha_pass_scores.get((t, b_sha), [p_obj["best_score"]])
                est = round(sum(priors) / len(priors), 2)
                gates = eval_gates(t, est, is_restore_zero=True)
                advisory_items.append(
                    {
                        "id": f"live-{t}-{b_sha[:7].lower()}",
                        "kind": "advisory",
                        "title": t,
                        "difficulty": p_obj["difficulty"],
                        "sha": b_sha,
                        "short_sha": b_sha[:7],
                        "note": f"Board is 0 ({p_obj['status']}); validated SHA scored {p_obj['best_score']:.2f}",
                        "detail": "",
                        "submission_id": None,
                        "queued_at": str(p_obj.get("raw_best_at") or ""),
                        "finished_at": None,
                        "badge": "CRITICAL RESTORE",
                        "raw_status": "advisory_restore",
                        "current_score": 0.0,
                        "prior_score": 0.0,
                        "estimated_score": est,
                        "actual_score": None,
                        "delta_score": None,
                        "breakthrough_delta": None,
                        "is_breakthrough": False,
                        "experience_id": exp_map.get((t, b_sha[:7])),
                        "baseline_pass_sha": None,
                        "wspt_priority": gates["wspt_score"],
                        "gates": gates,
                        "can_approve": True,
                        "can_skip": False,
                        "can_restore": True,
                    }
                )

    from ..config import workspace_state_dir

    # Compute per-operator ESCAPE_LOCK state (prefer calibration.json single source of truth; fallback to outcome scan)
    verified_escapes: dict[str, Any] = {}
    for ve_cand in (
        results_dir / "verified-escapes.json",
        results_dir / "escape-verifications.json",
        workspace_state_dir(workspace) / "verified_escapes.json",
    ):
        if ve_cand.is_file():
            ve_doc = read_json(ve_cand, {})
            if isinstance(ve_doc, dict):
                sub = ve_doc.get("verified") if isinstance(ve_doc.get("verified"), dict) else ve_doc
                verified_escapes.update(sub)
    escape_lock_info: dict[str, tuple[str, str]] = {}
    for t_name in prob_map:
        cal_p = results_dir / t_name / "calibration.json"
        if cal_p.is_file():
            cd = read_json(cal_p, {})
            if cd.get("escape_lock"):
                escape_lock_info[t_name] = (str(cd.get("escape_sha") or "")[:7], str(cd.get("escape_status") or "Escape"))
            else:
                # Explicitly unlocked (e.g. verified via `opsb bench verify-escape`)
                continue
        else:
            outcomes_by_title_rows = [
                o for o in outcome_rows
                if o.get("title") == t_name and str(o.get("status", "")) not in ("rejected", "skipped", "duplicate")
            ]
            prior_passes: list[set[str]] = []
            acc_p: set[str] = set()
            for r in outcomes_by_title_rows:
                prior_passes.append(set(acc_p))
                r_sha = str(r.get("sha") or "")
                if r.get("status") == "Pass" and r_sha and isinstance(r.get("score"), (int, float)) and r["score"] > 0:
                    acc_p.add(r_sha[:7])
            for idx_r in range(len(outcomes_by_title_rows) - 1, -1, -1):
                r = outcomes_by_title_rows[idx_r]
                st_r = str(r.get("status", ""))
                r_sha = str(r.get("sha") or "")
                n_low = str(r.get("note") or "").strip().lower()
                is_rest_r = (r_sha[:7] in prior_passes[idx_r]) or any(
                    n_low.startswith(pfx) for pfx in ("restore", "rollback", "board is 0", "closing restore")
                )
                if st_r == "Pass":
                    if is_rest_r:
                        continue
                    break
                if st_r in ("Wrong Answer", "Time Limit Exceeded", "Runtime Error"):
                    if not (verified_escapes.get(f"{t_name}:{r_sha}") or verified_escapes.get(f"{t_name}:{r_sha[:7]}")):
                        escape_lock_info[t_name] = (r_sha[:7], st_r)
                    break

    prior_board: dict[str, float] = {}
    last_pass_score: dict[str, float] = {}
    last_pass_sha: dict[str, str] = {}
    peak_pass_score: dict[str, float] = {}
    seen_pass_shas: set[tuple[str, str]] = set()
    chron_items: list[dict[str, Any]] = []
    completed_attempts: list[tuple] = []
    inflight_q = build_inflight_queue_items(pipeline, prob_map, queue_rows, exp_map, now.isoformat())
    inflight_emitted = {
        (str(it.get("title") or ""), str(it.get("short_sha") or "").lower()[:7])
        for it in inflight_q
        if it.get("title") and it.get("short_sha")
    }
    inflight_set = set(pipeline.get("inflight_keys") or [])
    inflight_lower = {
        (k.split("|", 1)[0], k.split("|", 1)[1].lower()[:7])
        for k in inflight_set
        if "|" in str(k)
    } | inflight_emitted

    def _append_completed_item(item_id: str, t: str, sha: str, q_at: str, note: str, est: float | None, out: dict[str, Any]) -> None:
        p_obj = prob_map.get(t, {"difficulty": DEFAULT_DIFFICULTIES.get(t, 2), "current_score": 0.0, "best_score": 0.0})
        cur_sc = float(p_obj.get("current_score", 0.0))
        prior_sc = prior_board.get(t, 0.0)
        st = out.get("status", "done")
        raw_detail = str(out.get("detail") or "")
        if st == "rejected" and (raw_detail.lower().startswith("poll error:") or bool(out.get("submission_id"))):
            st = "sync_error"
        if st == "rejected" and "不合规内容" in raw_detail:
            st = "blocked_compliance"
        actual = float(out["score"]) if out.get("score") is not None else None
        delta = round(actual - prior_sc, 2) if (actual is not None and prior_sc > 0) else (round(actual, 2) if actual is not None else None)
        bt_delta: float | None = None
        base_p_sha = last_pass_sha.get(t)
        effective_note = str(out.get("note") or note or "")
        low_note = effective_note.strip().lower()
        is_restore_note = any(low_note.startswith(pfx) for pfx in ("restore", "rollback", "retry restore", "closing restore"))
        if st == "Pass":
            badge = "PASS ↑" if (delta is None or delta >= 0) else "PASS ↓"
            if actual is not None:
                peak_p = peak_pass_score.get(t, 0.0)
                is_repeat = (t, sha[:7]) in seen_pass_shas
                if (not is_repeat) and (not is_restore_note) and (peak_p > 0) and (actual > peak_p):
                    bt_delta = round(actual - peak_p, 2)
                if sha:
                    seen_pass_shas.add((t, sha[:7]))
                last_pass_score[t] = actual
                last_pass_sha[t] = sha[:7]
                peak_pass_score[t] = max(peak_p, actual)
                prior_board[t] = actual
        elif st == "skipped":
            badge = "SKIPPED"
        elif st == "sync_error":
            badge = "SYNC ERROR"
        elif st in ("Running", "Queued", "Pending"):
            badge = "JUDGING"
        else:
            badge = "REJECTED"
            if st in ("Wrong Answer", "Time Limit Exceeded", "Runtime Error", "Compile Error"):
                prior_board[t] = 0.0
        chron_items.append(
            {
                "id": item_id,
                "kind": "held" if st in ("rejected", "blocked_compliance") else "completed",
                "title": t,
                "difficulty": int(p_obj.get("difficulty", 2)),
                "sha": sha,
                "short_sha": sha[:7],
                "note": raw_detail if st in ("blocked_compliance", "rejected") else effective_note,
                "detail": out.get("detail", ""),
                "submission_id": out.get("submission_id"),
                "queued_at": q_at,
                "finished_at": out.get("finished_at") or out.get("submitted_at"),
                "badge": "HOLD: COMPLIANCE" if st == "blocked_compliance" else badge,
                "raw_status": st,
                "current_score": cur_sc,
                "prior_score": round(prior_sc, 2),
                "estimated_score": est,
                "actual_score": round(actual, 2) if actual is not None else None,
                "delta_score": delta,
                "breakthrough_delta": bt_delta,
                "is_breakthrough": bool(bt_delta is not None and bt_delta >= bt_threshold),
                "experience_id": exp_map.get((t, sha[:7])),
                "baseline_pass_sha": base_p_sha,
                "wspt_priority": round(int(p_obj.get("difficulty", 2)) * max(0.0, (est or 0) - prior_sc), 2),
                "gates": {
                    "safety_ok": st not in ("rejected", "Compile Error", "Runtime Error", "Wrong Answer"),
                    "noise_ok": st != "skipped",
                    "circuit_ok": True,
                    "quota_ok": True,
                    "all_pass": st == "Pass",
                    "delta_expected": round((est or 0) - prior_sc, 2),
                    "wspt_score": round(int(p_obj.get("difficulty", 2)) * max(0.0, (est or 0) - prior_sc), 2),
                },
                "can_approve": False,
                "can_skip": False,
                "can_restore": False,
            }
        )

    for idx, q in enumerate(queue_rows):
        t, sha, q_at = q.get("title", ""), q.get("sha", ""), q.get("queued_at", "")
        note = q.get("note", "")
        forced = bool(q.get("force", False))
        p_obj = prob_map.get(t, {"difficulty": DEFAULT_DIFFICULTIES.get(t, 2), "current_score": 0.0, "best_score": 0.0})

        sha_low = str(sha or "").lower()
        out = _resolve_queue_outcome(t, sha_low, str(q_at or ""), forced)
        prior_sc = prior_board.get(t, 0.0)
        cur_sc = float(p_obj.get("current_score", 0.0))

        priors = sha_pass_scores.get((t, sha), [])
        saved_est = (out or {}).get("estimated_score") if out else q.get("estimated_score")
        if saved_est is not None:
            est = round(float(saved_est), 2)
        else:
            base_anchor = prior_sc if (out is not None and prior_sc > 0) else (cur_sc or prior_sc)
            est = estimate_score_from_note(note, base_anchor, float(p_obj.get("best_score", 0.0)))
            if est is None:
                est = round(sum(priors) / len(priors), 2) if priors else None

        if out is None:
            # Skip putting in waiting list if this (title, sha) is ALREADY inflight/running OR has an unreconciled receipt!
            s7_low = sha_low[:7]
            if (
                f"{t}|{sha[:7]}" in inflight_set
                or (bool(s7_low) and (t, s7_low) in inflight_lower)
                or (len(s7_low) >= 7 and (t, s7_low) in unreconciled_pairs)
                or (
                    bool(s7_low)
                    and (t, s7_low) in inflight_emitted
                    and pipeline["is_busy"]
                    and pipeline.get("active_title") == t
                    and str(pipeline.get("active_sha") or "").lower().startswith(s7_low)
                )
            ):
                continue
            is_known_pass = bool(sha_pass_scores.get((t, sha))) or (t, sha[:7]) in seen_pass_shas
            is_rest = is_known_pass and (cur_sc == 0.0 or "restore" in note.lower())
            esc_hit = escape_lock_info.get(t)
            esc_blocked = bool(esc_hit and not is_known_pass)
            gates = eval_gates(t, est, is_restore_zero=is_rest, safety_ok=not esc_blocked, forced=forced, sha=sha)
            if esc_blocked:
                badge = "ESCAPE LOCK"
                esc_s7, esc_st = esc_hit  # type: ignore[misc]
                esc_detail = f"ESCAPE_LOCK active ({esc_s7} {esc_st}); add reproducer & verify-escape before submitting new SHA"
            elif is_rest and cur_sc == 0.0:
                badge = "CRITICAL RESTORE"
                esc_detail = ""
            elif not gates["quota_ok"]:
                badge = "QUOTA EXHAUSTED" if team_remaining <= 0 else "HOLD: RESERVE"
                esc_detail = ""
            elif not gates["circuit_ok"] and not forced:
                badge = "CIRCUIT OPEN"
                esc_detail = ""
            elif not gates["noise_ok"] and not forced:
                badge = "HOLD: NOISE"
                esc_detail = ""
            else:
                badge = "READY"
                esc_detail = ""
            cand_bt_delta = (
                round((est or cur_sc) - cur_sc, 2)
                if (est is not None and cur_sc > 0 and not is_rest and not esc_blocked and not is_known_pass and "restore" not in note.lower())
                else None
            )
            chron_items.append(
                {
                    "id": f"live-{t}-{sha[:7].lower()}" if sha[:7] else f"queued-{idx}-{t}",
                    "kind": "pending",
                    "title": t,
                    "difficulty": int(p_obj.get("difficulty", 2)),
                    "sha": sha,
                    "short_sha": sha[:7],
                    "note": note,
                    "detail": esc_detail,
                    "submission_id": None,
                    "queued_at": q_at,
                    "finished_at": None,
                    "badge": badge,
                    "raw_status": "queued",
                    "current_score": cur_sc,
                    "prior_score": cur_sc,
                    "estimated_score": est,
                    "actual_score": None,
                    "delta_score": None,
                    "breakthrough_delta": cand_bt_delta,
                    "is_breakthrough": bool(cand_bt_delta is not None and cand_bt_delta >= bt_threshold),
                    "experience_id": exp_map.get((t, sha[:7])),
                    "baseline_pass_sha": last_pass_sha.get(t),
                    "forced": forced,
                    "wspt_priority": 0.0 if esc_blocked else gates["wspt_score"],
                    "gates": gates,
                    "can_approve": badge in ("HOLD: NOISE", "CIRCUIT OPEN"),
                    "can_skip": True,
                    "can_restore": False,
                }
            )
        else:
            if out.get("submission_id"):
                continue  # Project the journal once, independently of queue membership.
            if out.get("status") in ("rejected", "blocked_compliance"):
                pair = outcomes_pair.get((t, sha_low))
                if pair and pair.get("submission_id"):
                    continue
            completed_attempts.append((f"done-{idx}-{t}-{sha[:7]}", t, sha, q_at, note, saved_est, out))

    queue_by_claim = {(q.get("title"), q.get("sha"), q.get("queued_at")): q for q in queue_rows}
    for out in outcome_rows:
        sid = out.get("submission_id")
        if not sid or out.get("status") == "duplicate":
            continue
        t, sha = out.get("title", ""), out.get("sha") or ""
        q = queue_by_claim.get((t, sha, out.get("queued_at")), {})
        at = out.get("submitted_at") or out.get("queued_at") or out.get("finished_at") or ""
        completed_attempts.append((f"judge-{sid}", t, sha, at, q.get("note", ""),
                                   out.get("estimated_score", q.get("estimated_score")), out))

    for attempt in sorted(completed_attempts, key=lambda a: a[6].get("submitted_at") or a[6].get("finished_at") or a[3]):
        _append_completed_item(*attempt)

    raw_active = [x for x in (advisory_items + chron_items) if x["kind"] != "completed"]
    raw_active.sort(
        key=lambda x: (
            0 if x["badge"] == "CRITICAL RESTORE" else 1,
            -float(x.get("wspt_priority") or 0.0),
            str(x.get("title") or ""),
            str(x.get("short_sha") or "").lower(),
        )
    )
    # Deduplicate waiting items by (title, short_sha)
    seen_active: set[tuple[str, str]] = set()
    active_q: list[dict[str, Any]] = []
    for item in raw_active:
        k = (item["title"], str(item["short_sha"]).lower())
        if k not in seen_active:
            seen_active.add(k)
            active_q.append(item)

    done_q = [x for x in chron_items if x["kind"] == "completed"]
    done_q.sort(
        key=lambda x: (
            str(x.get("finished_at") or x.get("queued_at") or ""),
            str(x.get("title") or ""),
            str(x.get("short_sha") or "").lower(),
            str(x.get("submission_id") or ""),
        ),
        reverse=True,
    )

    return {
        "sampled_at": now.isoformat(),
        "contest": contest_summary,
        "scheduler": {
            "auto_consumer_enabled": bool(sched_cfg.get("auto_consumer_enabled", False)),
            "rate_limit_gap_sec": gap_sec,
            "cooldown_remaining_sec": cooldown_sec,
            "cooldown_ends_epoch": pipeline["cooldown_ends_epoch"],
            "rate_ready": cooldown_sec == 0,
            "last_submitted_at": hist_idx["last_submitted_dt"].isoformat() if hist_idx["last_submitted_dt"] else None,
            "daily_quota_limit": team_daily_limit,
            "used_today_24h": team_used_today,
            "closing_reserve": eff_closing_reserve,
            "configured_closing_reserve": closing_reserve,
            "available_normal_shots": avail_shots,
            "noise_floor_delta": noise_floor,
            "breakthrough_threshold": bt_threshold,
            "breakthrough_count": sum(1 for x in done_q if x.get("is_breakthrough")),
            "circuit_breaker_limit": cb_limit,
            "queue_lock_held": queue_lock_held,
            "actionable_count": len(active_q),
            "skipped_keys": sorted(f"{st_t}|{st_s}" for st_t, st_s in skipped_short),
            "pipeline": pipeline,
        },
        "problems": problems_out,
        "submission_queue": inflight_q + active_q + done_q,
    }


def check_commit_safety_gate(workspace: Path, title: str, sha: str) -> bool:
    res_json = workspace / "results" / title / sha / "result.json"
    val_log = workspace / "results" / title / sha / "validation.log"
    if not res_json.is_file() or not val_log.is_file():
        return False
    r_data = read_json(res_json, {})
    if r_data.get("exit_code") != 0:
        return False
    if str(SUPERVISOR_ROOT) not in sys.path:
        sys.path.insert(0,str(SUPERVISOR_ROOT))
    from scheduler.case_bench import formal_evidence, has_reference
    problems = resolve_workspace_meta(workspace)["problems"]
    repo = workspace / next((p["path"] for p in problems if p["title"]==title),f"bench/{title}")
    if r_data.get("scheme")=="reference" or has_reference(repo,sha):
        return bool(formal_evidence(res_json.parent,repo,r_data))
    txt = val_log.read_text(encoding="utf-8", errors="replace")
    if "EXTENDED_FAILED" in txt:
        return False
    if "EXTENDED_PASSED" in txt or "EXTENDED_RC=0" in txt:
        return True
    # Official-template repos ship no validation/extended.py, so EXTENDED_PASSED can never
    # appear there. Their equivalent evidence is the template's own verifier: exit 0 plus
    # "verified": true in result.json.
    if bool(r_data.get("verified")) and str(r_data.get("layout") or "") == "template":
        return True
    return "VERIFY OK" in txt or "verify: OK" in txt
