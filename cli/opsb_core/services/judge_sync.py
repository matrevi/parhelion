"""3-Layer Clock & 10-Stage Submission Lifecycle Synchronizer (Zero Fallback Ambiguity).

Explicit non-terminal pipeline phases:
  - idle             : Queue lock free, no in-flight remote submission
  - waiting_cooldown : Local worker holds lock, waiting out 190s platform rate window before POST
  - packaging        : Local worker checking out git worktree & verifying read-only templates
  - uploading        : Local worker sending POST /api/submissions/submit to Judge
  - remote_queued    : Receipt created; Judge status is Pending / Waiting / Queued
  - remote_compiling : Receipt created; Judge status is Compiling / Building
  - remote_running   : Receipt created; Judge status is Running / Judging / Testing
  - fetching_score   : Judge reached terminal verdict, local consumer fetching ranking score before writing outcome
"""
from __future__ import annotations

import email.utils
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from ..config import (
    CONFIG_DIR,
    parse_iso,
    read_json,
    read_ttl_cache,
    resolve_python_exe,
    utc_now,
    write_ttl_cache,
)

INFLIGHT_FILE = CONFIG_DIR / "inflight_submit.json"

TERMINAL_JUDGE_STATUSES = {
    "Pass",
    "Accepted",
    "Fail",
    "Wrong Answer",
    "Compile Error",
    "Runtime Error",
    "Time Limit Exceeded",
    "Memory Limit Exceeded",
    "System Error",
}

TERMINAL_OUTCOME_STATUSES = TERMINAL_JUDGE_STATUSES | {"rejected", "skipped", "duplicate"}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _scan_all_receipts(workspace: Path) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    results_dir = workspace / "results"
    if not results_dir.is_dir():
        return receipts
    for r_path in results_dir.glob("*/*/submission-receipt.json"):
        data = read_json(r_path)
        if isinstance(data, dict) and data.get("submission_id") and data.get("submitted_at"):
            dt = parse_iso(data["submitted_at"])
            if dt:
                receipts.append(
                    {
                        "title": r_path.parents[1].name,
                        "sha": data.get("validated_commit") or r_path.parent.name,
                        "submission_id": str(data["submission_id"]),
                        "account_id": str(data.get("account_id") or "primary"),
                        "user_id": str(data.get("user_id") or ""),
                        "submitted_dt": dt,
                    }
                )
    receipts.sort(key=lambda x: x["submitted_dt"], reverse=True)
    return receipts


def _query_remote_submission_cached(workspace: Path, submission_id: str, session_file: str = "") -> dict[str, Any] | None:
    cache_key = f"remote_judge_{submission_id[:12]}"
    cached = read_ttl_cache(cache_key, ttl_sec=3.0, fingerprint=submission_id)
    if isinstance(cached, dict):
        return cached

    py_exe = resolve_python_exe(workspace)
    env = dict(os.environ)
    if session_file:
        env["JUDGE_SESSION_FILE"] = session_file
    probe = (
        "import datetime, email.utils, json, os, pathlib, sys\n"
        f"root = pathlib.Path({str(workspace)!r})\n"
        "sys.path.insert(0, str(root / 'skills/problem-submit/scripts'))\n"
        "from ict_cli import ICTClient\n"
        "c = ICTClient(session_file=os.environ.get('JUDGE_SESSION_FILE') or None)\n"
        "if not c.load_session():\n"
        "    print('{}'); sys.exit(0)\n"
        f"resp = c.session.get(c.BASE_URL + '/api/submissions/' + {submission_id!r}, timeout=6)\n"
        "offset = 0.0\n"
        "if resp.headers.get('Date'):\n"
        "    srv_dt = email.utils.parsedate_to_datetime(resp.headers['Date'])\n"
        "    loc_dt = datetime.datetime.now(datetime.timezone.utc)\n"
        "    offset = round((srv_dt - loc_dt).total_seconds(), 2)\n"
        "d = resp.json()\n"
        "sub = d.get('data') if isinstance(d.get('data'), dict) else d\n"
        "res = sub.get('result') or []\n"
        "vis_pass = sum(1 for x in res if isinstance(x, dict) and x.get('testcase_status') in ('Pass', 'Accepted'))\n"
        "print(json.dumps({\n"
        "    'ok': True,\n"
        "    'id': sub.get('_id'),\n"
        "    'judge_id': sub.get('ID'),\n"
        "    'status': sub.get('status'),\n"
        "    'create_time': sub.get('create_time'),\n"
        "    'vis_pass': vis_pass,\n"
        "    'total_cases': len(res),\n"
        "    'server_offset_sec': offset\n"
        "}))\n"
    )
    try:
        proc = subprocess.run([py_exe, "-c", probe], stdin=subprocess.DEVNULL, env=env, capture_output=True, text=True, timeout=7.5)
        if proc.returncode == 0 and proc.stdout.strip():
            parsed = json.loads(proc.stdout.strip().splitlines()[-1])
            if parsed.get("ok"):
                write_ttl_cache(cache_key, parsed, fingerprint=submission_id)
                return parsed
    except Exception:
        pass
    return None


