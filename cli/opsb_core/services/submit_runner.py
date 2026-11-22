"""Single-shot jump-queue submission worker & live in-flight detector."""
from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


class _CooldownReservationCancelled(BaseException):
    """Raised when a sleeping `waiting_cooldown` worker is cleanly replaced by another jump-submit."""

from ..config import CONFIG_DIR, invalidate_ttl_cache, parse_iso, read_json, read_jsonl, resolve_python_exe, utc_now
from .auth_service import list_judge_accounts
from .judge_sync import INFLIGHT_FILE

MIN_RATE_GAP_SEC = 190
POLL_TIMEOUT_SEC = 1800


def inflight_file_for(account_id: str) -> Path:
    return INFLIGHT_FILE if account_id == "primary" else (CONFIG_DIR / f"inflight-{account_id}.json")


def lock_file_for(workspace: Path, account_id: str) -> Path:
    res = workspace / "results"
    return (res / ".submit-queue.lock") if account_id == "primary" else (res / f".submit-queue-{account_id}.lock")


def compute_account_slots(workspace: Path, outcome_rows: list[dict[str, Any]] | None = None, gap_sec: int = MIN_RATE_GAP_SEC) -> list[dict[str, Any]]:
    """Compute per-teammate-account independent 190s cooldown & live worker status."""
    results_dir = workspace / "results"
    if outcome_rows is None:
        outcome_rows = read_jsonl(results_dir / "submit-queue-outcome.jsonl")
    accounts = list_judge_accounts()
    uid_to_aid = {str(a.get("user_id")): str(a["id"]) for a in accounts if a.get("user_id")}

    last_epoch_by_aid: dict[str, float] = {str(a["id"]): 0.0 for a in accounts}
    for r_file in results_dir.glob("*/*/submission-receipt.json"):
        rd = read_json(r_file, {})
        dt = parse_iso(rd.get("submitted_at"))
        if not dt:
            continue
        aid = str(rd.get("account_id") or "")
        if not aid or aid not in last_epoch_by_aid:
            aid = uid_to_aid.get(str(rd.get("user_id") or ""), "primary")
        if aid in last_epoch_by_aid:
            last_epoch_by_aid[aid] = max(last_epoch_by_aid[aid], dt.timestamp())

    for o in outcome_rows:
        if o.get("status") not in ("rejected", "skipped", "duplicate") and o.get("submission_id"):
            dt = parse_iso(o.get("submitted_at") or o.get("finished_at"))
            if not dt:
                continue
            aid = str(o.get("account_id") or "primary")
            if aid not in last_epoch_by_aid:
                aid = "primary"
            last_epoch_by_aid[aid] = max(last_epoch_by_aid[aid], dt.timestamp())

    now_ep = utc_now().timestamp()
    slots: list[dict[str, Any]] = []
    for a in accounts:
        aid = str(a["id"])
        last_ep = last_epoch_by_aid.get(aid, 0.0)
        ends_ep = (last_ep + gap_sec) if last_ep > 0 else 0.0
        rem = max(0, int(round(ends_ep - now_ep)))
        inf_path = inflight_file_for(aid)
        phase = "idle"
        active_title = None
        active_sha = None
        worker_pid = 0
        if inf_path.is_file():
            inf = read_json(inf_path, {})
            pid = int(inf.get("pid") or 0)
            if pid > 0 and _pid_alive(pid):
                phase = str(inf.get("phase") or "packaging")
                active_title = inf.get("title")
                active_sha = inf.get("sha")
                worker_pid = pid
            else:
                inf_path.unlink(missing_ok=True)
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
                "phase": phase,
                "active_title": active_title,
                "active_sha": active_sha,
                "worker_pid": worker_pid,
            }
        )
    return slots


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
    if INFLIGHT_FILE.is_file():
        data = read_json(INFLIGHT_FILE, {})
        pid = int(data.get("pid") or 0)
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
            INFLIGHT_FILE.unlink(missing_ok=True)

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


