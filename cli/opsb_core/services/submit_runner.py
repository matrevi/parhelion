"""Single-shot jump-queue submission worker & live in-flight detector."""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


class _CooldownReservationCancelled(BaseException):
    """Raised when a sleeping `waiting_cooldown` worker is cleanly replaced by another jump-submit."""

from ..config import CLI_DIR, CONFIG_DIR, LOGS_DIR, _resolve_parhelion_home, invalidate_ttl_cache, parse_iso, read_json, read_jsonl, resolve_python_exe, utc_now
from .auth_service import list_judge_accounts
from .inflight_io import (
    inflight_pid,
    read_inflight,
    reap_inflight_if_stale,
    write_inflight_atomic,
)
from .judge_score_fetch import DEFAULT_RANKING_RETRY_DELAYS, TERMINAL_JUDGE_STATUSES
from .judge_sync import INFLIGHT_FILE, TERMINAL_OUTCOME_STATUSES
from .sync_reconciler import collapse_reconciled_outcomes, has_pending_sync_error_for_commit

MIN_RATE_GAP_SEC = 0  # only the judge's own 429 decides pacing
JUDGE_SUBMIT_GAP_SEC = 0
POLL_TIMEOUT_SEC = 1800
_LOCAL_ACTIVE_PHASES = ("waiting_cooldown", "packaging", "preparing", "uploading")


def inflight_file_for(account_id: str) -> Path:
    base = _resolve_parhelion_home()
    if base == CONFIG_DIR and account_id == "primary":
        return INFLIGHT_FILE
    return base / ("inflight_submit.json" if account_id == "primary" else f"inflight-{account_id}.json")


def lock_file_for(workspace: Path, account_id: str) -> Path:
    res = workspace / "results"
    return (res / ".submit-queue.lock") if account_id == "primary" else (res / f".submit-queue-{account_id}.lock")


def compute_account_slots(
    workspace: Path,
    outcome_rows: list[dict[str, Any]] | None = None,
    gap_sec: int = MIN_RATE_GAP_SEC,
    daily_limit: int = 50,
) -> list[dict[str, Any]]:
    """Compute per-teammate-account independent 190s cooldown, Beijing-day quota & live worker status."""
    import datetime as _dt

    results_dir = workspace / "results"
    if outcome_rows is None:
        outcome_rows = read_jsonl(results_dir / "submit-queue-outcome.jsonl")
    outcome_rows = collapse_reconciled_outcomes(outcome_rows)
    try:
        from .judge_quota import read_quota  # type: ignore

        _quota = read_quota(results_dir.parent)
    except Exception:
        _quota = None
    accounts = list_judge_accounts()
    uid_to_aid = {str(a.get("user_id")): str(a["id"]) for a in accounts if a.get("user_id")}

    bj_tz = _dt.timezone(_dt.timedelta(hours=8))
    bj_now = utc_now().astimezone(bj_tz)
    bj_day_start_ep = bj_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    last_epoch_by_aid: dict[str, float] = {str(a["id"]): 0.0 for a in accounts}
    today_sids_by_aid: dict[str, set[str]] = {str(a["id"]): set() for a in accounts}

    for r_file in results_dir.glob("*/*/submission-receipt.json"):
        rd = read_json(r_file, {})
        dt = parse_iso(rd.get("submitted_at"))
        if not dt:
            continue
        aid = str(rd.get("account_id") or "")
        if not aid or aid not in last_epoch_by_aid:
            aid = uid_to_aid.get(str(rd.get("user_id") or ""), "primary")
        if aid in last_epoch_by_aid:
            ts = dt.timestamp()
            last_epoch_by_aid[aid] = max(last_epoch_by_aid[aid], ts)
            sid = str(rd.get("submission_id") or "")
            if sid and ts >= bj_day_start_ep:
                today_sids_by_aid[aid].add(sid)

    for o in outcome_rows:
        if o.get("status") not in ("rejected", "skipped", "duplicate") and o.get("submission_id"):
            dt = parse_iso(o.get("submitted_at") or o.get("finished_at"))
            if not dt:
                continue
            aid = str(o.get("account_id") or "primary")
            if aid not in last_epoch_by_aid:
                aid = "primary"
            ts = dt.timestamp()
            last_epoch_by_aid[aid] = max(last_epoch_by_aid[aid], ts)
            sid = str(o.get("submission_id") or "")
            if sid and ts >= bj_day_start_ep:
                today_sids_by_aid[aid].add(sid)

    now_ep = utc_now().timestamp()
    observed_wait = judge_cooldown_remaining(workspace)
    slots: list[dict[str, Any]] = []
    for a in accounts:
        aid = str(a["id"])
        last_ep = last_epoch_by_aid.get(aid, 0.0)
        ends_ep = now_ep + observed_wait if observed_wait else 0.0
        rem = observed_wait
        inf_path = inflight_file_for(aid)
        phase = "idle"
        active_title = None
        active_sha = None
        worker_pid = 0
        if inf_path.is_file():
            inf = read_inflight(inf_path)
            pid = inflight_pid(inf)
            if inf is not None and pid > 0 and _pid_alive(pid):
                phase = str(inf.get("phase") or "packaging")
                active_title = inf.get("title")
                active_sha = inf.get("sha")
                worker_pid = pid
            else:
                # Never unlink on a torn/unreadable read; only reap a provably dead, unchanged tracker.
                reap_inflight_if_stale(inf_path, inf)
        used_today = len(today_sids_by_aid.get(aid, set()))
        # The judge's own count wins: receipts only cover submissions our queue sent.
        if _quota and aid == "primary":
            used_today = max(used_today, int(_quota.get("used") or 0))
        rem_today = max(0, daily_limit - used_today)
        slots.append(
            {
                "id": aid,
                "label": str(a.get("label") or aid),
                "account_hint": str(a.get("account_hint") or ""),
                "user_id": a.get("user_id"),
                "session_file": str(a.get("session_file") or ""),
                "logged_in": bool(a.get("logged_in")),
                "enabled": bool(a.get("enabled", True)),
                "cooldown_ends_epoch": round(ends_ep, 2),
                "cooldown_remaining_sec": rem,
                "used_today": used_today,
                "daily_limit": daily_limit,
                "remaining_today": rem_today,
                "quota_exhausted": rem_today <= 0,
                "phase": phase,
                "active_title": active_title,
                "active_sha": active_sha,
                "worker_pid": worker_pid,
            }
        )
    return slots


