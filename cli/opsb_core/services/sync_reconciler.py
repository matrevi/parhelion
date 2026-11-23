"""Reconciliation & deduplication helpers for `sync_error` submission outcomes (V3-013 / V3-015)."""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import threading
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ..config import (
    CACHE_DIR,
    invalidate_ttl_cache,
    parse_iso,
    read_json,
    read_jsonl,
    read_ttl_cache,
    utc_now,
    workspace_id,
    write_ttl_cache,
)

NON_TERMINAL_JUDGE_STATUSES = {
    "Running",
    "Queued",
    "Pending",
    "Waiting",
    "Compiling",
    "Building",
    "Judging",
    "Testing",
    "Created",
    "Init",
}

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

_RECONCILE_LOCK = threading.Lock()
_ACTIVE_WORKSPACES: set[Path] = set()


def is_sync_error_outcome(row: dict[str, Any]) -> bool:
    """Return True if an outcome record represents an interrupted sync carrying a submission_id."""
    sid = str(row.get("submission_id") or "").strip()
    if not sid:
        return False
    st = str(row.get("status") or "").strip()
    if st == "sync_error":
        return True
    detail = str(row.get("detail") or "").lower()
    return st == "rejected" and ("poll error:" in detail or bool(sid))


def needs_score_backfill(row: dict[str, Any]) -> bool:
    """Return True if an outcome is `Pass` with a `submission_id` but missing score."""
    sid = str(row.get("submission_id") or "").strip()
    return bool(sid and row.get("status") == "Pass" and row.get("score") is None)