def spawn_jump_submit(
    opsb_bin: Path,
    workspace: Path,
    title: str,
    sha: str,
    note: str,
    account_id: str | None = None,
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
    outcome_sids = {
        str(o.get("submission_id"))
        for o in read_jsonl(results_dir / "submit-queue-outcome.jsonl")
        if o.get("submission_id") and o.get("status") not in ("Running", "Queued", "Pending")
    }
    now_ep = utc_now().timestamp()
    for r_file in (results_dir / title).glob(f"{sha[:7]}*/submission-receipt.json") if (results_dir / title).is_dir() else []:
        rd = read_json(r_file, {})
        sid = str(rd.get("submission_id") or "")
        dt = parse_iso(rd.get("submitted_at"))
        if sid and sid not in outcome_sids and dt and (now_ep - dt.timestamp()) < 2100:
            return {
                "ok": False,
                "duplicate_inflight": True,
                "error": f"{title} ({sha[:7]}) 已在远程判题中 (sid={sid[:8]})，禁止重复碰撞提交",
            }

    eligible = [s for s in slots if s["enabled"] and s["logged_in"]]
    if not eligible:
        eligible = [s for s in slots if s["id"] == "primary"]

    chosen: dict[str, Any] | None = None
    if account_id:
        chosen = next((s for s in eligible if s["id"] == account_id), None)
    if chosen is None:
        # 1. Idle & cooldown ready (0s wait)
        ready_idle = [s for s in eligible if s["phase"] == "idle" and s["cooldown_remaining_sec"] == 0]
        if ready_idle:
            chosen = ready_idle[0]
        else:
            # 2. Idle with shortest remaining cooldown
            idle_any = sorted([s for s in eligible if s["phase"] == "idle"], key=lambda x: x["cooldown_remaining_sec"])
            if idle_any:
                chosen = idle_any[0]
            else:
                # 3. Slot sleeping in waiting_cooldown (can cleanly replace reservation)
                waiting = sorted([s for s in eligible if s["phase"] == "waiting_cooldown"], key=lambda x: x["cooldown_remaining_sec"])
                if waiting:
                    chosen = waiting[0]
                else:
                    chosen = eligible[0]

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
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_path = CONFIG_DIR / "last_jump_submit.log"
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
            ],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )

    inf_path.write_text(
        json.dumps(
            {
                "pid": proc.pid,
                "workspace": str(workspace),
                "title": title,
                "sha": sha,
                "account_id": aid,
                "account_label": chosen["label"],
                "started_at": utc_now().isoformat(),
                "phase": "packaging",
            }
        ),
        encoding="utf-8",
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
        CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        inflight_file_for(account_id).write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "workspace": str(workspace),
                    "title": title,
                    "sha": sha,
                    "account_id": account_id,
                    "account_label": account_label,
                    "started_at": started_iso,
                    "phase": phase,
                }
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def run_worker_submit_one(workspace: Path, title: str, sha: str, note: str, account_id: str = "primary") -> int:
    """Detached consumer: holds per-account lock ONLY for cooldown wait + local pack/upload.

    Each enabled teammate account obeys its own independent 190s platform cooldown and session file.
    """
    results_dir = workspace / "results"
    aid = (account_id or "primary").strip() or "primary"
    accounts = list_judge_accounts()
    slot_meta = next((a for a in accounts if a.get("id") == aid), {"id": aid, "label": "主账号", "session_file": ""})
    acc_label = str(slot_meta.get("label") or aid)
    session_file = str(slot_meta.get("session_file") or "")
    uid_to_aid = {str(a.get("user_id")): str(a["id"]) for a in accounts if a.get("user_id")}

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

    # Phase 1 (locked per account): claim queue record, wait THIS account's 190s cooldown, pack & upload
    with lock_path.open("a") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 2

        try:
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
                            "force": True,
                        }
                        with queue_path.open("a", encoding="utf-8") as qf:
                            qf.write(json.dumps(record, ensure_ascii=False) + "\n")
                finally:
                    fcntl.flock(iolf, fcntl.LOCK_UN)

            # Respect THIS account's 190s platform rate-limit across receipts AND outcomes
            last_epoch = 0.0
            for r_file in results_dir.glob("*/*/submission-receipt.json"):
                rd = read_json(r_file, {})
                r_aid = str(rd.get("account_id") or "") or uid_to_aid.get(str(rd.get("user_id") or ""), "primary")
                if r_aid != aid:
                    continue
                dt = parse_iso(rd.get("submitted_at"))
                if dt:
                    last_epoch = max(last_epoch, dt.timestamp())
            for o in read_jsonl(outcome_path):
                if o.get("status") not in ("rejected", "skipped", "duplicate") and o.get("submission_id"):
                    o_aid = str(o.get("account_id") or "primary")
                    if o_aid != aid:
                        continue
                    dt = parse_iso(o.get("submitted_at") or o.get("finished_at"))
                    if dt:
                        last_epoch = max(last_epoch, dt.timestamp())
            if last_epoch > 0:
                wait_s = MIN_RATE_GAP_SEC - (utc_now().timestamp() - last_epoch)
                if wait_s > 0:
                    _write_inflight_phase(title, sha, workspace, started_iso, "waiting_cooldown", aid, acc_label)
                    prev_handler = signal.getsignal(signal.SIGTERM)

                    def _on_cancel_wait(_sig: int, _frame: Any) -> None:
                        raise _CooldownReservationCancelled()

                    signal.signal(signal.SIGTERM, _on_cancel_wait)
                    try:
                        time.sleep(wait_s)
                    finally:
                        signal.signal(signal.SIGTERM, prev_handler)

            _write_inflight_phase(title, sha, workspace, started_iso, "uploading", aid, acc_label)
            receipt = _submit_via_worktree(workspace, title, sha, account_id=aid, session_file=session_file)
            invalidate_ttl_cache("remote_judge")
            invalidate_ttl_cache("scheduler_bridge")
            if receipt.get("existing"):
                early_out_rec = {
                    **record,
                    "finished_at": utc_now().isoformat(),
                    "status": "duplicate",
                    "submission_id": receipt["submission_id"],
                    "account_id": aid,
                    "account_label": acc_label,
                }
                with io_lock_path.open("a") as iolf:
                    fcntl.flock(iolf, fcntl.LOCK_EX)
                    try:
                        with outcome_path.open("a", encoding="utf-8") as of:
                            of.write(json.dumps(early_out_rec, ensure_ascii=False) + "\n")
                    finally:
                        fcntl.flock(iolf, fcntl.LOCK_UN)
                invalidate_ttl_cache("scheduler_bridge")
        except _CooldownReservationCancelled:
            early_out_rec = {"cancelled": True}
            invalidate_ttl_cache("scheduler_bridge")
        except Exception as exc:
            base_rec = record or {"title": title, "sha": sha, "note": note, "queued_at": started_iso}
            early_out_rec = {
                **base_rec,
                "finished_at": utc_now().isoformat(),
                "status": "rejected",
                "account_id": aid,
                "account_label": acc_label,
                "detail": str(exc)[:400],
            }
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

    # Phase 2 (lock-free): poll remote Judge to terminal status using the submitting account's session
    sub_id = receipt["submission_id"]
    try:
        status, score = _wait_and_fetch_score(workspace, title, sub_id, session_file=session_file)
        out_rec = {
            **record,
            "finished_at": utc_now().isoformat(),
            "status": status,
            "submission_id": sub_id,
            "score": score,
            "account_id": aid,
            "account_label": acc_label,
            "submitted_at": receipt.get("submitted_at") or utc_now().isoformat(),
        }
    except Exception as exc:
        out_rec = {
            **record,
            "finished_at": utc_now().isoformat(),
            "status": "rejected",
            "submission_id": sub_id,
            "account_id": aid,
            "account_label": acc_label,
            "detail": f"poll error: {str(exc)[:360]}",
        }

    # Briefly lock IO only to append terminal outcome atomically
    with io_lock_path.open("a") as iolf:
        fcntl.flock(iolf, fcntl.LOCK_EX)
        try:
            with outcome_path.open("a", encoding="utf-8") as of:
                of.write(json.dumps(out_rec, ensure_ascii=False) + "\n")
            invalidate_ttl_cache("scheduler_bridge")
            invalidate_ttl_cache("remote_judge")
        finally:
            fcntl.flock(iolf, fcntl.LOCK_UN)

    # Closed-loop calibration + auto-archive into `breakthrough/` on terminal evaluation
    if out_rec.get("status") not in ("rejected", "skipped", "duplicate"):
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