def _slot_balance_key(slot: dict[str, Any]) -> tuple[int, int, str, str]:
    """Load-balance key across N accounts:
    1. Higher remaining daily quota first (-remaining_today)
    2. Teammate accounts before primary when remaining quota ties (0 vs 1)
    3. Lexicographical order among teammates (label, then id)
    """
    lim = int(slot.get("daily_limit") or 50)
    used = int(slot.get("used_today") or 0)
    rem = int(slot["remaining_today"]) if slot.get("remaining_today") is not None else max(0, lim - used)
    aid = str(slot.get("id") or "")
    is_primary = 1 if aid == "primary" else 0
    label_key = str(slot.get("label") or aid).casefold()
    return (-rem, is_primary, label_key, aid.casefold())


def _is_slot_exhausted(slot: dict[str, Any]) -> bool:
    if slot.get("quota_exhausted") is not None:
        return bool(slot["quota_exhausted"])
    lim = int(slot.get("daily_limit") or 50)
    used = int(slot.get("used_today") or 0)
    return used >= lim


def select_best_account_slot(
    slots: list[dict[str, Any]],
    account_id: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Pick the best non-exhausted submitter slot using quota-balanced, teammate-first routing."""
    eligible = [s for s in slots if s.get("enabled") and s.get("logged_in")]
    if not eligible:
        eligible = [s for s in slots if s.get("id") == "primary"]
    if not eligible:
        return None, "未找到已登录的提交账号，请在设置中登录"

    if account_id:
        exact = next((s for s in eligible if s.get("id") == account_id), None)
        if exact is not None:
            if _is_slot_exhausted(exact):
                lim = int(exact.get("daily_limit") or 50)
                used = int(exact.get("used_today") or lim)
                return None, f"账号 {exact.get('label') or account_id} 今日额度已满 ({used}/{lim})，已自动跳过"
            return exact, None

    available = [s for s in eligible if not _is_slot_exhausted(s)]
    if not available:
        lim = int(eligible[0].get("daily_limit") or 50)
        return None, f"所有已启用账号今日 {lim}/{lim} 额度均已用尽，已自动暂停提交"

    # Tier 0: Idle & cooldown ready (0s wait) — strictly balanced by remaining quota, teammate-first, lex order
    ready_idle = sorted(
        [s for s in available if s.get("phase") == "idle" and int(s.get("cooldown_remaining_sec") or 0) == 0],
        key=_slot_balance_key,
    )
    if ready_idle:
        return ready_idle[0], None

    # Tier 1: Idle & cooling — shortest cooldown first, tie-break by load-balance key
    idle_any = sorted(
        [s for s in available if s.get("phase") == "idle"],
        key=lambda x: (int(x.get("cooldown_remaining_sec") or 0), *_slot_balance_key(x)),
    )
    if idle_any:
        return idle_any[0], None

    # Tier 2: Sleeping in waiting_cooldown (cleanly replaceable)
    waiting = sorted(
        [s for s in available if s.get("phase") == "waiting_cooldown"],
        key=lambda x: (int(x.get("cooldown_remaining_sec") or 0), *_slot_balance_key(x)),
    )
    if waiting:
        return waiting[0], None

    # Tier 3: Fallback among non-exhausted slots
    return sorted(available, key=_slot_balance_key)[0], None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def detect_inflight_submission(
    workspace: Path,
    queue_lock_held: bool,
    first_pending: tuple[str, str] | None,
) -> dict[str, Any]:
    """Identify if a submission is currently in-flight (either via Parhelion worker or legacy submit-queue.py)."""
    now = utc_now()

    # 1. Check Parhelion's own inflight tracker
    inf_file = inflight_file_for("primary")
    if inf_file.is_file():
        raw_inf = read_inflight(inf_file)
        data = raw_inf or {}
        pid = inflight_pid(data)
        if pid and _pid_alive(pid):
            st_dt = parse_iso(data.get("started_at"))
            el = max(0, int((now - st_dt).total_seconds())) if st_dt else 0
            return {
                "active": True,
                "title": data.get("title") or "",
                "sha": data.get("sha") or "",
                "phase": data.get("phase") or "submitting",
                "elapsed_sec": el,
            }
        else:
            reap_inflight_if_stale(inf_file, raw_inf)

    if not queue_lock_held:
        return {"active": False, "title": None, "sha": None, "phase": None, "elapsed_sec": 0}

    # 2. Lock is held by external `tools/submit-queue.py` — check `ps` for `submit-validated.py` first
    try:
        out = subprocess.check_output(["ps", "-eo", "etimes,args"], text=True, timeout=1.0)
        for line in out.splitlines():
            if "submit-validated.py" in line and "grep" not in line:
                parts = line.strip().split(None, 1)
                el = int(parts[0]) if parts and parts[0].isdigit() else 0
                cmd = parts[1] if len(parts) > 1 else ""
                m_t = re.search(r"submit-validated\.py\s+([A-Za-z0-9_]+)", cmd)
                m_s = re.search(r"--sha\s+([0-9a-f]{7,40})", cmd)
                if m_t:
                    return {
                        "active": True,
                        "title": m_t.group(1),
                        "sha": m_s.group(1) if m_s else "",
                        "phase": "submitting",
                        "elapsed_sec": el,
                    }
    except Exception:
        pass

    # 3. Fallback when external submit-queue.py is in `wait_and_score` polling Judge:
    # `submit-queue.py` drains `pending` in FIFO order, so `first_pending` is the active item!
    if first_pending:
        return {
            "active": True,
            "title": first_pending[0],
            "sha": first_pending[1],
            "phase": "judging",
            "elapsed_sec": 0,
        }

    return {"active": True, "title": "Submission", "sha": "", "phase": "judging", "elapsed_sec": 0}


def inflight_titles_from_pipeline(pipeline: dict[str, Any] | None) -> set[str]:
    """Collect operator titles currently active in `pipeline` (`inflight_keys`, `active_submissions`, `active_title`)."""
    titles: set[str] = set()
    if not isinstance(pipeline, dict):
        return titles
    for key in pipeline.get("inflight_keys") or []:
        t = str(key).split("|", 1)[0].strip()
        if t:
            titles.add(t)
    for act in pipeline.get("active_submissions") or []:
        if isinstance(act, dict):
            t = str(act.get("title") or "").strip()
            if t:
                titles.add(t)
    act_title = str(pipeline.get("active_title") or "").strip()
    if act_title:
        titles.add(act_title)
    for s in pipeline.get("account_slots") or []:
        if isinstance(s, dict) and s.get("phase") in _LOCAL_ACTIVE_PHASES:
            t = str(s.get("active_title") or "").strip()
            if t:
                titles.add(t)
    return titles


def find_active_inflight_for_title(
    workspace: Path,
    title: str,
    *,
    pipeline: dict[str, Any] | None = None,
    slots: list[dict[str, Any]] | None = None,
    outcomes_all: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Return active inflight descriptor if `title` already has a live/running submission (any SHA)."""
    if not title:
        return None
    if pipeline is not None and title in inflight_titles_from_pipeline(pipeline):
        for act in pipeline.get("active_submissions") or []:
            if isinstance(act, dict) and act.get("title") == title:
                return {"source": "pipeline", "title": title, "sha": str(act.get("sha") or ""), "phase": str(act.get("phase") or "inflight")}
        return {"source": "pipeline", "title": title, "sha": str(pipeline.get("active_sha") or ""), "phase": str(pipeline.get("phase") or "inflight")}

    check_slots = slots if slots is not None else compute_account_slots(workspace)
    for s in check_slots:
        if s.get("phase") in _LOCAL_ACTIVE_PHASES and s.get("active_title") == title:
            return {"source": "slot", "title": title, "sha": str(s.get("active_sha") or ""), "phase": str(s.get("phase")), "account_label": s.get("label")}

    results_dir = workspace / "results"
    title_dir = results_dir / title
    if title_dir.is_dir():
        outs = outcomes_all if outcomes_all is not None else collapse_reconciled_outcomes(read_jsonl(results_dir / "submit-queue-outcome.jsonl"))
        terminal_sids = {
            str(o.get("submission_id"))
            for o in outs
            if o.get("submission_id") and o.get("status") in TERMINAL_OUTCOME_STATUSES
        }
        now_ep = utc_now().timestamp()
        for r_file in title_dir.glob("*/submission-receipt.json"):
            rd = read_json(r_file, {})
            sid = str(rd.get("submission_id") or "")
            dt = parse_iso(rd.get("submitted_at"))
            if sid and sid not in terminal_sids and dt and (now_ep - dt.timestamp()) < 2100:
                return {"source": "receipt", "title": title, "sha": str(rd.get("validated_commit") or r_file.parent.name), "phase": "remote_running", "submission_id": sid}
    return None


def cancel_waiting_cooldown_for_commit(title: str, sha: str) -> int:
    """Cancel any `waiting_cooldown` worker for `(title, sha[:7])` when skipped; returns killed count."""
    prefix = (sha or "")[:7].lower()
    if not title or not prefix:
        return 0
    cancelled = 0
    for inf_path in _resolve_parhelion_home().glob("inflight*.json"):
        inf = read_json(inf_path, {})
        if not isinstance(inf, dict) or inf.get("phase") != "waiting_cooldown":
            continue
        if inf.get("title") != title or not str(inf.get("sha") or "").lower().startswith(prefix):
            continue
        pid = int(inf.get("pid") or 0)
        if pid > 0 and pid != os.getpid() and _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and _pid_alive(pid):
                    time.sleep(0.05)
                inf_path.unlink(missing_ok=True)
                cancelled += 1
            except OSError:
                pass
    return cancelled


def spawn_jump_submit(
    opsb_bin: Path,
    workspace: Path,
    title: str,
    sha: str,
    note: str,
    account_id: str | None = None,
    force: bool = False,
    enforce_operator_mutex: bool = False,
    pipeline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Workspace-wide dispatch gate: the UI asks to submit every queued problem at once. Only
    # one may enter the send path; the rest must stay "pending" instead of showing 提交中.
    results_dir = workspace / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    with (results_dir / ".submit-dispatch.lock").open("a") as dlf:
        try:
            fcntl.flock(dlf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"ok": False, "deferred": True, "reason": "busy",
                    "error": "另一个题目正在发送中，本条保持排队"}
        if submissions_frozen(workspace):
            return {"ok": False, "deferred": True, "reason": "frozen",
                    "error": "提交已冻结（results/.submit-freeze），修复队列后删除该文件恢复"}
        cooling = judge_cooldown_remaining(workspace)
        if cooling > 0:
            return {"ok": False, "deferred": True, "reason": "cooldown", "retry_after": cooling,
                    "error": f"判题器冷却中，{cooling}s 后再试"}
        return _spawn_jump_submit_locked(
            opsb_bin, workspace, title, sha, note, account_id=account_id, force=force,
            enforce_operator_mutex=enforce_operator_mutex, pipeline=pipeline, dispatch_fd=dlf.fileno(),
        )


def _spawn_jump_submit_locked(
    opsb_bin: Path,
    workspace: Path,
    title: str,
    sha: str,
    note: str,
    account_id: str | None = None,
    force: bool = False,
    enforce_operator_mutex: bool = False,
    pipeline: dict[str, Any] | None = None,
    *, dispatch_fd: int,
) -> dict[str, Any]:
    slots = compute_account_slots(workspace)
    # Global anti-collision guard: NEVER let two teammate accounts submit the exact same (title, sha)
    # while it is already packaging/uploading/waiting OR already submitted & still running remotely!
    for s in slots:
        if s["phase"] in ("waiting_cooldown", "packaging", "uploading"):
            if s.get("active_title") == title and str(s.get("active_sha") or "").startswith(sha[:7]):
                return {
                    "ok": False,
                    "duplicate_inflight": True,
                    "error": f"{title} ({sha[:7]}) 已在 {s['label']} 通道中，禁止重复提交",
                }
    results_dir = workspace / "results"
    outcomes_all = collapse_reconciled_outcomes(read_jsonl(results_dir / "submit-queue-outcome.jsonl"))
    if enforce_operator_mutex:
        op_inflight = find_active_inflight_for_title(
            workspace, title, pipeline=pipeline, slots=slots, outcomes_all=outcomes_all
        )
        if op_inflight is not None:
            return {
                "ok": False,
                "operator_inflight": True,
                "operator_inflight_mutex": True,
                "duplicate_inflight": True,
                "inflight": op_inflight,
                "error": f"{title} 已有在途提交 ({str(op_inflight.get('sha') or '')[:7]})，按单算子互斥保护保持排队等待",
            }
    outcome_sids = {
        str(o.get("submission_id"))
        for o in outcomes_all
        if o.get("submission_id")
        and o.get("status") not in ("Running", "Queued", "Pending", "Waiting", "Compiling", "Building", "Judging", "Testing")
    }
    now_ep = utc_now().timestamp()
    sha7_low = (sha or "")[:7].lower()
    title_dir = results_dir / title
    for r_file in title_dir.glob("*/submission-receipt.json") if title_dir.is_dir() else []:
        rd = read_json(r_file, {})
        if not isinstance(rd, dict):
            continue
        r_sha = str(rd.get("validated_commit") or r_file.parent.name or "").lower()
        if not (r_file.parent.name.lower().startswith(sha7_low) or r_sha.startswith(sha7_low)):
            continue
        sid = str(rd.get("submission_id") or "")
        dt = parse_iso(rd.get("submitted_at"))
        if sid and sid not in outcome_sids and dt and (now_ep - dt.timestamp()) < 2100:
            return {
                "ok": False,
                "duplicate_inflight": True,
                "error": f"{title} ({sha[:7]}) 已在远程判题中 (sid={sid[:8]})，禁止重复碰撞提交",
            }
    if not force:
        sync_hit = has_pending_sync_error_for_commit(outcomes_all, title, sha)
        if sync_hit is not None:
            sid = str(sync_hit.get("submission_id") or "")
            return {
                "ok": False,
                "duplicate_sync_error": True,
                "duplicate_inflight": True,
                "duplicate_terminal": True,
                "error": f"{title} ({sha[:7]}) 已上传 (sid={sid[:8]}) 但同步中断，等待自动补偿同步，禁止重复提交",
            }
        terminal_statuses = TERMINAL_JUDGE_STATUSES | {"skipped", "blocked_compliance"}
        for o in reversed(outcomes_all):
            if o.get("title") != title or not str(o.get("sha") or "").lower().startswith(sha7_low):
                continue
            st = str(o.get("status") or "").strip()
            has_term = (st in terminal_statuses) or (st == "sync_error" and bool(o.get("submission_id")))
            if not has_term:
                continue
            f_dt = parse_iso(o.get("finished_at") or o.get("submitted_at"))
            is_recent = bool(st in TERMINAL_JUDGE_STATUSES and f_dt and (now_ep - f_dt.timestamp()) < 600)
            return {
                "ok": False,
                "duplicate_terminal": True,
                **({"duplicate_recent_outcome": True} if is_recent else {}),
                "status": st,
                "score": o.get("score"),
                "submission_id": o.get("submission_id"),
                "error": f"{title} ({sha[:7]}) 已有终态记录 ({st}, score={o.get('score')})，禁止自动重复提交（手动重提请使用 --force）",
            }

    chosen, select_err = select_best_account_slot(slots, account_id=account_id)
    if chosen is None:
        return {
            "ok": False,
            "quota_exhausted": True,
            "error": select_err or "所有账号今日提交额度已满",
        }

    aid = str(chosen["id"])
    inf_path = inflight_file_for(aid)
    lock_file = lock_file_for(workspace, aid)

    # If the chosen slot is ONLY sleeping in `waiting_cooldown` (not yet packaging/uploading),
    # cleanly replace that cooldown reservation with the newly clicked candidate!
    if inf_path.is_file():
        inf = read_json(inf_path, {})
        old_pid = int(inf.get("pid") or 0)
        if inf.get("phase") == "waiting_cooldown" and old_pid > 0 and old_pid != os.getpid() and _pid_alive(old_pid):
            try:
                os.kill(old_pid, signal.SIGTERM)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and _pid_alive(old_pid):
                    time.sleep(0.05)
                inf_path.unlink(missing_ok=True)
            except OSError:
                pass

    if lock_file.exists():
        try:
            with lock_file.open("r") as lf:
                try:
                    fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(lf, fcntl.LOCK_UN)
                except BlockingIOError:
                    return {"ok": False, "busy": True, "error": f"提交器 ({chosen['label']}) 正在打包上传中，请等待数秒后再试"}
        except Exception:
            pass

    py_exe = resolve_python_exe(workspace)
    LOGS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_path = LOGS_DIR / "last_jump_submit.log"
    with log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            [
                py_exe,
                str(opsb_bin),
                "queue",
                "_worker-submit-one",
                title,
                sha,
                "--note",
                note or "jump-queue submit via Parhelion",
                "--account-id",
                aid,
                "--workspace",
                str(workspace),
                *(["--force"] if force else []),
            ],
            pass_fds=(dispatch_fd,),
            env={**os.environ, "OPSB_DISPATCH_FD": str(dispatch_fd)},
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )

    invalidate_ttl_cache("scheduler_bridge")
    invalidate_ttl_cache("remote_judge")
    return {
        "ok": True,
        "started": True,
        "pid": proc.pid,
        "title": title,
        "sha": sha,
        "account_id": aid,
        "account_label": chosen["label"],
    }


def _write_inflight_phase(title: str, sha: str, workspace: Path, started_iso: str, phase: str, account_id: str = "primary", account_label: str = "主账号") -> None:
    try:
        inf_path = inflight_file_for(account_id)
        write_inflight_atomic(
            inf_path,
            {
                "pid": os.getpid(),
                "workspace": str(workspace),
                "title": title,
                "sha": sha,
                "account_id": account_id,
                "account_label": account_label,
                "started_at": started_iso,
                "phase": phase,
            },
        )
    except Exception:
        pass


def run_worker_submit_one(workspace: Path, title: str, sha: str, note: str, account_id: str = "primary", force: bool = False) -> int:
    """Claim one upload; remote polling continues after releasing the claim."""
    results_dir = workspace / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    lock_path = results_dir / ".submit-dispatch.lock"
    inherited_fd = os.environ.pop("OPSB_DISPATCH_FD", None)
    with (os.fdopen(int(inherited_fd), "a") if inherited_fd else lock_path.open("a")) as dlf:
        if not os.path.samestat(os.fstat(dlf.fileno()), lock_path.stat()):
            raise ValueError("dispatch lock does not belong to workspace")
        try:
            fcntl.flock(dlf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 2  # someone else is submitting: stay silent, keep the item pending
        try:
            if submissions_frozen(workspace):
                return 5
            cooling = judge_cooldown_remaining(workspace)
            if cooling > 0:
                try:
                    from scheduler.feedback_log import log_feedback  # type: ignore

                    log_feedback(workspace, "submit_cooldown", title=title, sha=sha[:7], retry_after=cooling)
                except Exception:
                    pass
                return 4
            return _run_worker_submit_locked(workspace, title, sha, note, account_id, force, dlf.fileno())
        finally:
            fcntl.flock(dlf, fcntl.LOCK_UN)


def _run_worker_submit_locked(workspace: Path, title: str, sha: str, note: str, account_id: str, force: bool, dispatch_fd: int) -> int:
    """Serialize upload claims and publish receipts before parallel polling."""
    results_dir = workspace / "results"
    aid = (account_id or "primary").strip() or "primary"
    accounts = list_judge_accounts()
    slot_meta = next((a for a in accounts if a.get("id") == aid), {"id": aid, "label": "主账号", "session_file": ""})
    acc_label = str(slot_meta.get("label") or aid)
    session_file = str(slot_meta.get("session_file") or "")
    lock_path = lock_file_for(workspace, aid)
    io_lock_path = results_dir / ".submit-queue-io.lock"
    inf_path = inflight_file_for(aid)
    queue_path = results_dir / "submit-queue.jsonl"
    outcome_path = results_dir / "submit-queue-outcome.jsonl"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    started_iso = utc_now().isoformat()
    record: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None
    early_out_rec: dict[str, Any] | None = None

    # Hold the claim until the judge acknowledges the upload.
    with lock_path.open("a") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 2

        try:
            # Final race-condition guard: verify THIS account still has Beijing-day quota (< 50)
            fresh_slots = compute_account_slots(workspace)
            my_slot = next((s for s in fresh_slots if s.get("id") == aid), None)
            if my_slot is not None and _is_slot_exhausted(my_slot):
                return 3

            cooling = judge_cooldown_remaining(workspace)
            if cooling > 0:
                # Staying pending is the whole state: an item with no outcome row is retried
                # by the consumer. Writing a cooldown row here flooded the history with one
                # entry per queue tick.
                try:
                    from scheduler.feedback_log import log_feedback  # type: ignore

                    log_feedback(workspace, "submit_cooldown", title=title, sha=sha[:7], retry_after=cooling)
                except Exception:
                    pass
                return 4

            from ..ipc_bridge import invoke_bridge
            gate_args = ["--title", title, "--sha", sha, "--note", note] + (["--force"] if force else [])
            verdict = invoke_bridge(workspace, "evaluate", gate_args)
            if not verdict or not verdict.get("approved"):
                return 4
            _write_inflight_phase(title, sha, workspace, started_iso, "packaging", aid, acc_label)

            # Reuse existing pending queue entry if present, otherwise append audit record
            with io_lock_path.open("a") as iolf:
                fcntl.flock(iolf, fcntl.LOCK_EX)
                try:
                    existing_pending = None
                    existing_outcomes = {(o.get("title"), o.get("sha"), o.get("queued_at")) for o in read_jsonl(outcome_path)}
                    for q in reversed(read_jsonl(queue_path)):
                        if q.get("title") == title and str(q.get("sha", "")).startswith(sha[:7]):
                            if (q.get("title"), q.get("sha"), q.get("queued_at")) not in existing_outcomes:
                                existing_pending = q
                                break

                    if existing_pending is not None:
                        record = dict(existing_pending)
                        if note and not record.get("note"):
                            record["note"] = note
                    else:
                        record = {
                            "title": title,
                            "sha": sha,
                            "note": note,
                            "queued_at": started_iso,
                            "status": "queued",
                            "force": force,
                        }
                        with queue_path.open("a", encoding="utf-8") as qf:
                            qf.write(json.dumps(record, ensure_ascii=False) + "\n")
                finally:
                    fcntl.flock(iolf, fcntl.LOCK_UN)

            # A manual skip may arrive after the worker claimed the queue row.
            sha7_low = sha[:7].lower()
            skipped_during_wait = any(
                o.get("title") == title
                and o.get("status") == "skipped"
                and str(o.get("sha") or "").lower().startswith(sha7_low)
                and str(o.get("finished_at") or o.get("queued_at") or "") >= started_iso
                for o in read_jsonl(outcome_path)
            )
            if skipped_during_wait:
                raise _CooldownReservationCancelled()

            _write_inflight_phase(title, sha, workspace, started_iso, "uploading", aid, acc_label)
            receipt = _submit_via_worktree(workspace, title, sha, account_id=aid, session_file=session_file, force=force)
            if JUDGE_SUBMIT_GAP_SEC > 0:
                set_judge_cooldown(workspace, JUDGE_SUBMIT_GAP_SEC, "pacing after successful submit")
            invalidate_ttl_cache("remote_judge")
            invalidate_ttl_cache("scheduler_bridge")
            try:
                from scheduler.feedback_log import log_feedback  # type: ignore

                log_feedback(workspace, "submit", title=title, sha=sha[:7], account_id=aid,
                             submission_id=receipt.get("submission_id"),
                             existing=bool(receipt.get("existing")))
            except Exception:
                pass
        except _CooldownReservationCancelled:
            early_out_rec = {"cancelled": True}
            invalidate_ttl_cache("scheduler_bridge")
        except SubmitRateLimited as exc:
            # Defer silently: no outcome row (that is what fills the history), no queue copy
            # (the original pending row is still unclaimed). The judge's own retry_after sets
            # the next attempt window for every problem.
            set_judge_cooldown(workspace, exc.retry_after, "judge 429")
            try:
                from scheduler.feedback_log import log_feedback  # type: ignore

                log_feedback(workspace, "submit_rate_limited", title=title, sha=sha[:7],
                             retry_after=exc.retry_after, account_id=aid)
            except Exception:
                pass
            invalidate_ttl_cache("scheduler_bridge")
        except Exception as exc:
            msg = str(exc)
            # Attach the child's step trace: an rc=0/no-output child left no other evidence.
            trace_tail = ""
            try:
                lines = (workspace / "results" / title / sha / "submit-trace.log").read_text(errors="replace").splitlines()[-6:]
                trace_tail = " | trace: " + " ; ".join(ln[:120] for ln in lines)
            except OSError:
                pass
            base_rec = record or {"title": title, "sha": sha, "note": note, "queued_at": started_iso}
            early_out_rec = {
                **base_rec,
                "finished_at": utc_now().isoformat(),
                "status": "blocked_compliance" if "不合规内容" in msg else "rejected",
                "account_id": aid,
                "account_label": acc_label,
                "detail": (msg + trace_tail)[:2000],
            }
            try:
                from scheduler.feedback_log import log_feedback  # type: ignore

                log_feedback(workspace, "submit_error", title=title, sha=sha[:7],
                             detail=str(exc)[:2000], account_id=aid)
            except Exception:
                pass
            with io_lock_path.open("a") as iolf:
                fcntl.flock(iolf, fcntl.LOCK_EX)
                try:
                    with outcome_path.open("a", encoding="utf-8") as of:
                        of.write(json.dumps(early_out_rec, ensure_ascii=False) + "\n")
                finally:
                    fcntl.flock(iolf, fcntl.LOCK_UN)
            invalidate_ttl_cache("scheduler_bridge")
        finally:
            inf_path.unlink(missing_ok=True)
            fcntl.flock(lf, fcntl.LOCK_UN)

    if early_out_rec is not None or receipt is None or record is None:
        return 0

    fcntl.flock(dispatch_fd, fcntl.LOCK_UN)
    # Phase 2 (lock-free): poll remote Judge to terminal status using the submitting account's session
    sub_id = receipt["submission_id"]
    submitted_at = str(receipt.get("submitted_at") or utc_now().isoformat())
    try:
        status, score, pub_times, pub_tbest = _wait_and_fetch_score(
            workspace, title, sub_id, session_file=session_file, submitted_at=submitted_at
        )
        out_rec = {
            **record,
            "finished_at": utc_now().isoformat(),
            "status": status,
            "submission_id": sub_id,
            "score": score,
            "account_id": aid,
            "account_label": acc_label,
            "submitted_at": submitted_at,
            **({"public_times_us": pub_times} if pub_times else {}),
            **({"public_tbest_us": pub_tbest} if pub_tbest else {}),
        }
    except Exception as exc:
        out_rec = {
            **record,
            "finished_at": utc_now().isoformat(),
            "status": "sync_error",
            "submission_id": sub_id,
            "account_id": aid,
            "account_label": acc_label,
            "submitted_at": receipt.get("submitted_at") or utc_now().isoformat(),
            "detail": f"poll error: {str(exc)[:360]}",
        }

    # Briefly lock IO only to append terminal outcome atomically (dedup if reconciler already wrote it)
    with io_lock_path.open("a") as iolf:
        fcntl.flock(iolf, fcntl.LOCK_EX)
        try:
            existing_rows = collapse_reconciled_outcomes(read_jsonl(outcome_path))
            existing_match = next((r for r in existing_rows if str(r.get("submission_id") or "") == sub_id), None)
            already_terminal = bool(
                existing_match is not None
                and str(existing_match.get("status") or "") in TERMINAL_JUDGE_STATUSES
                and (out_rec.get("score") is None or existing_match.get("score") is not None)
            )
            if not already_terminal:
                with outcome_path.open("a", encoding="utf-8") as of:
                    of.write(json.dumps(out_rec, ensure_ascii=False) + "\n")
                invalidate_ttl_cache("scheduler_bridge")
                invalidate_ttl_cache("remote_judge")
        finally:
            fcntl.flock(iolf, fcntl.LOCK_UN)

    # Closed-loop calibration + auto-archive into `breakthrough/` on terminal evaluation
    if out_rec.get("status") in TERMINAL_JUDGE_STATUSES:
        from ..knowledge.bugs import sync_bugs
        sync_bugs(workspace)
    if out_rec.get("status") not in ("rejected", "skipped", "duplicate", "sync_error"):
        try:
            from ..config import SUPERVISOR_ROOT

            py_exe = resolve_python_exe(workspace)
            subprocess.run(
                [py_exe, "-m", "scheduler.cli_bridge", "calibrate", "--workspace", str(workspace), "--title", title],
                cwd=str(SUPERVISOR_ROOT),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15.0,
            )
            invalidate_ttl_cache("scheduler_bridge")
        except Exception:
            pass

    if out_rec.get("status") == "Pass":
        try:
            from ..config import ConfigRepository
            from .breakthrough_service import sync_breakthroughs_from_items
            from .queue_service import compute_workspace_snapshot

            cfg = ConfigRepository.load()
            snap = compute_workspace_snapshot(workspace, cfg)
            thresh = float(snap["scheduler"].get("breakthrough_threshold", 5.0))
            probs_map = {p["title"]: p for p in snap["problems"]}
            sync_breakthroughs_from_items(workspace, snap["submission_queue"], probs_map, thresh)
        except Exception:
            pass

    # Every judged submission shifts the contest board: refresh the team ranking off-path.
    try:
        from . import ranking_service

        ranking_service.refresh_async(workspace)
    except Exception:
        pass
    return 0


class SubmitRateLimited(RuntimeError):
    """Judge asked us to wait; requeue instead of burning the caller's 90s timeout."""

    def __init__(self, retry_after: int, message: str = "") -> None:
        super().__init__(message or f"judge cooldown {retry_after}s")
        self.retry_after = max(1, int(retry_after))



def _judge_cooldown_path(workspace: Path) -> Path:
    return workspace / "results" / ".judge-cooldown.json"


def submissions_frozen(workspace: Path) -> bool:
    """Operator freeze: results/.submit-freeze exists -> nothing is dispatched.

    Used while the queue/board are being reconciled; delete the file to resume.
    """
    return (workspace / "results" / ".submit-freeze").is_file()


def judge_cooldown_remaining(workspace: Path) -> int:
    """Seconds left on the *judge-wide* submission cooldown (0 = free to attempt).

    The judge rate-limits the account, not the title: with several problems queued,
    per-title pacing let every worker race for the same slot and all of them got 429.
    """
    try:
        doc = read_json(_judge_cooldown_path(workspace), {})
        not_before = float(doc.get("not_before_epoch") or 0.0)
        return max(0, int(round(not_before - utc_now().timestamp())))
    except Exception:
        return 0


def set_judge_cooldown(workspace: Path, seconds: float, reason: str = "") -> None:
    try:
        epoch = utc_now().timestamp() + max(0.0, float(seconds))
        path = _judge_cooldown_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "not_before_epoch": epoch,
            "not_before": datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat(),
            "reason": reason,
            "updated_at": utc_now().isoformat(),
        }, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass

def _submit_via_worktree(
    workspace: Path,
    title: str,
    sha: str,
    account_id: str = "primary",
    session_file: str = "",
    force: bool = False,
) -> dict[str, Any]:
    try:
        from scheduler.parhelion_paths import load_problem_manifest  # type: ignore

        problems = load_problem_manifest(workspace)
    except Exception:
        problems = read_json(workspace / "bench" / "problems.json", [])
    p_meta = next((p for p in problems if p.get("title") == title), {"path": title})
    repo = workspace / str(p_meta.get("path") or title)
    if not (repo / ".git").is_dir() and (workspace / "bench" / title).is_dir():
        repo = workspace / "bench" / title
    safe_aid = re.sub(r"[^A-Za-z0-9_-]", "", account_id or "primary") or "primary"
    worktree = Path("/tmp") / f"opsb-submit-{title}-{safe_aid}"
    venv_py = workspace / ".venv" / "bin" / "python"
    py_exe = str(venv_py) if venv_py.is_file() else sys.executable

    env = dict(os.environ)
    env["OPSB_ACCOUNT_ID"] = safe_aid
    env["OPSB_SUBMIT_TRACE"] = str(workspace / "results" / title / sha / "submit-trace.log")
    if session_file:
        env["JUDGE_SESSION_FILE"] = session_file

    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)], stdin=subprocess.DEVNULL, capture_output=True)
    added = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), sha], stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if added.returncode != 0:
        raise RuntimeError(f"worktree add failed: {added.stderr.strip()}")
    try:
        from ..config import SUPERVISOR_ROOT
        runner = SUPERVISOR_ROOT / "runtime" / "submit-validated.py"
        if not runner.is_file():
            runner = workspace / "tools" / "submit-validated.py"
        res = subprocess.run(
            [py_exe, str(runner), title, "--workspace", str(workspace), "--sha", sha, "--project-dir", str(worktree), *(["--force"] if force else [])],
            stdin=subprocess.DEVNULL,
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
        )
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)], stdin=subprocess.DEVNULL, capture_output=True)
    last_line = (res.stdout or "").strip().splitlines()[-1] if (res.stdout or "").strip() else ""
    if res.returncode == 75:
        try:
            signal = json.loads(last_line)
        except ValueError:
            signal = {}
        raise SubmitRateLimited(int(signal.get("retry_after") or 90), str(signal.get("message") or ""))
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip())
    if not last_line:
        # rc=0 with no stdout used to surface as "Expecting value: line 1 column 1", which
        # told us nothing; keep the exit code and both streams instead.
        raise RuntimeError(
            "submit-validated.py exited 0 without a JSON receipt "
            f"(stderr={ (res.stderr or '')[:400]!r} stdout={(res.stdout or '')[:400]!r})"
        )
    try:
        return json.loads(last_line)
    except ValueError as exc:
        raise RuntimeError(
            f"submit-validated.py printed non-JSON ({exc}): {last_line[:400]!r} "
            f"stderr={(res.stderr or '')[:400]!r}"
        ) from exc