def collapse_reconciled_outcomes(outcome_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse superseded `sync_error` / `score=None` rows when a later row exists for the same `submission_id`."""
    best_idx_by_sid: dict[str, int] = {}
    best_rank_by_sid: dict[str, tuple[int, int]] = {}

    for idx, row in enumerate(outcome_rows):
        sid = str(row.get("submission_id") or "").strip()
        if not sid:
            continue
        st = str(row.get("status") or "").strip()
        has_verdict = 1 if (st in TERMINAL_JUDGE_STATUSES and not is_sync_error_outcome(row)) else 0
        has_score = 1 if (row.get("score") is not None) else 0
        rank = (has_verdict, has_score)
        prev = best_rank_by_sid.get(sid)
        if prev is None or rank >= prev:
            best_rank_by_sid[sid] = rank
            best_idx_by_sid[sid] = idx

    collapsed: list[dict[str, Any]] = []
    for idx, row in enumerate(outcome_rows):
        sid = str(row.get("submission_id") or "").strip()
        if sid and best_idx_by_sid.get(sid) != idx:
            continue
        collapsed.append(row)
    return collapsed


def has_pending_sync_error_for_commit(
    outcome_rows: list[dict[str, Any]],
    title: str,
    sha: str,
) -> dict[str, Any] | None:
    """Return the latest unreconciled `sync_error` row for `(title, sha[:7])` if one exists."""
    prefix = (sha or "")[:7]
    if not title or not prefix:
        return None
    for row in reversed(collapse_reconciled_outcomes(outcome_rows)):
        if row.get("title") == title and str(row.get("sha") or "").startswith(prefix):
            if is_sync_error_outcome(row):
                return row
    return None


def find_unreconciled_sync_errors(
    outcome_rows: list[dict[str, Any]],
    max_age_hours: float = 72.0,
) -> list[dict[str, Any]]:
    """Find unreconciled `sync_error` (or scoreless `Pass`) records carrying a `submission_id`."""
    now = utc_now()
    collapsed = collapse_reconciled_outcomes(outcome_rows)
    out: list[dict[str, Any]] = []
    seen_sids: set[str] = set()
    for row in reversed(collapsed):
        sid = str(row.get("submission_id") or "").strip()
        if not sid or sid in seen_sids:
            continue
        if not (is_sync_error_outcome(row) or needs_score_backfill(row)):
            continue
        if max_age_hours > 0:
            dt = parse_iso(row.get("submitted_at") or row.get("finished_at") or row.get("queued_at"))
            if dt and (now - dt).total_seconds() > max_age_hours * 3600.0:
                continue
        seen_sids.add(sid)
        out.append(row)
    return out


def _read_cookie_session(session_file: Path) -> tuple[str, str, str] | None:
    if not session_file.is_file():
        return None
    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    base = str(data.get("base_url") or "https://judge.invalid").rstrip("/")
    uid = str(data.get("user_id") or "")
    cookies = [
        f"{rec['name']}={rec['value']}"
        for rec in (data.get("cookies") or [])
        if isinstance(rec, dict) and rec.get("name") and rec.get("value") is not None
    ]
    if not cookies:
        return None
    return base, "; ".join(cookies), uid


def _resolve_sessions_for_account(account_id: str) -> list[tuple[str, str, str]]:
    from .auth_service import list_judge_accounts

    sessions: list[tuple[str, str, str]] = []
    seen_files: set[str] = set()
    try:
        accounts = list_judge_accounts()
    except Exception:
        accounts = []
    ordered = sorted(accounts, key=lambda a: 0 if a.get("id") == account_id else 1)
    for acc in ordered:
        if not acc.get("logged_in"):
            continue
        sf = str(acc.get("session_file") or "")
        if not sf or sf in seen_files:
            continue
        seen_files.add(sf)
        parsed = _read_cookie_session(Path(sf))
        if parsed:
            sessions.append(parsed)
    return sessions


def _fetch_json(base_url: str, cookie: str, path: str, timeout: float = 5.0) -> Any:
    req = urllib.request.Request(
        base_url + path,
        headers={
            "Accept": "application/json",
            "Cookie": cookie,
            "User-Agent": "opsb-sync-reconciler/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _resolve_problem_id(workspace: Path, title: str) -> str | None:
    for cand in (workspace / "opsb.workspace.json", workspace / "bench" / "problems.json"):
        data = read_json(cand)
        probs = data.get("problems") if isinstance(data, dict) else data
        if isinstance(probs, list):
            for p in probs:
                if isinstance(p, dict) and p.get("title") == title and p.get("problem_id"):
                    return str(p["problem_id"])
    return None


def _is_latest_submission_for_title(
    outcome_rows: list[dict[str, Any]],
    workspace: Path,
    title: str,
    sid: str,
) -> bool:
    latest_sid = ""
    latest_ts = -1.0
    for row in outcome_rows:
        if row.get("title") != title or not row.get("submission_id"):
            continue
        dt = parse_iso(row.get("submitted_at") or row.get("finished_at"))
        ts = dt.timestamp() if dt else 0.0
        if ts >= latest_ts:
            latest_ts = ts
            latest_sid = str(row["submission_id"])
    for r_file in (workspace / "results" / title).glob("*/submission-receipt.json") if (workspace / "results" / title).is_dir() else []:
        rd = read_json(r_file, {})
        if isinstance(rd, dict) and rd.get("submission_id"):
            dt = parse_iso(rd.get("submitted_at"))
            ts = dt.timestamp() if dt else 0.0
            if ts > latest_ts:
                latest_ts = ts
                latest_sid = str(rd["submission_id"])
    return not latest_sid or latest_sid == sid


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _update_snapshot_if_current(
    workspace: Path,
    title: str,
    sid: str,
    status: str,
    score: float | None,
    rank: Any,
    pub_times: list[float],
    pub_tbest: list[float],
    submitted_at: str,
) -> None:
    snap_path = workspace / "results" / "score-snapshot.json"
    if not snap_path.is_file():
        return
    snap = read_json(snap_path, {})
    if not isinstance(snap, dict):
        return
    prev = snap.get(title) if isinstance(snap.get(title), dict) else {}
    prev_sid = str(prev.get("submission_id") or "")
    prev_dt = parse_iso(prev.get("submission_time"))
    cur_dt = parse_iso(submitted_at)
    should_update = (prev_sid == sid) or (not prev_dt) or (cur_dt is not None and cur_dt >= prev_dt)
    if not should_update:
        return
    snap[title] = {
        **prev,
        "rank": rank if rank is not None else prev.get("rank"),
        "status": status,
        "submission_id": sid,
        "score": (score if score is not None else prev.get("score")) if status == "Pass" else 0,
        "public_times_us": pub_times if pub_times else prev.get("public_times_us", []),
        **({"public_tbest_us": pub_tbest} if pub_tbest else {}),
        **({"submission_time": submitted_at} if submitted_at and not prev.get("submission_time") else {}),
    }
    _atomic_write_json(snap_path, snap)


def _fetch_ranking_row(
    base_url: str,
    cookie: str,
    problem_id: str,
    sid: str,
    user_id: str,
    is_latest: bool,
    timeout: float,
) -> tuple[dict[str, Any] | None, bool]:
    """Return `(matched_row, row_belongs_to_sid)` from problem ranking pages."""
    fallback_row: dict[str, Any] | None = None
    for page in range(1, 8):
        try:
            payload = _fetch_json(base_url, cookie, f"/api/problems/{problem_id}/ranking?page={page}&size=100", timeout=timeout)
        except Exception:
            break
        if not isinstance(payload, dict):
            break
        rows = payload.get("rows") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            r_sid = str(row.get("submission_id") or "")
            if r_sid and r_sid == sid:
                return row, True
            if user_id and str(row.get("user_id") or "") == user_id:
                fallback_row = row
        if page >= int(payload.get("pages") or 1):
            break
    if fallback_row is not None:
        r_sid = str(fallback_row.get("submission_id") or "")
        belongs = (not r_sid and is_latest) or (r_sid == sid)
        return fallback_row, belongs
    return None, False


def reconcile_sync_errors(
    workspace: Path,
    *,
    force: bool = False,
    max_age_hours: float = 72.0,
    ttl_sec: float = 45.0,
    request_timeout: float = 5.0,
    max_items: int = 6,
) -> list[dict[str, Any]]:
    """Query Judge for unreconciled `sync_error` outcomes and backfill terminal results."""
    results_dir = workspace / "results"
    outcome_path = results_dir / "submit-queue-outcome.jsonl"
    if not outcome_path.is_file():
        return []

    outcome_rows = read_jsonl(outcome_path)
    candidates = find_unreconciled_sync_errors(outcome_rows, max_age_hours=max_age_hours)
    if not candidates:
        return []

    reconciled: list[dict[str, Any]] = []
    io_lock_path = results_dir / ".submit-queue-io.lock"

    for orig in candidates[:max_items]:
        sid = str(orig.get("submission_id") or "").strip()
        title = str(orig.get("title") or "").strip()
        if not sid or not title:
            continue
        cache_key = f"sync_reconcile_{sid[:12]}"
        if not force and read_ttl_cache(cache_key, ttl_sec=ttl_sec, fingerprint=sid) is not None:
            continue

        aid = str(orig.get("account_id") or "primary")
        sessions = _resolve_sessions_for_account(aid)
        if not sessions:
            continue
        write_ttl_cache(cache_key, {"checked_at": utc_now().isoformat()}, fingerprint=sid)

        sub_detail: dict[str, Any] | None = None
        used_session: tuple[str, str, str] | None = None
        for base_url, cookie, uid in sessions:
            try:
                raw_sub = _fetch_json(base_url, cookie, f"/api/submissions/{sid}", timeout=request_timeout)
                body = raw_sub.get("data") if isinstance(raw_sub, dict) and isinstance(raw_sub.get("data"), dict) else raw_sub
                if isinstance(body, dict) and body.get("status"):
                    sub_detail = body
                    used_session = (base_url, cookie, uid)
                    break
            except Exception:
                continue

        if not sub_detail or not used_session:
            continue

        status = str(sub_detail.get("status") or "").strip()
        if not status or status in NON_TERMINAL_JUDGE_STATUSES or status not in TERMINAL_JUDGE_STATUSES:
            continue

        sub_res = sub_detail.get("result") or []
        pub_times = [float(r["time"]) for r in sub_res if isinstance(r, dict) and r.get("time") is not None][:5]
        pub_tbest = [float(r["best_time"]) for r in sub_res if isinstance(r, dict) and r.get("best_time") is not None][:5]

        score: float | None = 0.0 if status != "Pass" else None
        rank_row: dict[str, Any] | None = None
        belongs_to_sid = False
        pid = _resolve_problem_id(workspace, title)
        is_latest = _is_latest_submission_for_title(outcome_rows, workspace, title, sid)

        if pid:
            base_url, cookie, uid = used_session
            rank_row, belongs_to_sid = _fetch_ranking_row(base_url, cookie, pid, sid, uid, is_latest, request_timeout)
            if rank_row is not None and belongs_to_sid:
                if status == "Pass" and rank_row.get("score") is not None:
                    score = float(rank_row["score"])
                if not pub_times:
                    rr_res = rank_row.get("result") or []
                    if rr_res and isinstance(rr_res[0], dict):
                        pub_times = [float(x["time"]) for x in rr_res if x.get("time") is not None][:5]
                    else:
                        pub_times = [float(t) for t in (rank_row.get("times") or [])[:5] if t is not None]

        # Avoid appending a second scoreless `Pass` row if this item was already `Pass(score=None)`
        if needs_score_backfill(orig) and score is None:
            continue

        submitted_at = str(orig.get("submitted_at") or orig.get("finished_at") or utc_now().isoformat())
        new_row = {
            **orig,
            "finished_at": orig.get("finished_at") or utc_now().isoformat(),
            "submitted_at": submitted_at,
            "status": status,
            "score": score if status == "Pass" else 0,
            "reconciled_at": utc_now().isoformat(),
            "reconciled_from": orig.get("status") or "sync_error",
        }
        new_row.pop("detail", None)
        if pub_times:
            new_row["public_times_us"] = pub_times
        if pub_tbest:
            new_row["public_tbest_us"] = pub_tbest

        with io_lock_path.open("a") as iolf:
            fcntl.flock(iolf, fcntl.LOCK_EX)
            try:
                latest_rows = read_jsonl(outcome_path)
                still_pending = any(
                    str(r.get("submission_id") or "") == sid and (is_sync_error_outcome(r) or needs_score_backfill(r))
                    for r in collapse_reconciled_outcomes(latest_rows)
                )
                if still_pending:
                    with outcome_path.open("a", encoding="utf-8") as of:
                        of.write(json.dumps(new_row, ensure_ascii=False) + "\n")
                    if rank_row is not None and belongs_to_sid:
                        _update_snapshot_if_current(
                            workspace,
                            title,
                            sid,
                            status,
                            score,
                            rank_row.get("rank"),
                            pub_times,
                            pub_tbest,
                            submitted_at,
                        )
                    reconciled.append(new_row)
            finally:
                fcntl.flock(iolf, fcntl.LOCK_UN)

    if reconciled:
        invalidate_ttl_cache("scheduler_bridge")
        invalidate_ttl_cache("remote_judge")
    return reconciled


def reconcile_sync_errors_async(
    workspace: Path,
    *,
    on_reconciled: Callable[[], None] | None = None,
    ttl_sec: float = 45.0,
) -> bool:
    """Spawn a single non-blocking daemon thread per workspace when unreconciled `sync_error`s exist."""
    if os.environ.get("OPSB_DISABLE_SYNC_RECONCILE") == "1":
        return False
    ws = workspace.expanduser().resolve()
    outcome_path = ws / "results" / "submit-queue-outcome.jsonl"
    if not outcome_path.is_file():
        return False
    candidates = find_unreconciled_sync_errors(read_jsonl(outcome_path))
    if not candidates:
        return False

    has_unchecked = any(
        read_ttl_cache(f"sync_reconcile_{str(c.get('submission_id') or '')[:12]}", ttl_sec=ttl_sec, fingerprint=str(c.get("submission_id") or "")) is None
        for c in candidates
    )
    if not has_unchecked:
        return False

    with _RECONCILE_LOCK:
        if ws in _ACTIVE_WORKSPACES:
            return False
        _ACTIVE_WORKSPACES.add(ws)

    def _worker() -> None:
        try:
            lock_f = CACHE_DIR / f"sync_reconcile_{workspace_id(ws)}.lock"
            lock_f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with lock_f.open("a") as lf:
                try:
                    fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                try:
                    fixed = reconcile_sync_errors(ws, ttl_sec=ttl_sec)
                    if fixed and on_reconciled is not None:
                        on_reconciled()
                finally:
                    fcntl.flock(lf, fcntl.LOCK_UN)
        except Exception:
            pass
        finally:
            with _RECONCILE_LOCK:
                _ACTIVE_WORKSPACES.discard(ws)

    threading.Thread(target=_worker, daemon=True, name="opsb-sync-reconcile").start()
    return True