def _submit_via_worktree(
    workspace: Path,
    title: str,
    sha: str,
    account_id: str = "primary",
    session_file: str = "",
) -> dict[str, Any]:
    problems = read_json(workspace / "bench" / "problems.json", [])
    p_meta = next((p for p in problems if p["title"] == title), {"path": f"bench/{title}"})
    repo = workspace / p_meta["path"]
    safe_aid = re.sub(r"[^A-Za-z0-9_-]", "", account_id or "primary") or "primary"
    worktree = Path("/tmp") / f"opsb-submit-{title}-{safe_aid}"
    venv_py = workspace / ".venv" / "bin" / "python"
    py_exe = str(venv_py) if venv_py.is_file() else sys.executable

    env = dict(os.environ)
    env["OPSB_ACCOUNT_ID"] = safe_aid
    if session_file:
        env["JUDGE_SESSION_FILE"] = session_file

    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)], stdin=subprocess.DEVNULL, capture_output=True)
    added = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), sha], stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if added.returncode != 0:
        raise RuntimeError(f"worktree add failed: {added.stderr.strip()}")
    try:
        res = subprocess.run(
            [py_exe, str(workspace / "tools" / "submit-validated.py"), title, "--sha", sha, "--project-dir", str(worktree), "--force"],
            stdin=subprocess.DEVNULL,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)], stdin=subprocess.DEVNULL, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip())
    return json.loads(res.stdout.strip().splitlines()[-1])


