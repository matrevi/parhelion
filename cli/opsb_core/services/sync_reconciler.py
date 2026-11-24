"""Reconciliation & deduplication helpers for `sync_error`, `Pass(score=None)` & orphan receipts."""
from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

from ..config import (
    CACHE_DIR,
    invalidate_ttl_cache,
    parse_iso,
    read_jsonl,
    read_ttl_cache,
    utc_now,
    workspace_id,
    write_ttl_cache,
)
from .board_refresh_service import _resolve_team_name
from .judge_score_fetch import (
    NON_TERMINAL_JUDGE_STATUSES,
    TERMINAL_JUDGE_STATUSES,
    extract_numeric_score,
    extract_public_times,
    fetch_json_with_cookie,
    fetch_ranking_sid_and_team_with_retry,
    resolve_problem_id,
    resolve_score_and_times,
    update_score_snapshot,
)
from .orphan_receipts import (
    build_reconciled_outcome_row,
    collect_recent_unreconciled_receipt_pairs,
    fetch_submission_via_sessions,
    find_orphan_receipts as _find_orphan_receipts_impl,
    is_latest_submission_for_title,
    resolve_sessions_for_account,
)

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
    best_rank_by_sid: dict[str, tuple[int, int, int]] = {}
    for idx, row in enumerate(outcome_rows):
        sid = str(row.get("submission_id") or "").strip()
        if not sid:
            continue
        st = str(row.get("status") or "").strip()
        has_verdict = 1 if (st in TERMINAL_JUDGE_STATUSES and not is_sync_error_outcome(row)) else 0
        has_score = 1 if (row.get("score") is not None) else 0
        rank = (has_verdict, int(row.get("source") != "judge-reconcile"), has_score)
        prev = best_rank_by_sid.get(sid)
        if prev is None or rank >= prev:
            best_rank_by_sid[sid] = rank
            best_idx_by_sid[sid] = idx
    return [
        row for idx, row in enumerate(outcome_rows)
        if not str(row.get("submission_id") or "").strip()
        or best_idx_by_sid.get(str(row.get("submission_id") or "").strip()) == idx
    ]


def has_pending_sync_error_for_commit(
    outcome_rows: list[dict[str, Any]],
    title: str,
    sha: str,
) -> dict[str, Any] | None:
    """Return the latest unreconciled `sync_error` row for `(title, sha[:7])` if one exists."""
    prefix = (sha or "")[:7].lower()
    if not title or not prefix:
        return None
    for row in reversed(collapse_reconciled_outcomes(outcome_rows)):
        if row.get("title") == title and str(row.get("sha") or "").lower().startswith(prefix):
            if is_sync_error_outcome(row):
                return row
    return None


def find_unreconciled_sync_errors(
    outcome_rows: list[dict[str, Any]],
    max_age_hours: float = 72.0,
) -> list[dict[str, Any]]:
    """Find unreconciled `sync_error` (or scoreless `Pass`) records carrying a `submission_id`."""
    now = utc_now()
    out: list[dict[str, Any]] = []
    seen_sids: set[str] = set()
    for row in reversed(collapse_reconciled_outcomes(outcome_rows)):
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


