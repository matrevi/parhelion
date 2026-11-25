"""Orphan `submission-receipt.json` discovery, session resolution & title recency helpers (V3-028)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import parse_iso, read_json, read_jsonl, utc_now
from ..device_env import device_text
from .judge_score_fetch import TERMINAL_JUDGE_STATUSES


def _account_labels_map() -> dict[str, str]:
    try:
        from .auth_service import list_judge_accounts

        return {str(a["id"]): str(a.get("label") or a["id"]) for a in list_judge_accounts() if a.get("id")}
    except Exception:
        return {}


def read_cookie_session(session_file: Path) -> tuple[str, str, str] | None:
    """Read `(base_url, cookie_header, user_id)` from a saved Judge session JSON."""
    if not session_file.is_file():
        return None
    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    base = str(data.get("base_url") or device_text("judge_base")).rstrip("/")
    uid = str(data.get("user_id") or "")
    cookies = [
        f"{r['name']}={r['value']}"
        for r in (data.get("cookies") or [])
        if isinstance(r, dict) and r.get("name") and r.get("value") is not None
    ]
    return (base, "; ".join(cookies), uid) if cookies else None


def resolve_sessions_for_account(account_id: str) -> list[tuple[str, str, str]]:
    """Return logged-in `(base_url, cookie, user_id)` sessions ordered with `account_id` first."""
    from .auth_service import list_judge_accounts

    sessions: list[tuple[str, str, str]] = []
    seen_files: set[str] = set()
    try:
        accounts = sorted(list_judge_accounts(), key=lambda a: 0 if a.get("id") == account_id else 1)
    except Exception:
        accounts = []
    for acc in accounts:
        sf = str(acc.get("session_file") or "")
        if not acc.get("logged_in") or not sf or sf in seen_files:
            continue
        seen_files.add(sf)
        parsed = read_cookie_session(Path(sf))
        if parsed:
            sessions.append(parsed)
    return sessions


def is_latest_submission_for_title(
    outcome_rows: list[dict[str, Any]],
    workspace: Path,
    title: str,
    sid: str,
) -> bool:
    """Return True if `sid` is the newest known submission for `title` across outcomes and receipts."""
    latest_sid, latest_ts = "", -1.0
    for row in outcome_rows:
        if row.get("title") != title or not row.get("submission_id"):
            continue
        dt = parse_iso(row.get("submitted_at") or row.get("finished_at"))
        ts = dt.timestamp() if dt else 0.0
        if ts >= latest_ts:
            latest_ts, latest_sid = ts, str(row["submission_id"])
    t_dir = workspace / "results" / title
    for r_file in t_dir.glob("*/submission-receipt.json") if t_dir.is_dir() else []:
        rd = read_json(r_file, {})
        if isinstance(rd, dict) and rd.get("submission_id"):
            dt = parse_iso(rd.get("submitted_at"))
            ts = dt.timestamp() if dt else 0.0
            if ts > latest_ts:
                latest_ts, latest_sid = ts, str(rd["submission_id"])
    return not latest_sid or latest_sid == sid


def find_orphan_receipts(
    workspace: Path,
    collapsed_outcomes: list[dict[str, Any]],
    known_sids: set[str],
    *,
    max_age_hours: float = 72.0,
    queue_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Discover `results/*/*/submission-receipt.json` whose `submission_id` lacks a terminal outcome."""
    results_dir = workspace / "results"
    if not results_dir.is_dir():
        return []
    q_rows = queue_rows if queue_rows is not None else read_jsonl(results_dir / "submit-queue.jsonl")
    labels = _account_labels_map()
    now = utc_now()
    orphans: list[dict[str, Any]] = []
    seen_sids: set[str] = set()

    for r_file in results_dir.glob("*/*/submission-receipt.json"):
        rd = read_json(r_file, {})
        if not isinstance(rd, dict):
            continue
        sid = str(rd.get("submission_id") or "").strip()
        if not sid or sid in known_sids or sid in seen_sids:
            continue
        submitted_at = str(rd.get("submitted_at") or "").strip()
        dt = parse_iso(submitted_at)
        if not dt:
            continue
        if max_age_hours > 0 and (now - dt).total_seconds() > max_age_hours * 3600.0:
            continue
        title = r_file.parents[1].name
        sha = str(rd.get("validated_commit") or r_file.parent.name).strip()
        aid = str(rd.get("account_id") or "primary").strip() or "primary"
        s7 = sha[:7].lower()
        q_match = next(
            (q for q in reversed(q_rows) if q.get("title") == title and str(q.get("sha") or "").lower().startswith(s7)),
            None,
        )
        seen_sids.add(sid)
        row: dict[str, Any] = {
            "title": title,
            "sha": sha,
            "note": str((q_match or {}).get("note") or "reconciled from orphan submission receipt"),
            "queued_at": str((q_match or {}).get("queued_at") or submitted_at),
            "submitted_at": submitted_at,
            "submission_id": sid,
            "account_id": aid,
            "account_label": labels.get(aid) or ("主账号" if aid == "primary" else aid),
            "reconciled_from": "orphan_receipt",
            "_reconcile_kind": "orphan_receipt",
        }
        if isinstance((q_match or {}).get("estimated_score"), (int, float)):
            row["estimated_score"] = q_match["estimated_score"]
        if (q_match or {}).get("force") is not None:
            row["force"] = bool(q_match["force"])
        orphans.append(row)

    orphans.sort(key=lambda r: str(r.get("submitted_at") or ""), reverse=True)
    return orphans


