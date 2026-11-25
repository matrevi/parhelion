"""24h Auto-Drift Refresh: live `tbest(t)`, frontier, board scores & Simplex MAP recalibration.

In multi-day KernelC contests, site-wide `tbest_i(t)` tightens monotonically as rival
teams submit faster kernels.  If an agent or `OpsSupervisor.app` starts >24h after the
last submission (or >24h after the last board refresh), cached `tbest`, frontier Top1/Top5,
live scores (`gamma_p`), and `calibration.json` predictions go stale.

This service performs a lightweight read-only refresh (`GET /api/problems/{pid}/ranking`)
across all contest problems (~2s total, zero rate-limit cost), persists
`results/scoring-evidence/live-board-tbest.json`, refreshes `results/score-snapshot.json`,
and re-runs `BenchmarkCalibrator` so predicted scores, `gamma_p`, and bucket headroom
stay anchored to live `tbest(t)`.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import SUPERVISOR_ROOT, invalidate_ttl_cache, parse_iso, read_json, read_jsonl, resolve_python_exe, utc_now

DEFAULT_STALE_HOURS = 24.0
DEFAULT_SCORE_DRIFT_PTS = 0.5
ATTEMPT_THROTTLE_SEC = 300.0
REQUEST_TIMEOUT_SEC = 10.0

_bg_lock = threading.Lock()
_bg_running: set[Path] = set()


def live_board_path(workspace: Path) -> Path:
    return workspace / "results" / "scoring-evidence" / "live-board-tbest.json"


def _lock_path(workspace: Path) -> Path:
    return workspace / "results" / "scoring-evidence" / ".board-refresh.lock"


def _session_info() -> tuple[str, str, str]:
    """Return (base_url, cookie_header, user_id) from `~/.parhelion/sessions/` or `~/.judge/session.json`."""
    parhelion_home = Path(os.environ.get("PARHELION_HOME") or os.environ.get("OPSB_HOME") or (Path.home() / ".parhelion")).expanduser()
    candidates = [
        os.environ.get("JUDGE_SESSION_FILE"),
        str(parhelion_home / "sessions" / "primary.json"),
        str(Path.home() / ".judge" / "session.json"),
    ]
    path = next((Path(c).expanduser() for c in candidates if c and Path(c).expanduser().is_file()), Path.home() / ".judge" / "session.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    base = str(data.get("base_url") or "https://judge.invalid").rstrip("/")
    user_id = str(data.get("user_id") or "")
    jar = []
    for rec in data.get("cookies") or []:
        name = rec.get("name")
        value = rec.get("value")
        if name and value is not None:
            jar.append(f"{name}={value}")
    return base, "; ".join(jar), user_id


def _all_team_sessions() -> list[tuple[str, str, str]]:
    """Return all connected teammate sessions (base_url, cookie_header, user_id)."""
    out: list[tuple[str, str, str]] = []
    seen_uids: set[str] = set()
    parhelion_home = Path(os.environ.get("PARHELION_HOME") or os.environ.get("OPSB_HOME") or (Path.home() / ".parhelion")).expanduser()
    candidates: list[Path] = []
    env_p = os.environ.get("JUDGE_SESSION_FILE")
    if env_p:
        candidates.append(Path(env_p))
    candidates.append(parhelion_home / "sessions" / "primary.json")
    if (parhelion_home / "sessions").is_dir():
        candidates.extend(sorted((parhelion_home / "sessions").glob("*.json")))
    candidates.append(Path.home() / ".judge" / "session.json")
    sdir = Path.home() / ".judge" / "sessions"
    if sdir.is_dir():
        candidates.extend(sorted(sdir.glob("*.json")))
    for p in candidates:
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        base = str(data.get("base_url") or "https://judge.invalid").rstrip("/")
        uid = str(data.get("user_id") or "")
        jar = "; ".join(
            f"{rec['name']}={rec['value']}"
            for rec in (data.get("cookies") or [])
            if rec.get("name") and rec.get("value") is not None
        )
        if jar and (not uid or uid not in seen_uids):
            if uid:
                seen_uids.add(uid)
            out.append((base, jar, uid))
    return out


def _get_json(base: str, cookie: str, path: str) -> Any:
    req = urllib.request.Request(
        base + path,
        headers={
            "Accept": "application/json",
            "Cookie": cookie,
            "User-Agent": "opsb-board-refresh/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _latest_submit_epoch(workspace: Path) -> float:
    results_dir = workspace / "results"
    latest = 0.0
    if not results_dir.is_dir():
        return latest
    for rf in results_dir.glob("*/*/submission-receipt.json"):
        rd = read_json(rf, {})
        if isinstance(rd, dict):
            dt = parse_iso(rd.get("submitted_at"))
            if dt:
                latest = max(latest, dt.timestamp())
    for o in read_jsonl(results_dir / "submit-queue-outcome.jsonl"):
        if o.get("status") not in ("rejected", "skipped", "duplicate") and o.get("submission_id"):
            dt = parse_iso(o.get("submitted_at") or o.get("finished_at"))
            if dt:
                latest = max(latest, dt.timestamp())
    return latest


def _resolve_team_name(workspace: Path) -> str:
    from . import ranking_service
    from .contest_service import resolve_workspace_meta

    ranking = ranking_service.team_ranking(workspace, allow_fetch=False) or {}
    target = ranking_service._target(workspace)
    if target and len(target) > 3:
        return ranking.get("team_name") or ""
    return ranking.get("team_name") or resolve_workspace_meta(workspace).get("team_name") or ""


def board_scope(workspace: Path) -> dict:
    from . import ranking_service

    target = ranking_service._target(workspace)
    if not target or len(target) == 3:
        return {}
    ranking = ranking_service._read_cache(ranking_service._cache_path(workspace), target) or {}
    return {"target": list(target), "team_id": ranking.get("team_id"), "team_name": ranking.get("team_name")}


def current_board(workspace: Path, problems: dict) -> tuple[dict, dict, dict]:
    """One captain-scoped snapshot for the scheduler and the contest inspector."""
    scope = board_scope(workspace)
    payload = read_json(live_board_path(workspace), {}) or {}
    snapshot = {} if scope else read_json(workspace / "results/score-snapshot.json", {}) or {}
    live = payload.get("problems", {}) if payload.get("scope", {}) == scope else {}
    matched = {}
    for title, meta in problems.items():
        row = live.get(title) or {}
        if row.get("problem_id") and row["problem_id"] != meta.get("problem_id"):
            continue
        if not isinstance(row.get("listed"), bool):
            continue
        matched[title] = row
        score = row.get("our_score")
        status = "unranked" if not row["listed"] else (
            "board-unknown" if score is None else row.get("our_status") or ("Pass" if score > 0 else "ranked")
        )
        snapshot[title] = {"listed": row["listed"], "score": score, "status": status,
                           "rank": row.get("our_rank"), "submission_id": row.get("board_submission_id"),
                           "public_times_us": row.get("our_public_times_us") or []}
    return snapshot, matched, scope


def inspect_board_freshness(
    workspace: Path,
    max_age_hours: float = DEFAULT_STALE_HOURS,
    *,
    score_drift: float | None = None,
    drift_threshold: float = DEFAULT_SCORE_DRIFT_PTS,
) -> dict[str, Any]:
    now_ep = utc_now().timestamp()
    bp = live_board_path(workspace)
    data = read_json(bp, {}) if bp.is_file() else {}
    scope = board_scope(workspace)
    if data.get("scope", {}) != scope:
        data = {}
    fetched_dt = parse_iso(data.get("fetched_at")) if isinstance(data, dict) else None
    attempt_dt = parse_iso(data.get("attempt_at")) if isinstance(data, dict) else None
    last_sub_ep = _latest_submit_epoch(workspace)

    refresh_age_hrs = round((now_ep - fetched_dt.timestamp()) / 3600.0, 2) if fetched_dt else None
    submit_age_hrs = round((now_ep - last_sub_ep) / 3600.0, 2) if last_sub_ep > 0 else None
    attempt_age_sec = (now_ep - attempt_dt.timestamp()) if attempt_dt else float("inf")

    stale = False
    reason = "fresh"
    if fetched_dt is None:
        stale = True
        reason = "never_refreshed"
    elif score_drift is not None and abs(float(score_drift)) >= float(drift_threshold):
        stale = True
        reason = f"score_drift={abs(float(score_drift)):.2f}pts"
    elif refresh_age_hrs is not None and refresh_age_hrs >= max_age_hours:
        stale = True
        reason = f"refresh_age={refresh_age_hrs:.1f}h>={max_age_hours:.0f}h"
    elif (
        last_sub_ep > 0
        and submit_age_hrs is not None
        and submit_age_hrs >= max_age_hours
        and fetched_dt.timestamp() < last_sub_ep + max_age_hours * 3600.0
    ):
        stale = True
        reason = f"submit_gap={submit_age_hrs:.1f}h>={max_age_hours:.0f}h"

    throttled = stale and (attempt_age_sec < ATTEMPT_THROTTLE_SEC)
    return {
        "stale": stale,
        "should_refresh": stale and not throttled and (not scope or bool(scope["team_name"])),
        "throttled": throttled,
        "reason": reason,
        "last_refreshed_at": data.get("fetched_at") if isinstance(data, dict) else None,
        "refresh_age_hours": refresh_age_hrs,
        "hours_since_last_submit": submit_age_hrs,
        "max_age_hours": max_age_hours,
    }


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def refresh_board_and_recalibrate(
    workspace: Path,
    *,
    force: bool = False,
    max_age_hours: float = DEFAULT_STALE_HOURS,
    recalibrate: bool = True,
    score_drift: float | None = None,
) -> dict[str, Any]:
    """Fetch live tbest / frontier / board scores for all 10 problems and recalibrate models."""
    fresh = inspect_board_freshness(workspace, max_age_hours=max_age_hours, score_drift=score_drift)
    if not force and not fresh["should_refresh"]:
        return {"ok": True, "skipped": True, **fresh}

    lock_f = _lock_path(workspace)
    lock_f.parent.mkdir(parents=True, exist_ok=True)
    with lock_f.open("a") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"ok": True, "skipped": True, "reason": "refresh_in_progress", **fresh}

        try:
            # Re-check after lock acquisition
            fresh2 = inspect_board_freshness(workspace, max_age_hours=max_age_hours, score_drift=score_drift)
            if not force and not fresh2["should_refresh"]:
                return {"ok": True, "skipped": True, **fresh2}

            bp = live_board_path(workspace)
            scope = board_scope(workspace)
            if scope and not scope["team_name"]:
                return {"ok": False, "error": "Awaiting captain team registration"}
            prev_payload = read_json(bp, {}) if bp.is_file() else {}
            if prev_payload.get("scope", {}) != scope:
                prev_payload = {"scope": scope}
            prev_boards = prev_payload.get("problems", {}) if isinstance(prev_payload, dict) else {}

            # Mark attempt timestamp immediately so failures don't hot-loop
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if isinstance(prev_payload, dict):
                prev_payload["attempt_at"] = now_iso
                try:
                    _atomic_write_json(bp, prev_payload)
                except Exception:
                    pass

            from . import ranking_service
            if scope:
                base, cookie = ranking_service.captain_session(tuple(scope["target"]))
                user_id = scope["target"][4]
                team_sessions = [(base, cookie, user_id)]
            else:
                base, cookie, user_id = _session_info()
                team_sessions = _all_team_sessions() or [(base, cookie, user_id)]
            team_user_ids = {uid for _, _, uid in team_sessions if uid}
            if user_id:
                team_user_ids.add(user_id)
            team_name = _resolve_team_name(workspace)
            from .contest_service import resolve_workspace_meta
            problems = resolve_workspace_meta(workspace)["problems"]
            if not isinstance(problems, list) or not problems:
                return {"ok": False, "error": f"no contest problems under {workspace}"}

            snap_path = workspace / "results" / "score-snapshot.json"
            snap = read_json(snap_path, {}) if snap_path.is_file() else {}
            if not isinstance(snap, dict):
                snap = {}

            updated_problems: dict[str, Any] = {}
            drift_alerts: list[dict[str, Any]] = []

            for p in problems:
                title = p.get("title")
                pid = p.get("problem_id")
                if not title or not pid:
                    continue

                tbest_list: list[float] = []
                total_cases = None
                frontier_top1: float | None = None
                frontier_top5: float | None = None
                our_row: dict[str, Any] | None = None
                tc_ids: list[str] = []

                page = 1
                while True:
                    d = _get_json(base, cookie, f"/api/problems/{pid}/ranking?page={page}&size=100")
                    if not isinstance(d.get("rows"), list) or "pages" not in d:
                        raise ValueError(f"Incomplete ranking response for {title}")
                    if page == 1:
                        total_cases = int(d["total_testcase_count"]) if d.get("total_testcase_count") else None
                        tcs = d.get("testcases") or []
                        tbest_list = [float(x["tbest"]) for x in tcs if x.get("tbest") is not None]
                        tc_ids = [str(x.get("id") or x.get("_id") or "") for x in tcs if x.get("tbest") is not None]
                        rows1 = d.get("rows") or []
                        if rows1:
                            frontier_top1 = round(float(rows1[0].get("score") or 0.0), 2)
                            frontier_top5 = round(float(rows1[min(4, len(rows1) - 1)].get("score") or 0.0), 2)
                    rows = d.get("rows") or []
                    for r in rows:
                        if not isinstance(r, dict):
                            continue
                        r_uid = str(r.get("user_id") or "").strip()
                        r_tname = str(
                            r.get("team_name")
                            or ((r.get("team") or {}).get("team_name") if isinstance(r.get("team"), dict) else "")
                            or ""
                        ).strip()
                        is_team_row = (bool(team_user_ids) and bool(r_uid) and r_uid in team_user_ids) or (
                            bool(team_name) and bool(r_tname) and r_tname == team_name
                        )
                        if scope:
                            is_team_row = ranking_service.matches_team(r, scope["team_id"], scope["team_name"])
                        if is_team_row:
                            # Prefer highest-scoring / lowest-rank team row across all teammate accounts
                            if our_row is None or float(r.get("score") or 0.0) > float(our_row.get("score") or 0.0):
                                our_row = r
                    if our_row is not None or page >= int(d.get("pages") or 1):
                        break
                    if not rows:
                        raise ValueError(f"Missing ranking page {page} for {title}")
                    page += 1

                prev_entry = prev_boards.get(title) or {}
                prev_tbest = prev_entry.get("tbest") or []
                tbest_delta_pct = None
                if tbest_list and len(tbest_list) == len(prev_tbest) and sum(prev_tbest) > 0:
                    tbest_delta_pct = round(((sum(tbest_list) - sum(prev_tbest)) / sum(prev_tbest)) * 100.0, 2)

                our_times: list[float] = []
                if our_row is not None:
                    res_list = our_row.get("result") or []
                    if res_list and isinstance(res_list[0], dict):
                        id_set = set(tc_ids)
                        our_times = [
                            float(x["time"])
                            for x in res_list
                            if (not id_set or str(x.get("testcase_id") or "") in id_set) and x.get("time") is not None
                        ]
                    elif isinstance(our_row.get("times"), list):
                        our_times = [float(x) for x in our_row["times"] if x is not None]
                    # Fallback: query /api/submissions/{sid} using whichever teammate session owns it
                    sub_id = str(our_row.get("submission_id") or "")
                    if not our_times and sub_id:
                        for s_base, s_cookie, _ in team_sessions:
                            try:
                                sd = _get_json(s_base, s_cookie, f"/api/submissions/{sub_id}")
                                s_body = sd.get("data") if isinstance(sd, dict) and isinstance(sd.get("data"), dict) else sd
                                s_res = s_body.get("result") or [] if isinstance(s_body, dict) else []
                                times_from_sub = [float(x["time"]) for x in s_res if isinstance(x, dict) and x.get("time") is not None]
                                if times_from_sub:
                                    our_times = times_from_sub
                                    if not tbest_list:
                                        tbest_list = [float(x["best_time"]) for x in s_res if isinstance(x, dict) and x.get("best_time") is not None]
                                    break
                            except Exception:
                                continue

                    prev_snap = snap.get(title) if isinstance(snap.get(title), dict) else {}
                    prev_score = prev_snap.get("score")
                    raw_row_score = our_row.get("score")
                    has_pos_score = (
                        isinstance(raw_row_score, (int, float))
                        and not isinstance(raw_row_score, bool)
                        and float(raw_row_score) > 0.0
                    )
                    # Ranking rows may omit `"status"`; a positive ranking score is always a live Pass.
                    live_status = str(our_row.get("status") or ("Pass" if has_pos_score else (prev_snap.get("status") or "")))
                    live_score = round(float(raw_row_score or 0.0), 2) if live_status == "Pass" else 0.0
                    snap[title] = {
                        **prev_snap,
                        "rank": our_row.get("rank"),
                        "status": live_status,
                        "submission_id": our_row.get("submission_id") or prev_snap.get("submission_id"),
                        "score": live_score,
                        "public_times_us": our_times if our_times else prev_snap.get("public_times_us", []),
                        "board_refreshed_at": now_iso,
                    }
                    if isinstance(prev_score, (int, float)) and abs(live_score - float(prev_score)) >= 0.05:
                        drift_alerts.append({
                            "title": title,
                            "score_before": round(float(prev_score), 2),
                            "score_after": live_score,
                            "delta": round(live_score - float(prev_score), 2),
                        })

                updated_problems[title] = {
                    "listed": our_row is not None,
                    "ranking_total": d.get("total"),
                    "board_submission_id": our_row.get("submission_id") if our_row else None,
                    "problem_id": pid,
                    "tbest": tbest_list,
                    "tbest_delta_pct": tbest_delta_pct,
                    "total_testcase_count": total_cases,
                    "frontier_top1": frontier_top1,
                    "frontier_top5": frontier_top5,
                    "our_score": our_row.get("score") if our_row else None,
                    "our_rank": our_row.get("rank") if our_row else None,
                    "our_status": our_row.get("status") if our_row else None,
                    "our_public_times_us": our_times,
                }

            payload = {
                "scope": scope,
                "schema_version": 1,
                "fetched_at": now_iso,
                "attempt_at": now_iso,
                "trigger_reason": "manual_force" if force else fresh2["reason"],
                "problems": updated_problems,
                "score_drifts": drift_alerts,
            }
            if board_scope(workspace) != scope:
                return {"ok": True, "skipped": True, "reason": "team_changed"}
            _atomic_write_json(bp, payload)
            if not scope:
                _atomic_write_json(snap_path, snap)

            calibrated_ok = False
            if recalibrate:
                try:
                    py_exe = resolve_python_exe(workspace)
                    proc = subprocess.run(
                        [py_exe, "-m", "scheduler.cli_bridge", "calibrate", "--workspace", str(workspace)],
                        cwd=str(SUPERVISOR_ROOT),
                        capture_output=True,
                        text=True,
                        timeout=30.0,
                    )
                    calibrated_ok = proc.returncode == 0
                except Exception:
                    calibrated_ok = False

            invalidate_ttl_cache("scheduler_bridge")
            try:
                from . import ranking_service
                ranking_service.refresh_async(workspace, force=True)
            except Exception:
                pass

            return {
                "ok": True,
                "skipped": False,
                "fetched_at": now_iso,
                "trigger_reason": payload["trigger_reason"],
                "problems_refreshed": len(updated_problems),
                "recalibrated": calibrated_ok,
                "score_drifts": drift_alerts,
                "problems": updated_problems,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300], **fresh}
        finally:
            try:
                fcntl.flock(lf, fcntl.LOCK_UN)
            except Exception:
                pass


def refresh_if_stale_async(
    workspace: Path,
    max_age_hours: float = DEFAULT_STALE_HOURS,
    *,
    score_drift: float | None = None,
    on_refreshed: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Non-blocking drift check (24h OR weighted-vs-team-board divergence) for `opsb status` / daemon."""
    fresh = inspect_board_freshness(workspace, max_age_hours=max_age_hours, score_drift=score_drift)
    if os.environ.get("OPSB_DISABLE_BOARD_REFRESH") == "1" or not fresh["should_refresh"]:
        return {**fresh, "auto_triggered": False}

    ws = workspace.expanduser().resolve()
    with _bg_lock:
        if ws in _bg_running:
            return {**fresh, "auto_triggered": False}
        _bg_running.add(ws)

    def _run() -> None:
        try:
            res = refresh_board_and_recalibrate(
                ws, force=False, max_age_hours=max_age_hours, recalibrate=True, score_drift=score_drift
            )
            if isinstance(res, dict) and res.get("ok") and not res.get("skipped") and on_refreshed is not None:
                try:
                    on_refreshed()
                except Exception:
                    pass
        finally:
            with _bg_lock:
                _bg_running.discard(ws)

    threading.Thread(target=_run, daemon=True, name="opsb-board-refresh-24h").start()
    return {**fresh, "auto_triggered": True}