def _classify_remote_phase(remote_status: str) -> str:
    low = remote_status.strip().lower()
    if low in ("pending", "waiting", "queued", "created", "init"):
        return "remote_queued"
    if "compil" in low or "build" in low:
        return "remote_compiling"
    return "remote_running"


def _inspect_local_worker(queue_lock_held: bool) -> dict[str, Any] | None:
    if INFLIGHT_FILE.is_file():
        d = read_json(INFLIGHT_FILE, {})
        pid = int(d.get("pid") or 0)
        if pid and _pid_alive(pid):
            st_dt = parse_iso(d.get("started_at"))
            return {
                "title": d.get("title") or "",
                "sha": d.get("sha") or "",
                "phase": d.get("phase") or "packaging",
                "started_epoch": st_dt.timestamp() if st_dt else utc_now().timestamp(),
            }
        INFLIGHT_FILE.unlink(missing_ok=True)

    if not queue_lock_held:
        return None

    try:
        out = subprocess.check_output(["ps", "-eo", "etime,args"], text=True, stderr=subprocess.DEVNULL, timeout=1.0)
        for line in out.splitlines():
            if "submit-validated.py" in line and "grep" not in line:
                parts = line.strip().split(None, 1)
                cmd = parts[1] if len(parts) > 1 else ""
                m_t = re.search(r"submit-validated\.py\s+([A-Za-z0-9_]+)", cmd)
                m_s = re.search(r"--sha\s+([0-9a-f]{7,40})", cmd)
                if m_t:
                    return {
                        "title": m_t.group(1),
                        "sha": m_s.group(1) if m_s else "",
                        "phase": "uploading",
                        "started_epoch": utc_now().timestamp(),
                    }
    except Exception:
        pass

    return {"title": "", "sha": "", "phase": "consumer_locked", "started_epoch": utc_now().timestamp()}