def collect_recent_unreconciled_receipt_pairs(
    workspace: Path,
    collapsed_outcomes: list[dict[str, Any]],
    *,
    max_age_sec: float = 2100.0,
) -> set[tuple[str, str]]:
    """Return `(title, sha7_lower)` for receipts (< max_age_sec) without a terminal outcome row for their `sid`."""
    results_dir = workspace / "results"
    if not results_dir.is_dir():
        return set()
    terminal_statuses = TERMINAL_JUDGE_STATUSES | {"rejected", "skipped", "duplicate", "sync_error"}
    terminal_sids = {
        str(o.get("submission_id") or "").strip()
        for o in collapsed_outcomes
        if o.get("submission_id") and str(o.get("status") or "").strip() in terminal_statuses
    }
    now_ep = utc_now().timestamp()
    pairs: set[tuple[str, str]] = set()
    for r_file in results_dir.glob("*/*/submission-receipt.json"):
        rd = read_json(r_file, {})
        if not isinstance(rd, dict):
            continue
        sid = str(rd.get("submission_id") or "").strip()
        dt = parse_iso(rd.get("submitted_at"))
        if not sid or sid in terminal_sids or not dt or (now_ep - dt.timestamp()) >= max_age_sec:
            continue
        title = r_file.parents[1].name
        for cand in (str(rd.get("validated_commit") or "").strip(), r_file.parent.name.strip()):
            if len(cand) >= 7:
                pairs.add((title, cand[:7].lower()))
    return pairs


def fetch_submission_via_sessions(
    sessions: list[tuple[str, str, str]],
    sid: str,
    fetch_fn: Any,
    timeout: float,
) -> tuple[dict[str, Any] | None, tuple[str, str, str] | None]:
    """Query `/api/submissions/{sid}` across logged-in sessions until a valid status body returns."""
    for base_url, cookie, uid in sessions:
        try:
            raw_sub = fetch_fn(base_url, cookie, f"/api/submissions/{sid}", timeout=timeout)
            body = raw_sub.get("data") if isinstance(raw_sub, dict) and isinstance(raw_sub.get("data"), dict) else raw_sub
            if isinstance(body, dict) and body.get("status"):
                return body, (base_url, cookie, uid)
        except Exception:
            continue
    return None, None


def build_reconciled_outcome_row(
    orig: dict[str, Any],
    status: str,
    score: float | int | None,
    pub_times: list[float],
    pub_tbest: list[float],
    submitted_at: str,
    reconciled_at: str,
) -> dict[str, Any]:
    """Build normalized terminal outcome row from a `sync_error`, `Pass(score=None)`, or orphan receipt."""
    new_row = {k: v for k, v in orig.items() if k not in ("_reconcile_kind", "detail")}
    new_row.update(
        {
            "finished_at": orig.get("finished_at") or reconciled_at,
            "submitted_at": submitted_at,
            "status": status,
            "score": score if status == "Pass" else 0,
            "reconciled_at": reconciled_at,
            "reconciled_from": orig.get("reconciled_from") or orig.get("status") or "sync_error",
        }
    )
    if pub_times:
        new_row["public_times_us"] = pub_times
    if pub_tbest:
        new_row["public_tbest_us"] = pub_tbest
    return new_row
