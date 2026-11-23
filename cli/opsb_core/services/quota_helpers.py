"""Shared quota/reserve unification & inflight queue item helpers (V3-002, V3-010, V3-012)."""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from ..config import DEFAULT_DIFFICULTIES, read_json
from .auth_service import list_judge_accounts
from .judge_sync import _classify_remote_phase


def compute_effective_closing_reserve(
    problems_out: list[dict[str, Any]],
    min_reserve: int,
    noise_floor: float,
) -> int:
    """Compute dynamic closing reserve matching `scheduler/gate.py` formula."""
    targets = 0
    for p in problems_out:
        b_sha = str(p.get("best_sha") or "")
        if not b_sha:
            continue
        cur = float(p.get("current_score") or 0.0)
        best = float(p.get("best_score") or 0.0)
        act = str(p.get("active_sha") or "")
        if cur > 0.0 and act and b_sha[:7] == act[:7]:
            continue
        if cur <= 0.0 or (best - cur) >= noise_floor:
            targets += 1
    cap = max(12, int(min_reserve))
    return min(cap, max(int(min_reserve), targets + (1 if targets else 0)))


def build_inflight_queue_items(
    pipeline: dict[str, Any],
    prob_map: dict[str, dict[str, Any]],
    queue_rows: list[dict[str, Any]],
    exp_map: dict[tuple[str, str], str],
    now_iso: str,
) -> list[dict[str, Any]]:
    """Convert `pipeline.active_submissions` into `kind='inflight'` queue items for `opsb queue list`."""
    out: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    q_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for q in queue_rows:
        qt, qs = str(q.get("title") or ""), str(q.get("sha") or "")
        if qt and qs:
            q_lookup[(qt, qs[:7])] = q

    for act in pipeline.get("active_submissions") or []:
        t = str(act.get("title") or "").strip()
        sha = str(act.get("sha") or "").strip()
        s7 = str(act.get("short_sha") or sha[:7]).strip()
        if not t:
            continue
        key = (t, s7)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)

        p_obj = prob_map.get(t, {})
        cur_sc = float(p_obj.get("current_score") or 0.0)
        diff = int(p_obj.get("difficulty") or DEFAULT_DIFFICULTIES.get(t, 2))
        q_rec = q_lookup.get(key) or {}
        est_raw = q_rec.get("estimated_score")
        est_sc = round(float(est_raw), 2) if isinstance(est_raw, (int, float)) else None
        phase = str(act.get("phase") or "judging")
        r_status = str(act.get("remote_status") or phase)
        acc_label = str(act.get("account_label") or act.get("account_id") or "主账号")
        jid = act.get("remote_judge_id")
        detail = f"{acc_label}" + (f" · #{jid}" if jid else "")
        started_ep = act.get("started_epoch")
        queued_at = (
            _dt.datetime.fromtimestamp(float(started_ep), tz=_dt.timezone.utc).isoformat()
            if isinstance(started_ep, (int, float)) and started_ep > 0
            else str(q_rec.get("queued_at") or now_iso)
        )
        out.append(
            {
                "id": f"live-{t}-{s7.lower()}" if s7 else str(act.get("id") or f"live-{t}"),
                "kind": "inflight",
                "title": t,
                "difficulty": diff,
                "sha": sha or s7,
                "short_sha": s7,
                "note": str(q_rec.get("note") or f"{acc_label} · {r_status}"),
                "detail": detail,
                "submission_id": act.get("remote_submission_id"),
                "queued_at": queued_at,
                "finished_at": None,
                "badge": "JUDGING",
                "raw_status": r_status,
                "phase": phase,
                "account_id": act.get("account_id"),
                "account_label": acc_label,
                "remote_judge_id": jid,
                "elapsed_sec": int(act.get("elapsed_sec") or 0),
                "current_score": cur_sc,
                "prior_score": cur_sc,
                "estimated_score": est_sc,
                "actual_score": None,
                "delta_score": None,
                "breakthrough_delta": None,
                "is_breakthrough": False,
                "experience_id": exp_map.get((t, s7)),
                "baseline_pass_sha": None,
                "wspt_priority": 9999.0,
                "gates": {
                    "safety_ok": True,
                    "noise_ok": True,
                    "circuit_ok": True,
                    "quota_ok": True,
                    "all_pass": True,
                    "delta_expected": round((est_sc - cur_sc), 2) if est_sc is not None else 0.0,
                    "wspt_score": 0.0,
                },
                "can_approve": False,
                "can_skip": False,
                "can_restore": False,
            }
        )
    return out


def active_eligible_account_count() -> int:
    try:
        accounts = list_judge_accounts()
        eligible = [a for a in accounts if a.get("enabled", True) and a.get("logged_in")]
        return max(1, len(eligible))
    except Exception:
        return 1