def compute_synchronized_pipeline(
    workspace: Path,
    outcome_rows: list[dict[str, Any]],
    queue_lock_held: bool,
    gap_sec: int = 190,
    first_pending: tuple[str, str] | None = None,
) -> dict[str, Any]:
    now_epoch = utc_now().timestamp()
    receipts = _scan_all_receipts(workspace)

    # 1. Ground-truth latest POST /submit timestamp across BOTH receipts & outcomes
    latest_sub_epoch = 0.0
    for r in receipts:
        latest_sub_epoch = max(latest_sub_epoch, r["submitted_dt"].timestamp())
    for o in outcome_rows:
        if o.get("status") not in ("rejected", "skipped", "duplicate") and o.get("submission_id"):
            dt = parse_iso(o.get("submitted_at") or o.get("finished_at"))
            if dt:
                latest_sub_epoch = max(latest_sub_epoch, dt.timestamp())

    # Multi-teammate submitter slots: each enabled logged-in account obeys its OWN 190s cooldown!
    account_slots: list[dict[str, Any]] = []
    try:
        from .submit_runner import compute_account_slots

        account_slots = compute_account_slots(workspace, outcome_rows=outcome_rows, gap_sec=gap_sec)
    except Exception:
        account_slots = []
    slot_by_id = {str(s["id"]): s for s in account_slots}
    slot_by_uid = {str(s.get("user_id")): s for s in account_slots if s.get("user_id")}

    # 2. Check ALL recent receipts (< 35 min) not yet written to outcome_rows (supports N concurrent remote runs!)
    outcome_sids = {str(o.get("submission_id")) for o in outcome_rows if o.get("submission_id") and o.get("status") in TERMINAL_OUTCOME_STATUSES}
    active_remotes: list[dict[str, Any]] = []
    finishing_score_sync: dict[str, Any] | None = None
    server_offset_sec = 0.0

    for r in receipts[:max(10, len(account_slots) * 4)]:
        age_sec = now_epoch - r["submitted_dt"].timestamp()
        if age_sec > 2100:
            break
        if r["submission_id"] not in outcome_sids:
            r_slot = slot_by_id.get(r.get("account_id") or "") or slot_by_uid.get(r.get("user_id") or "") or slot_by_id.get("primary") or {}
            r_sf = str(r_slot.get("session_file") or "")
            r_aid = str(r_slot.get("id") or r.get("account_id") or "primary")
            r_lbl = str(r_slot.get("label") or ("主账号" if r_aid == "primary" else r_aid))
            remote_info = _query_remote_submission_cached(workspace, r["submission_id"], session_file=r_sf)
            if remote_info:
                server_offset_sec = float(remote_info.get("server_offset_sec") or 0.0)
                srv_create_dt = parse_iso(remote_info.get("create_time"))
                adj_create_epoch = (srv_create_dt.timestamp() - server_offset_sec) if srv_create_dt else r["submitted_dt"].timestamp()
                latest_sub_epoch = max(latest_sub_epoch, adj_create_epoch)

                r_status = str(remote_info.get("status") or "Running")
                if r_status not in TERMINAL_JUDGE_STATUSES:
                    active_remotes.append(
                        {
                            "id": f"remote-{r['submission_id']}",
                            "title": r["title"],
                            "sha": r["sha"],
                            "short_sha": r["sha"][:7],
                            "submission_id": r["submission_id"],
                            "judge_id": remote_info.get("judge_id"),
                            "remote_status": r_status,
                            "phase": _classify_remote_phase(r_status),
                            "started_epoch": round(adj_create_epoch, 2),
                            "elapsed_sec": max(0, int(now_epoch - adj_create_epoch)),
                            "account_id": r_aid,
                            "account_label": r_lbl,
                            "vis_pass": remote_info.get("vis_pass", 0),
                        }
                    )
                elif age_sec < 300 and finishing_score_sync is None:
                    finishing_score_sync = {
                        "id": f"sync-{r['submission_id']}",
                        "title": r["title"],
                        "sha": r["sha"],
                        "short_sha": r["sha"][:7],
                        "submission_id": r["submission_id"],
                        "judge_id": remote_info.get("judge_id"),
                        "remote_status": r_status,
                        "phase": "fetching_score",
                        "started_epoch": round(adj_create_epoch, 2),
                        "elapsed_sec": max(0, int(now_epoch - adj_create_epoch)),
                        "account_id": r_aid,
                        "account_label": r_lbl,
                    }

    active_remote = active_remotes[0] if active_remotes else None

    cooldown_ends_epoch = (latest_sub_epoch + gap_sec) if latest_sub_epoch > 0 else 0.0
    cooldown_rem = max(0, int(round(cooldown_ends_epoch - now_epoch)))

    eligible_slots = [s for s in account_slots if s.get("enabled") and s.get("logged_in")]
    active_quota_slots = [s for s in eligible_slots if not s.get("quota_exhausted")]
    if active_quota_slots:
        # Effective submitter cooldown = minimum cooldown across enabled, logged-in, NON-EXHAUSTED accounts
        avail_slots = [s for s in active_quota_slots if s.get("phase") not in ("packaging", "uploading")] or active_quota_slots
        best_slot = min(avail_slots, key=lambda x: int(x.get("cooldown_remaining_sec") or 0))
        cooldown_rem = int(best_slot.get("cooldown_remaining_sec") or 0)
        cooldown_ends_epoch = float(best_slot.get("cooldown_ends_epoch") or 0.0)
    elif eligible_slots:
        # Every enabled account has exhausted its 50/50 daily quota
        cooldown_rem = 0
        cooldown_ends_epoch = 0.0

    local_worker = _inspect_local_worker(queue_lock_held)
    active_submissions: list[dict[str, Any]] = []
    seen_active_pairs: set[tuple[str, str, str]] = set()
    # Include all local workers across ALL teammate slots first
    for s in account_slots:
        if s.get("phase") in ("waiting_cooldown", "packaging", "uploading") and s.get("active_title"):
            a_sha = str(s.get("active_sha") or "")
            k = (str(s["active_title"]), a_sha[:7], str(s["id"]))
            if k not in seen_active_pairs:
                seen_active_pairs.add(k)
                st_ep = float(local_worker["started_epoch"]) if (local_worker and s["id"] == "primary") else now_epoch
                active_submissions.append(
                    {
                        "id": f"local-{s['id']}-{s['active_title']}-{a_sha[:7]}",
                        "title": str(s["active_title"]),
                        "sha": a_sha,
                        "short_sha": a_sha[:7],
                        "phase": str(s["phase"]),
                        "account_id": str(s["id"]),
                        "account_label": str(s["label"]),
                        "remote_submission_id": None,
                        "remote_judge_id": None,
                        "remote_status": None,
                        "started_epoch": round(st_ep, 2),
                        "elapsed_sec": max(0, int(now_epoch - st_ep)),
                    }
                )
    for ar in active_remotes:
        k = (ar["title"], ar["short_sha"], ar["account_id"])
        if k not in seen_active_pairs:
            seen_active_pairs.add(k)
            active_submissions.append(
                {
                    "id": ar["id"],
                    "title": ar["title"],
                    "sha": ar["sha"],
                    "short_sha": ar["short_sha"],
                    "phase": ar["phase"],
                    "account_id": ar["account_id"],
                    "account_label": ar["account_label"],
                    "remote_submission_id": ar["submission_id"],
                    "remote_judge_id": ar["judge_id"],
                    "remote_status": ar["remote_status"],
                    "started_epoch": ar["started_epoch"],
                    "elapsed_sec": ar["elapsed_sec"],
                }
            )
    if finishing_score_sync is not None:
        k = (finishing_score_sync["title"], finishing_score_sync["short_sha"], finishing_score_sync["account_id"])
        if k not in seen_active_pairs:
            active_submissions.append(
                {
                    "id": finishing_score_sync["id"],
                    "title": finishing_score_sync["title"],
                    "sha": finishing_score_sync["sha"],
                    "short_sha": finishing_score_sync["short_sha"],
                    "phase": "fetching_score",
                    "account_id": finishing_score_sync["account_id"],
                    "account_label": finishing_score_sync["account_label"],
                    "remote_submission_id": finishing_score_sync["submission_id"],
                    "remote_judge_id": finishing_score_sync["judge_id"],
                    "remote_status": finishing_score_sync["remote_status"],
                    "started_epoch": finishing_score_sync["started_epoch"],
                    "elapsed_sec": finishing_score_sync["elapsed_sec"],
                }
            )
    inflight_keys = sorted({f"{x['title']}|{x['short_sha']}" for x in active_submissions if x.get("title") and x.get("short_sha")})
    if local_worker is None:
        # Also detect an active worker running on a secondary teammate slot
        for s in eligible_slots:
            if s.get("phase") in ("waiting_cooldown", "packaging", "uploading"):
                local_worker = {
                    "title": s.get("active_title") or "",
                    "sha": s.get("active_sha") or "",
                    "phase": s.get("phase") or "packaging",
                    "started_epoch": now_epoch,
                }
                break
    # If at least one non-exhausted account is idle & ready, the submit channel is NOT locally blocked!
    has_other_idle_ready = any(
        s.get("phase") == "idle" and int(s.get("cooldown_remaining_sec") or 0) == 0
        for s in active_quota_slots
    )
    local_busy = (
        False
        if (len(active_quota_slots) >= 1 and len(eligible_slots) >= 2 and has_other_idle_ready)
        else (
            bool(local_worker is not None and local_worker.get("phase") in (
                "waiting_cooldown", "packaging", "preparing", "uploading", "consumer_locked"
            ))
            or bool(queue_lock_held and active_remote is None and finishing_score_sync is None)
        )
    )
    all_exhausted = bool(eligible_slots) and not active_quota_slots
    can_submit = (not all_exhausted) and (not local_busy) and (cooldown_rem == 0)

    public_slots = [
        {
            "id": s["id"],
            "label": s["label"],
            "logged_in": s["logged_in"],
            "enabled": s["enabled"],
            "cooldown_remaining_sec": s["cooldown_remaining_sec"],
            "cooldown_ends_epoch": s["cooldown_ends_epoch"],
            "used_today": int(s.get("used_today") or 0),
            "daily_limit": int(s.get("daily_limit") or 50),
            "remaining_today": int(s.get("remaining_today") if s.get("remaining_today") is not None else max(0, int(s.get("daily_limit") or 50) - int(s.get("used_today") or 0))),
            "quota_exhausted": bool(s.get("quota_exhausted")),
            "phase": s["phase"],
            "active_title": s["active_title"],
        }
        for s in account_slots
    ]

    # Priority A: Local uploader actively waiting cooldown / packaging / uploading a new submission
    if local_worker is not None and local_worker.get("phase") in (
        "waiting_cooldown", "packaging", "preparing", "uploading"
    ):
        w_title = local_worker.get("title") or (first_pending[0] if first_pending else None)
        w_sha = local_worker.get("sha") or (first_pending[1] if first_pending else "")
        raw_p = local_worker["phase"]
        mapped_phase = "packaging" if raw_p == "preparing" else raw_p
        st_ep = float(local_worker["started_epoch"])
        return {
            "is_busy": True,
            "local_busy": local_busy,
            "can_submit": can_submit,
            "phase": mapped_phase,
            "active_title": w_title,
            "active_sha": w_sha,
            "active_short_sha": w_sha[:7] if w_sha else "",
            "remote_submission_id": None,
            "remote_judge_id": None,
            "remote_status": None,
            "judge_started_epoch": round(st_ep, 2),
            "judge_elapsed_sec": max(0, int(now_epoch - st_ep)),
            "cooldown_ends_epoch": round(cooldown_ends_epoch, 2),
            "cooldown_remaining_sec": cooldown_rem,
            "rate_limit_gap_sec": gap_sec,
            "server_clock_offset_sec": server_offset_sec,
            "account_slots": public_slots,
            "active_submissions": active_submissions,
            "inflight_keys": inflight_keys,
        }

    # Priority B: Active non-terminal submission on Remote Judge (does NOT block next submit once cooldown clears!)
    if active_remote is not None:
        return {
            "is_busy": True,
            "local_busy": local_busy,
            "can_submit": can_submit,
            "phase": active_remote["phase"],
            "active_title": active_remote["title"],
            "active_sha": active_remote["sha"],
            "active_short_sha": active_remote["sha"][:7],
            "remote_submission_id": active_remote["submission_id"],
            "remote_judge_id": active_remote["judge_id"],
            "remote_status": active_remote["remote_status"],
            "judge_started_epoch": round(active_remote["started_epoch"], 2),
            "judge_elapsed_sec": max(0, int(now_epoch - active_remote["started_epoch"])),
            "cooldown_ends_epoch": round(cooldown_ends_epoch, 2),
            "cooldown_remaining_sec": cooldown_rem,
            "rate_limit_gap_sec": gap_sec,
            "server_clock_offset_sec": server_offset_sec,
            "account_slots": public_slots,
            "active_submissions": active_submissions,
            "inflight_keys": inflight_keys,
        }

    # Priority C: Remote judge finished, local consumer fetching ranking score before writing outcome
    if finishing_score_sync is not None:
        return {
            "is_busy": True,
            "local_busy": local_busy,
            "can_submit": can_submit,
            "phase": "fetching_score",
            "active_title": finishing_score_sync["title"],
            "active_sha": finishing_score_sync["sha"],
            "active_short_sha": finishing_score_sync["sha"][:7],
            "remote_submission_id": finishing_score_sync["submission_id"],
            "remote_judge_id": finishing_score_sync["judge_id"],
            "remote_status": finishing_score_sync["remote_status"],
            "judge_started_epoch": round(finishing_score_sync["started_epoch"], 2),
            "judge_elapsed_sec": max(0, int(now_epoch - finishing_score_sync["started_epoch"])),
            "cooldown_ends_epoch": round(cooldown_ends_epoch, 2),
            "cooldown_remaining_sec": cooldown_rem,
            "rate_limit_gap_sec": gap_sec,
            "server_clock_offset_sec": server_offset_sec,
            "account_slots": public_slots,
            "active_submissions": active_submissions,
            "inflight_keys": inflight_keys,
        }

    # Priority D: Fallback local lock holder (e.g. legacy external consumer)
    if local_worker is not None:
        w_title = local_worker.get("title") or (first_pending[0] if first_pending else None)
        w_sha = local_worker.get("sha") or (first_pending[1] if first_pending else "")
        raw_p = local_worker["phase"]
        if raw_p == "consumer_locked":
            mapped_phase = "waiting_cooldown" if cooldown_rem > 0 else "packaging"
        else:
            mapped_phase = "packaging"
        st_ep = float(local_worker["started_epoch"])
        return {
            "is_busy": True,
            "local_busy": local_busy,
            "can_submit": can_submit,
            "phase": mapped_phase,
            "active_title": w_title,
            "active_sha": w_sha,
            "active_short_sha": w_sha[:7] if w_sha else "",
            "remote_submission_id": None,
            "remote_judge_id": None,
            "remote_status": None,
            "judge_started_epoch": round(st_ep, 2),
            "judge_elapsed_sec": max(0, int(now_epoch - st_ep)),
            "cooldown_ends_epoch": round(cooldown_ends_epoch, 2),
            "cooldown_remaining_sec": cooldown_rem,
            "rate_limit_gap_sec": gap_sec,
            "server_clock_offset_sec": server_offset_sec,
            "account_slots": public_slots,
            "active_submissions": active_submissions,
            "inflight_keys": inflight_keys,
        }

    return {
        "is_busy": False,
        "local_busy": False,
        "can_submit": (cooldown_rem == 0),
        "phase": "idle",
        "active_title": None,
        "active_sha": None,
        "active_short_sha": None,
        "remote_submission_id": None,
        "remote_judge_id": None,
        "remote_status": None,
        "judge_started_epoch": None,
        "judge_elapsed_sec": 0,
        "cooldown_ends_epoch": round(cooldown_ends_epoch, 2),
        "cooldown_remaining_sec": cooldown_rem,
        "rate_limit_gap_sec": gap_sec,
        "server_clock_offset_sec": server_offset_sec,
        "account_slots": public_slots,
        "active_submissions": active_submissions,
        "inflight_keys": inflight_keys,
    }