def _wait_and_fetch_score(
    workspace: Path,
    title: str,
    submission_id: str,
    session_file: str = "",
    ranking_retry_delays: tuple[float, ...] = DEFAULT_RANKING_RETRY_DELAYS,
    submitted_at: str = "",
) -> tuple[str, float | None, list[float], list[float]]:
    py_exe = resolve_python_exe(workspace)
    env = dict(os.environ)
    if session_file:
        env["JUDGE_SESSION_FILE"] = session_file
    from ..config import SUPERVISOR_ROOT
    from .board_refresh_service import _resolve_team_name

    team_user_ids = sorted(
        {str(a.get("user_id") or "").strip() for a in list_judge_accounts() if a.get("user_id")}
    )
    team_name = _resolve_team_name(workspace)
    judge_dir = SUPERVISOR_ROOT / "runtime" / "judge"
    if not (judge_dir / "ict_cli.py").is_file():
        judge_dir = workspace / "skills" / "problem-submit" / "scripts"
    poll_code = (
        "import json, os, sys, time, pathlib\n"
        f"root = pathlib.Path({str(workspace)!r})\n"
        f"sys.path.insert(0, {str(CLI_DIR)!r})\n"
        f"sys.path.insert(0, {str(judge_dir)!r})\n"
        "from ict_cli import ICTClient\n"
        "from opsb_core.services.judge_score_fetch import (\n"
        "    extract_numeric_score,\n"
        "    extract_public_times,\n"
        "    fetch_ranking_sid_and_team_with_retry,\n"
        "    resolve_problem_id,\n"
        "    resolve_score_and_times,\n"
        "    update_score_snapshot,\n"
        ")\n"
        "client = ICTClient(session_file=os.environ.get('JUDGE_SESSION_FILE') or None)\n"
        "if not client.load_session(): raise RuntimeError('Judge session expired; sign in via Settings')\n"
        f"deadline = time.monotonic() + {POLL_TIMEOUT_SEC}\n"
        "status = 'Running'\n"
        "sub_detail = {}\n"
        "consec_errs = 0\n"
        "NON_TERM = ('Running', 'Queued', 'Pending', 'Waiting', 'Compiling', 'Building', 'Judging', 'Testing')\n"
        "while time.monotonic() < deadline:\n"
        "    try:\n"
        f"        sub_detail = client.get_submission({submission_id!r}) or {{}}\n"
        "        consec_errs = 0\n"
        "    except Exception as e:\n"
        "        consec_errs += 1\n"
        "        if consec_errs >= 15:\n"
        "            raise RuntimeError(f'get_submission failed {consec_errs}x consecutively: {e}') from e\n"
        "        time.sleep(min(20, 5 + consec_errs))\n"
        "        continue\n"
        "    status = sub_detail.get('status', 'Running')\n"
        "    if status not in NON_TERM: break\n"
        "    time.sleep(10)\n"
        "if status in NON_TERM:\n"
        "    raise RuntimeError(f'poll timeout after {POLL_TIMEOUT_SEC}s (last status={status})')\n"
        "rank_row = None\n"
        "team_best_row = None\n"
        f"team_uids = set({team_user_ids!r})\n"
        f"team_name = {team_name!r}\n"
        "try:\n"
        f"    pid = resolve_problem_id(root, {title!r})\n"
        "    if pid:\n"
        "        def _fetch_page(page):\n"
        "            for attempt in range(4):\n"
        "                try:\n"
        "                    resp = client.session.get(client.BASE_URL + f'/api/problems/{pid}/ranking', params={'page': page, 'size': 100}, timeout=20)\n"
        "                    return resp.json() if resp.status_code == 200 else {}\n"
        "                except Exception:\n"
        "                    if attempt == 3: return {}\n"
        "                    time.sleep(2 * (attempt + 1))\n"
        "            return {}\n"
        "        rank_row, team_best_row = fetch_ranking_sid_and_team_with_retry(\n"
        f"            _fetch_page, {submission_id!r}, team_uids, team_name=team_name, status=status, retry_delays={tuple(ranking_retry_delays)!r}\n"
        "        )\n"
        "except Exception:\n"
        "    rank_row = None\n"
        "    team_best_row = None\n"
        "score, pub_times, pub_tbest = resolve_score_and_times(sub_detail, rank_row, status)\n"
        "try:\n"
        "    snap_row = team_best_row if team_best_row is not None else rank_row\n"
        "    if snap_row is not None:\n"
        "        snap_sid = str(snap_row.get('submission_id') or snap_row.get('sid') or snap_row.get('_id') or '') or "
        f"{submission_id!r}\n"
        "        snap_sc = extract_numeric_score(snap_row)\n"
        "        if snap_sid == "
        f"{submission_id!r}:\n"
        "            snap_status = status\n"
        "            snap_times, snap_tbest = pub_times, pub_tbest\n"
        "            snap_score = score if snap_sc is None else snap_sc\n"
        "        else:\n"
        "            snap_status = str(snap_row.get('status') or ('Pass' if (snap_sc is not None and snap_sc > 0) else status))\n"
        "            snap_times, snap_tbest = extract_public_times(None, snap_row)\n"
        "            snap_score = snap_sc\n"
        "        update_score_snapshot(\n"
        f"            root, {title!r}, snap_sid, snap_status, snap_score,\n"
        "            snap_row.get('rank'),\n"
        f"            snap_times, snap_tbest, submitted_at={submitted_at!r}, require_current_check=False,\n"
        "        )\n"
        "    elif status == 'Pass' and score is not None:\n"
        "        update_score_snapshot(\n"
        f"            root, {title!r}, {submission_id!r}, status, score,\n"
        f"            None, pub_times, pub_tbest, submitted_at={submitted_at!r}, require_current_check=False,\n"
        "        )\n"
        "except Exception:\n"
        "    pass\n"
        "print(json.dumps({'status': status, 'score': score if status == 'Pass' else 0, 'public_times_us': pub_times, 'public_tbest_us': pub_tbest}))\n"
    )
    proc = subprocess.run([py_exe, "-c", poll_code], stdin=subprocess.DEVNULL, env=env, capture_output=True, text=True, timeout=POLL_TIMEOUT_SEC + 60)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip())
    parsed = json.loads(proc.stdout.strip().splitlines()[-1])
    return (
        parsed["status"],
        parsed.get("score"),
        list(parsed.get("public_times_us") or []),
        list(parsed.get("public_tbest_us") or []),
    )