def _wait_and_fetch_score(workspace: Path, title: str, submission_id: str, session_file: str = "") -> tuple[str, float | None]:
    py_exe = resolve_python_exe(workspace)
    env = dict(os.environ)
    if session_file:
        env["JUDGE_SESSION_FILE"] = session_file
    poll_code = (
        "import json, os, sys, time, pathlib\n"
        f"root = pathlib.Path({str(workspace)!r})\n"
        "sys.path.insert(0, str(root / 'skills/problem-submit/scripts'))\n"
        "from ict_cli import ICTClient\n"
        "client = ICTClient(session_file=os.environ.get('JUDGE_SESSION_FILE') or None)\n"
        "if not client.load_session(): raise RuntimeError('Judge session expired; sign in via Settings')\n"
        f"deadline = time.monotonic() + {POLL_TIMEOUT_SEC}\n"
        "status = 'Running'\n"
        "while time.monotonic() < deadline:\n"
        f"    status = client.get_submission({submission_id!r}).get('status', 'Running')\n"
        "    if status not in ('Running', 'Queued', 'Pending'): break\n"
        "    time.sleep(10)\n"
        f"probs = json.loads((root / 'bench/problems.json').read_text())\n"
        f"prob = next(p for p in probs if p['title'] == {title!r})\n"
        "score = None\n"
        "rank_row = None\n"
        "for page in range(1, 8):\n"
        "    payload = client.session.get(client.BASE_URL + f\"/api/problems/{prob['problem_id']}/ranking\", params={'page': page, 'size': 100}, timeout=30).json()\n"
        "    row = next((e for e in payload.get('rows', []) if e.get('user_id') == client.user_id), None)\n"
        "    if row: score = row.get('score'); rank_row = row; break\n"
        "snap_path = root / 'results' / 'score-snapshot.json'\n"
        "if snap_path.is_file() and rank_row is not None:\n"
        "    snap = json.loads(snap_path.read_text())\n"
        f"    prev_entry = snap.get({title!r}) or {{}}\n"
        f"    snap[{title!r}] = {{\n"
        "        **prev_entry,\n"
        "        'rank': rank_row.get('rank'),\n"
        "        'status': status,\n"
        f"        'submission_id': {submission_id!r},\n"
        "        'score': score if status == 'Pass' else 0,\n"
        "        'public_times_us': [t for t in (rank_row.get('times') or [])[:5] if t is not None]\n"
        "    }\n"
        "    snap_path.write_text(json.dumps(snap, indent=2) + '\\n')\n"
        "print(json.dumps({'status': status, 'score': score if status == 'Pass' else 0}))\n"
    )
    proc = subprocess.run([py_exe, "-c", poll_code], stdin=subprocess.DEVNULL, env=env, capture_output=True, text=True, timeout=POLL_TIMEOUT_SEC + 60)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip())
    parsed = json.loads(proc.stdout.strip().splitlines()[-1])
    return parsed["status"], parsed.get("score")