def find_orphan_receipts(
    workspace: Path,
    outcome_rows: list[dict[str, Any]],
    *,
    max_age_hours: float = 72.0,
    queue_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Discover orphan `submission-receipt.json` files lacking a terminal outcome row for `submission_id`."""
    collapsed = collapse_reconciled_outcomes(outcome_rows)
    known_sids = {
        str(o.get("submission_id") or "").strip()
        for o in collapsed
        if o.get("submission_id")
        and (
            str(o.get("status") or "").strip() in TERMINAL_JUDGE_STATUSES
            or is_sync_error_outcome(o)
            or str(o.get("status") or "").strip() in ("rejected", "duplicate")
        )
    }
    return _find_orphan_receipts_impl(workspace, collapsed, known_sids, max_age_hours=max_age_hours, queue_rows=queue_rows)


def recent_unreconciled_receipt_pairs(
    workspace: Path,
    outcome_rows: list[dict[str, Any]] | None = None,
    *,
    max_age_sec: float = 2100.0,
) -> set[tuple[str, str]]:
    """Return `(title, sha7_lower)` for receipts (< max_age_sec) without a terminal outcome row for their `sid`."""
    outs = outcome_rows if outcome_rows is not None else read_jsonl(workspace / "results" / "submit-queue-outcome.jsonl")
    return collect_recent_unreconciled_receipt_pairs(workspace, collapse_reconciled_outcomes(outs), max_age_sec=max_age_sec)


def find_reconcile_candidates(
    workspace: Path,
    outcome_rows: list[dict[str, Any]],
    *,
    max_age_hours: float = 72.0,
) -> list[dict[str, Any]]:
    """Collect `sync_error`, `Pass(score=None)`, and orphan `submission-receipt.json` candidates."""
    errs = find_unreconciled_sync_errors(outcome_rows, max_age_hours=max_age_hours)
    seen = {str(r.get("submission_id") or "").strip() for r in errs}
    orphans = [r for r in find_orphan_receipts(workspace, outcome_rows, max_age_hours=max_age_hours) if r["submission_id"] not in seen]
    return errs + orphans


def _fetch_json(base_url: str, cookie: str, path: str, timeout: float = 5.0) -> Any:
    return fetch_json_with_cookie(base_url, cookie, path, timeout=timeout)


def _effective_ttl(orig: dict[str, Any], ttl_sec: float) -> float:
    if needs_score_backfill(orig):
        dt = parse_iso(orig.get("submitted_at") or orig.get("finished_at"))
        if dt and (utc_now() - dt).total_seconds() < 600.0:
            return min(ttl_sec, 15.0)
    return ttl_sec


def _reconcile_one_candidate(
    workspace: Path,
    outcome_path: Path,
    io_lock_path: Path,
    orig: dict[str, Any],
    *,
    force: bool,
    ttl_sec: float,
    request_timeout: float,
) -> dict[str, Any] | None:
    sid = str(orig.get("submission_id") or "").strip()
    title = str(orig.get("title") or "").strip()
    if not sid or not title:
        return None
    cache_key = f"sync_reconcile_{sid[:12]}"
    if not force and read_ttl_cache(cache_key, ttl_sec=_effective_ttl(orig, ttl_sec), fingerprint=sid) is not None:
        return None

    sessions = resolve_sessions_for_account(str(orig.get("account_id") or "primary"))
    if not sessions:
        return None
    write_ttl_cache(cache_key, {"checked_at": utc_now().isoformat()}, fingerprint=sid)

    sub_detail, used_session = fetch_submission_via_sessions(sessions, sid, _fetch_json, request_timeout)
    if not sub_detail or not used_session:
        return None

    status = str(sub_detail.get("status") or "").strip()
    if not status or status in NON_TERMINAL_JUDGE_STATUSES or status not in TERMINAL_JUDGE_STATUSES:
        return None

    rank_row: dict[str, Any] | None = None
    team_best_row: dict[str, Any] | None = None
    pid = resolve_problem_id(workspace, title)
    if status == "Pass" and pid:
        base_url, cookie, _ = used_session
        team_uids = {uid for _, _, uid in sessions if uid}
        team_name = _resolve_team_name(workspace)
        rank_row, team_best_row = fetch_ranking_sid_and_team_with_retry(
            lambda page: _fetch_json(base_url, cookie, f"/api/problems/{pid}/ranking?page={page}&size=100", timeout=request_timeout),
            sid,
            team_uids,
            team_name=team_name,
            status=status,
            retry_delays=(),
        )

    score, pub_times, pub_tbest = resolve_score_and_times(sub_detail, rank_row, status)
    if needs_score_backfill(orig) and score is None:
        return None

    is_orphan = orig.get("_reconcile_kind") == "orphan_receipt"
    now_iso = utc_now().isoformat()
    submitted_at = str(orig.get("submitted_at") or orig.get("finished_at") or now_iso)
    new_row = build_reconciled_outcome_row(orig, status, score, pub_times, pub_tbest, submitted_at, now_iso)

    with io_lock_path.open("a") as iolf:
        fcntl.flock(iolf, fcntl.LOCK_EX)
        try:
            latest_rows = read_jsonl(outcome_path)
            latest_collapsed = collapse_reconciled_outcomes(latest_rows)
            if is_orphan:
                still_pending = not any(
                    str(r.get("submission_id") or "").strip() == sid
                    and (str(r.get("status") or "").strip() in TERMINAL_JUDGE_STATUSES or is_sync_error_outcome(r))
                    for r in latest_collapsed
                )
            else:
                still_pending = any(
                    str(r.get("submission_id") or "").strip() == sid and (is_sync_error_outcome(r) or needs_score_backfill(r))
                    for r in latest_collapsed
                )
            if not still_pending:
                return None
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            with outcome_path.open("a", encoding="utf-8") as of:
                of.write(json.dumps(new_row, ensure_ascii=False) + "\n")
            is_latest = is_latest_submission_for_title(latest_rows + [new_row], workspace, title, sid)
            snap_row = team_best_row if team_best_row is not None else rank_row
            if snap_row is not None:
                snap_sid = str(snap_row.get("submission_id") or snap_row.get("sid") or snap_row.get("_id") or "").strip() or sid
                snap_sc = extract_numeric_score(snap_row)
                if snap_sid == sid:
                    update_score_snapshot(
                        workspace,
                        title,
                        sid,
                        status,
                        score if snap_sc is None else snap_sc,
                        snap_row.get("rank"),
                        pub_times,
                        pub_tbest,
                        submitted_at,
                    )
                else:
                    # Another teammate account holds a higher active row on the ranking board; keep snapshot aligned with team board.
                    snap_status = str(snap_row.get("status") or ("Pass" if (snap_sc is not None and snap_sc > 0) else status))
                    snap_times, snap_tbest = extract_public_times(None, snap_row)
                    update_score_snapshot(
                        workspace,
                        title,
                        snap_sid,
                        snap_status,
                        snap_sc,
                        snap_row.get("rank"),
                        snap_times,
                        snap_tbest,
                        require_current_check=False,
                    )
            elif status == "Pass" and score is not None and is_latest:
                update_score_snapshot(
                    workspace,
                    title,
                    sid,
                    status,
                    score,
                    None,
                    pub_times,
                    pub_tbest,
                    submitted_at,
                )
            return new_row
        finally:
            fcntl.flock(iolf, fcntl.LOCK_UN)


def reconcile_sync_errors(
    workspace: Path,
    *,
    force: bool = False,
    max_age_hours: float = 72.0,
    ttl_sec: float = 45.0,
    request_timeout: float = 5.0,
    max_items: int = 8,
) -> list[dict[str, Any]]:
    """Query Judge for unreconciled `sync_error`, `Pass(score=None)`, and orphan receipt submissions."""
    results_dir = workspace / "results"
    if not results_dir.is_dir():
        return []
    outcome_path = results_dir / "submit-queue-outcome.jsonl"
    candidates = find_reconcile_candidates(workspace, read_jsonl(outcome_path), max_age_hours=max_age_hours)
    if not candidates:
        return []
    reconciled: list[dict[str, Any]] = []
    io_lock_path = results_dir / ".submit-queue-io.lock"
    for orig in candidates[:max_items]:
        fixed = _reconcile_one_candidate(
            workspace, outcome_path, io_lock_path, orig, force=force, ttl_sec=ttl_sec, request_timeout=request_timeout
        )
        if fixed is not None:
            reconciled.append(fixed)
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
    """Spawn a single non-blocking daemon thread per workspace when unreconciled submissions exist."""
    if os.environ.get("OPSB_DISABLE_SYNC_RECONCILE") == "1":
        return False
    ws = workspace.expanduser().resolve()
    candidates = find_reconcile_candidates(ws, read_jsonl(ws / "results" / "submit-queue-outcome.jsonl"))
    if not candidates or not any(
        read_ttl_cache(
            f"sync_reconcile_{str(c.get('submission_id') or '')[:12]}",
            ttl_sec=_effective_ttl(c, ttl_sec),
            fingerprint=str(c.get("submission_id") or ""),
        ) is None
        for c in candidates
    ):
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
