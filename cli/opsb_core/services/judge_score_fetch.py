"""Shared Judge strict submission-ID ranking match, retry & snapshot helpers (V3-025 / V3-028)."""
from __future__ import annotations

import datetime
import fcntl
import json
import math
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

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

DEFAULT_RANKING_RETRY_DELAYS: tuple[float, ...] = (2.0, 3.0, 4.0, 5.0)


def _parse_iso(ts: str | None) -> datetime.datetime | None:
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def fetch_json_with_cookie(base_url: str, cookie: str, path: str, timeout: float = 5.0) -> Any:
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        headers={
            "Accept": "application/json",
            "Cookie": cookie,
            "User-Agent": "opsb-sync-reconciler/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resolve_problem_id(workspace: Path, title: str) -> str | None:
    for cand in (workspace / "opsb.workspace.json", workspace / "bench" / "problems.json"):
        data = _read_json(cand)
        probs = data.get("problems") if isinstance(data, dict) else data
        if isinstance(probs, list):
            for p in probs:
                if isinstance(p, dict) and p.get("title") == title and p.get("problem_id"):
                    return str(p["problem_id"])
    return None


def _row_team_name(row: dict[str, Any]) -> str:
    direct = row.get("team_name")
    if direct:
        return str(direct).strip()
    team = row.get("team")
    if isinstance(team, dict) and team.get("team_name"):
        return str(team["team_name"]).strip()
    return ""


def _row_belongs_to_team(row: dict[str, Any], team_user_ids: set[str], team_name: str = "") -> bool:
    uid = str(row.get("user_id") or "").strip()
    if uid and team_user_ids and uid in team_user_ids:
        return True
    wanted = (team_name or "").strip()
    if wanted:
        r_team = _row_team_name(row)
        if r_team and r_team == wanted:
            return True
    return False


def pick_best_team_ranking_row(
    rows: Any,
    team_user_ids: set[str],
    team_name: str = "",
) -> dict[str, Any] | None:
    """Return the highest-scoring ranking row belonging to our team across all teammate accounts."""
    if not isinstance(rows, list):
        return None
    best: dict[str, Any] | None = None
    best_score = -1.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not _row_belongs_to_team(row, team_user_ids, team_name=team_name):
            continue
        sc = extract_numeric_score(row)
        sc_val = sc if sc is not None else 0.0
        if best is None or sc_val > best_score:
            best = row
            best_score = sc_val
    return best


def match_ranking_row_by_sid(rows: Any, sid: str) -> dict[str, Any] | None:
    """Strictly match a ranking row by `submission_id`/`sid`/`_id` (never fallback to `user_id`)."""
    target = str(sid or "").strip()
    if not target or not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        r_sid = str(row.get("submission_id") or row.get("sid") or row.get("_id") or "").strip()
        if r_sid and r_sid == target:
            return row
    return None


def extract_numeric_score(obj: Any) -> float | None:
    """Safely extract a finite numeric `score` from a submission detail or ranking row."""
    if not isinstance(obj, dict):
        return None
    raw = obj.get("score")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None


def extract_public_times(sub_detail: Any, rank_row: Any = None) -> tuple[list[float], list[float]]:
    """Extract first 5 public testcase times & tbest from submission detail / ranking row."""
    sub_res = (sub_detail.get("result") or []) if isinstance(sub_detail, dict) else []
    pub_times = [float(r["time"]) for r in sub_res if isinstance(r, dict) and r.get("time") is not None][:5]
    pub_tbest = [float(r["best_time"]) for r in sub_res if isinstance(r, dict) and r.get("best_time") is not None][:5]
    if not pub_times and isinstance(rank_row, dict):
        rr_res = rank_row.get("result") or []
        if rr_res and isinstance(rr_res[0], dict):
            pub_times = [float(x["time"]) for x in rr_res if isinstance(x, dict) and x.get("time") is not None][:5]
        else:
            pub_times = [float(t) for t in (rank_row.get("times") or [])[:5] if t is not None]
    return pub_times, pub_tbest


def scan_ranking_pages(
    fetch_page: Callable[[int], Any],
    sid: str,
    *,
    max_pages: int = 7,
) -> dict[str, Any] | None:
    """Scan ranking pages 1..max_pages strictly matching `sid`."""
    for page in range(1, max_pages + 1):
        try:
            payload = fetch_page(page)
        except Exception:
            break
        if not isinstance(payload, dict):
            break
        matched = match_ranking_row_by_sid(payload.get("rows") or [], sid)
        if matched is not None:
            return matched
        try:
            total_pages = int(payload.get("pages") or 1)
        except (TypeError, ValueError):
            total_pages = 1
        if page >= total_pages:
            break
    return None


def scan_ranking_for_sid_and_team(
    fetch_page: Callable[[int], Any],
    sid: str,
    team_user_ids: set[str],
    team_name: str = "",
    *,
    max_pages: int = 7,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Scan ranking pages for both the exact `sid` row AND the team's highest-scoring active row.

    Ranking pages are ordered by `score` descending, so once we locate `sid` (or finish pages),
    we have already inspected every higher-scoring page and therefore know the team's best active
    board row across all teammate accounts.
    """
    sid_row: dict[str, Any] | None = None
    best_team_row: dict[str, Any] | None = None
    best_team_score = -1.0
    for page in range(1, max_pages + 1):
        try:
            payload = fetch_page(page)
        except Exception:
            break
        if not isinstance(payload, dict):
            break
        rows = payload.get("rows") or []
        cand_team = pick_best_team_ranking_row(rows, team_user_ids, team_name=team_name)
        if cand_team is not None:
            sc = extract_numeric_score(cand_team)
            sc_val = sc if sc is not None else 0.0
            if best_team_row is None or sc_val > best_team_score:
                best_team_row = cand_team
                best_team_score = sc_val
        matched = match_ranking_row_by_sid(rows, sid)
        if matched is not None:
            sid_row = matched
            break
        try:
            total_pages = int(payload.get("pages") or 1)
        except (TypeError, ValueError):
            total_pages = 1
        if page >= total_pages:
            break
    if best_team_row is None:
        best_team_row = sid_row
    elif sid_row is not None:
        sid_sc = extract_numeric_score(sid_row)
        if sid_sc is not None and sid_sc > best_team_score:
            best_team_row = sid_row
    return sid_row, best_team_row


def fetch_ranking_row_with_retry(
    fetch_page: Callable[[int], Any],
    sid: str,
    *,
    status: str = "Pass",
    retry_delays: tuple[float, ...] = DEFAULT_RANKING_RETRY_DELAYS,
    max_pages: int = 7,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any] | None:
    """Strict `sid` ranking scan with short backoff retry on `Pass` async ranking lag."""
    row = scan_ranking_pages(fetch_page, sid, max_pages=max_pages)
    if row is not None or status != "Pass":
        return row
    for delay in retry_delays:
        if delay > 0:
            sleep_fn(delay)
        row = scan_ranking_pages(fetch_page, sid, max_pages=max_pages)
        if row is not None:
            return row
    return None


def fetch_ranking_sid_and_team_with_retry(
    fetch_page: Callable[[int], Any],
    sid: str,
    team_user_ids: set[str],
    team_name: str = "",
    *,
    status: str = "Pass",
    retry_delays: tuple[float, ...] = DEFAULT_RANKING_RETRY_DELAYS,
    max_pages: int = 7,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch both exact `sid` row and team-best board row with retry on `Pass` async ranking lag."""
    sid_row, team_row = scan_ranking_for_sid_and_team(
        fetch_page, sid, team_user_ids, team_name=team_name, max_pages=max_pages
    )
    if sid_row is not None or status != "Pass":
        return sid_row, team_row
    for delay in retry_delays:
        if delay > 0:
            sleep_fn(delay)
        sid_row, team_row = scan_ranking_for_sid_and_team(
            fetch_page, sid, team_user_ids, team_name=team_name, max_pages=max_pages
        )
        if sid_row is not None:
            return sid_row, team_row
    return sid_row, team_row


def resolve_score_and_times(
    sub_detail: Any,
    rank_row: Any,
    status: str,
) -> tuple[float | int | None, list[float], list[float]]:
    """Prefer strict ranking row score/times, falling back safely to numeric `sub_detail['score']`."""
    pub_times, pub_tbest = extract_public_times(sub_detail, rank_row)
    if status != "Pass":
        return 0, pub_times, pub_tbest
    score = extract_numeric_score(rank_row)
    if score is None:
        score = extract_numeric_score(sub_detail)
    return score, pub_times, pub_tbest


def update_score_snapshot(
    workspace: Path,
    title: str,
    sid: str,
    status: str,
    score: float | int | None,
    rank: Any,
    pub_times: list[float],
    pub_tbest: list[float],
    submitted_at: str = "",
    *,
    require_current_check: bool = True,
) -> bool:
    """Update `results/score-snapshot.json` atomically when `sid` is current/newer."""
    snap_path = workspace / "results" / "score-snapshot.json"
    if not snap_path.is_file():
        return False
    with (snap_path.parent / ".score-snapshot.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        snap = _read_json(snap_path, {})
        if not isinstance(snap, dict):
            return False
        prev = snap.get(title) if isinstance(snap.get(title), dict) else {}
        prev_sid = str(prev.get("submission_id") or "")
        prev_dt = _parse_iso(prev.get("submission_time"))
        cur_dt = _parse_iso(submitted_at)
        if require_current_check:
            should_update = (prev_sid == sid) or (not prev_dt) or (cur_dt is not None and cur_dt >= prev_dt)
            if not should_update:
                return False

        new_rank = rank if rank is not None else (prev.get("rank") if prev_sid == sid else None)
        new_score = (score if score is not None else (prev.get("score") if prev_sid == sid else None)) if status == "Pass" else 0
        sub_time = prev.get("submission_time")
        if submitted_at and (not sub_time or (cur_dt is not None and (prev_dt is None or cur_dt >= prev_dt))):
            sub_time = submitted_at

        snap[title] = {
            **prev,
            "rank": new_rank,
            "status": status,
            "submission_id": sid,
            "score": new_score,
            "public_times_us": pub_times if pub_times else prev.get("public_times_us", []),
            **({"public_tbest_us": pub_tbest} if pub_tbest else {}),
            **({"submission_time": sub_time} if sub_time else {}),
        }
        atomic_write_json(snap_path, snap)
        return True
