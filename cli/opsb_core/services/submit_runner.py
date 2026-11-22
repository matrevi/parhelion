"""Single-shot jump-queue submission worker & live in-flight detector."""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..config import CONFIG_DIR, invalidate_ttl_cache, parse_iso, read_json, read_jsonl, resolve_python_exe, utc_now
from .judge_sync import INFLIGHT_FILE

MIN_RATE_GAP_SEC = 190
POLL_TIMEOUT_SEC = 1800


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


def spawn_jump_submit(opsb_bin: Path, workspace: Path, title: str, sha: str, note: str) -> dict[str, Any]:
    lock_file = workspace / "results" / ".submit-queue.lock"
    if lock_file.exists():
        try:
            with lock_file.open("r") as lf:
                try:
                    fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(lf, fcntl.LOCK_UN)
                except BlockingIOError:
                    return {"ok": False, "busy": True, "error": "上一发正在提交评测中，请等待完成后再试"}
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
                "--workspace",
                str(workspace),
            ],
            start_new_session=True,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )

    INFLIGHT_FILE.write_text(
        json.dumps(
            {
                "pid": proc.pid,
                "workspace": str(workspace),
                "title": title,
                "sha": sha,
                "started_at": utc_now().isoformat(),
                "phase": "packaging",
            }
        ),
        encoding="utf-8",
    )
    invalidate_ttl_cache("scheduler_bridge")
    invalidate_ttl_cache("remote_judge")
    return {"ok": True, "started": True, "pid": proc.pid, "title": title, "sha": sha}


def _write_inflight_phase(title: str, sha: str, workspace: Path, started_iso: str, phase: str) -> None:
    try:
        CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        INFLIGHT_FILE.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "workspace": str(workspace),
                    "title": title,
                    "sha": sha,
                    "started_at": started_iso,
                    "phase": phase,
                }
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def run_worker_submit_one(workspace: Path, title: str, sha: str, note: str) -> int:
    """Detached consumer holding `.submit-queue.lock` exclusively for one jump-queued submission."""
    results_dir = workspace / "results"
    lock_path = results_dir / ".submit-queue.lock"
    queue_path = results_dir / "submit-queue.jsonl"
    outcome_path = results_dir / "submit-queue-outcome.jsonl"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    started_iso = utc_now().isoformat()
    with lock_path.open("a") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 2

        try:
            _write_inflight_phase(title, sha, workspace, started_iso, "packaging")

            # Reuse existing pending queue entry if present, otherwise append audit record
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

            # Respect 190s platform rate-limit across ALL receipts AND outcomes
            last_epoch = 0.0
            for r_file in results_dir.glob("*/*/submission-receipt.json"):
                rd = read_json(r_file, {})
                dt = parse_iso(rd.get("submitted_at"))
                if dt:
                    last_epoch = max(last_epoch, dt.timestamp())
            for o in read_jsonl(outcome_path):
                if o.get("status") not in ("rejected", "skipped", "duplicate") and o.get("submission_id"):
                    dt = parse_iso(o.get("submitted_at") or o.get("finished_at"))
                    if dt:
                        last_epoch = max(last_epoch, dt.timestamp())
            if last_epoch > 0:
                wait_s = MIN_RATE_GAP_SEC - (utc_now().timestamp() - last_epoch)
                if wait_s > 0:
                    _write_inflight_phase(title, sha, workspace, started_iso, "waiting_cooldown")
                    time.sleep(wait_s)

            _write_inflight_phase(title, sha, workspace, started_iso, "uploading")
            receipt = _submit_via_worktree(workspace, title, sha)
            invalidate_ttl_cache("remote_judge")
            if receipt.get("existing"):
                out_rec = {**record, "finished_at": utc_now().isoformat(), "status": "duplicate", "submission_id": receipt["submission_id"]}
            else:
                _write_inflight_phase(title, sha, workspace, started_iso, "judging")
                sub_id = receipt["submission_id"]
                status, score = _wait_and_fetch_score(workspace, title, sub_id)
                out_rec = {
                    **record,
                    "finished_at": utc_now().isoformat(),
                    "status": status,
                    "submission_id": sub_id,
                    "score": score,
                    "submitted_at": receipt.get("submitted_at") or utc_now().isoformat(),
                }
        except Exception as exc:
            out_rec = {**record, "finished_at": utc_now().isoformat(), "status": "rejected", "detail": str(exc)[:400]}
        finally:
            INFLIGHT_FILE.unlink(missing_ok=True)

        with outcome_path.open("a", encoding="utf-8") as of:
            of.write(json.dumps(out_rec, ensure_ascii=False) + "\n")
        invalidate_ttl_cache("scheduler_bridge")
        fcntl.flock(lf, fcntl.LOCK_UN)

    # Closed-loop calibration + auto-archive into `breakthrough/` on terminal evaluation
    if out_rec.get("status") not in ("rejected", "skipped", "duplicate"):
        try:
            from ..config import SUPERVISOR_ROOT

            py_exe = resolve_python_exe(workspace)
            subprocess.run(
                [py_exe, "-m", "scheduler.cli_bridge", "calibrate", "--workspace", str(workspace), "--title", title],
                cwd=str(SUPERVISOR_ROOT),
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


def _submit_via_worktree(workspace: Path, title: str, sha: str) -> dict[str, Any]:
    problems = read_json(workspace / "bench" / "problems.json", [])
    p_meta = next((p for p in problems if p["title"] == title), {"path": f"bench/{title}"})
    repo = workspace / p_meta["path"]
    worktree = Path("/tmp") / f"opsb-submit-{title}"
    venv_py = workspace / ".venv" / "bin" / "python"
    py_exe = str(venv_py) if venv_py.is_file() else sys.executable

    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)], capture_output=True)
    added = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), sha], capture_output=True, text=True)
    if added.returncode != 0:
        raise RuntimeError(f"worktree add failed: {added.stderr.strip()}")
    try:
        res = subprocess.run(
            [py_exe, str(workspace / "tools" / "submit-validated.py"), title, "--sha", sha, "--project-dir", str(worktree), "--force"],
            capture_output=True,
            text=True,
            timeout=90,
        )
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)], capture_output=True)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip())
    return json.loads(res.stdout.strip().splitlines()[-1])


def _wait_and_fetch_score(workspace: Path, title: str, submission_id: str) -> tuple[str, float | None]:
    py_exe = resolve_python_exe(workspace)
    poll_code = (
        "import json, sys, time, pathlib\n"
        f"root = pathlib.Path({str(workspace)!r})\n"
        "sys.path.insert(0, str(root / 'skills/problem-submit/scripts'))\n"
        "from ict_cli import ICTClient\n"
        "client = ICTClient()\n"
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
    proc = subprocess.run([py_exe, "-c", poll_code], capture_output=True, text=True, timeout=POLL_TIMEOUT_SEC + 60)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip())
    parsed = json.loads(proc.stdout.strip().splitlines()[-1])
    return parsed["status"], parsed.get("score")
