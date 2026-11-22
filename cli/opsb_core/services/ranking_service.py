"""Contest-wide team ranking (the `/public/ictcompetition/ranking` board).

The public ranking page is an SPA; its data comes from
``GET /api/submissions/contest/<contest_id>/stats`` which returns one row per
team: ``{team: {team_name, ...}, score, passCount, submissionCount}``.
Ranking mode for this contest is ``score`` (descending), with pass count and
submission count as stable tie-breakers.

The board is cached on disk so `opsb status` never blocks on the network:
a stale cache is served immediately, and a refresh happens at most once per
``ATTEMPT_INTERVAL_SEC``. Submissions completing (``submit_runner``) force a
refresh in a background thread so the app's card follows every new result.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_CONTEST_ID = "6a9e2d04bf41025d60f21804"
DEFAULT_TEAM_NAME = "XJU第二少年团"

CACHE_TTL_SEC = 30.0           # serve-fresh window for status reads (30s)
ATTEMPT_INTERVAL_SEC = 15.0    # minimum gap between background ranking refreshes
REQUEST_TIMEOUT_SEC = 6.0

_refresh_lock = threading.Lock()


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _cache_path(workspace: Path) -> Path:
    return workspace / "results" / "scoring-evidence" / "team-ranking.json"


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("rank") is None:
            return None
        return data
    except Exception:
        return None


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _age_sec(payload: dict[str, Any] | None) -> float:
    if not payload:
        return float("inf")
    stamp = payload.get("fetched_at") or payload.get("attempt_at") or ""
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except Exception:
        return float("inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())


def _session() -> tuple[str, str]:
    """Return (base_url, cookie_header) from the official CLI session, dependency-free."""
    import json as _json
    import os

    path = Path(os.environ.get("JUDGE_SESSION_FILE") or (Path.home() / ".judge" / "session.json"))
    data = _json.loads(path.read_text(encoding="utf-8"))
    base = str(data.get("base_url") or "https://judge.invalid").rstrip("/")
    jar = []
    for rec in data.get("cookies") or []:
        name = rec.get("name")
        value = rec.get("value")
        if name and value is not None:
            jar.append(f"{name}={value}")
    return base, "; ".join(jar)


def _get_json(path: str) -> Any:
    """GET a Judge API path using only the stdlib (opsb may run without requests)."""
    import json as _json
    import urllib.request

    base, cookie = _session()
    req = urllib.request.Request(base + path, headers={
        "Accept": "application/json",
        "Cookie": cookie,
        "User-Agent": "opsb-ranking/1.0",
    })
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def fetch_team_ranking(
    workspace: Path,
    *,
    contest_id: str = DEFAULT_CONTEST_ID,
    team_name: str = DEFAULT_TEAM_NAME,
) -> dict[str, Any] | None:
    """Fetch the whole contest board and locate our team by name. Blocking."""
    rows = _get_json(f"/api/submissions/contest/{contest_id}/stats")
    if not isinstance(rows, list):
        raise RuntimeError("ranking payload is not a list")

    scored = []
    for row in rows:
        try:
            score = float(row.get("score") or 0.0)
        except Exception:
            score = 0.0
        scored.append((score, int(row.get("passCount") or 0), int(row.get("submissionCount") or 0), row))
    scored.sort(key=lambda item: (-item[0], -item[1], -item[2]))

    wanted = (team_name or "").strip()
    for idx, (score, pass_count, sub_count, row) in enumerate(scored, start=1):
        name = str((row.get("team") or {}).get("team_name") or "").strip()
        if name and name == wanted:
            return {
                "team_name": name,
                "rank": idx,
                "total": len(scored),
                "score": round(score, 2),
                "pass_count": pass_count,
                "submission_count": sub_count,
                "contest_id": contest_id,
                "source": "api/submissions/contest/stats",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
    raise RuntimeError(f"team '{wanted}' not found in {len(scored)} rows")


def team_ranking(
    workspace: Path | None = None,
    *,
    contest_id: str = DEFAULT_CONTEST_ID,
    team_name: str = DEFAULT_TEAM_NAME,
    max_age: float = CACHE_TTL_SEC,
    allow_fetch: bool = True,
    force: bool = False,
) -> dict[str, Any] | None:
    """Cached ranking. Never raises; returns the stale cache when the network fails."""
    workspace = Path(workspace) if workspace else _workspace_root()
    path = _cache_path(workspace)
    cached = _read_cache(path)

    if not force and cached and _age_sec(cached) <= max_age:
        return cached
    if not allow_fetch:
        return cached
    if not force and cached:
        # throttle attempted-but-failed refreshes as well
        last_try = cached.get("attempt_at")
        probe = {"fetched_at": last_try} if last_try else None
        if probe and _age_sec(probe) < ATTEMPT_INTERVAL_SEC:
            return cached

    if not _refresh_lock.acquire(blocking=False):
        return cached
    try:
        payload = fetch_team_ranking(workspace, contest_id=contest_id, team_name=team_name)
        _write_cache(path, payload)
        return payload
    except Exception:
        import os
        if os.environ.get("OPSB_RANKING_DEBUG"):
            import traceback
            with open("/tmp/ranking-debug.log", "a") as _fh:
                _fh.write("---- failure ----\n" + traceback.format_exc())
        if cached:
            cached = dict(cached)
            cached["attempt_at"] = datetime.now(timezone.utc).isoformat()
            _write_cache(path, cached)
        return cached
    finally:
        _refresh_lock.release()


def refresh_async(
    workspace: Path | None = None,
    *,
    contest_id: str = DEFAULT_CONTEST_ID,
    team_name: str = DEFAULT_TEAM_NAME,
    force: bool = True,
) -> None:
    """Spawn a detached non-blocking background process to refresh team ranking.

    Unlike a daemon Thread (which dies immediately when a short-lived CLI process exits),
    a detached subprocess reliably completes the HTTP request and updates `team-ranking.json`
    without ever blocking the 0.5s/1s UI polling loop.
    """
    import fcntl
    import os
    import subprocess

    ws = Path(workspace) if workspace else _workspace_root()
    path = _cache_path(ws)
    if not force:
        cached = _read_cache(path)
        if cached:
            last_try = cached.get("attempt_at") or cached.get("fetched_at")
            if last_try and _age_sec({"fetched_at": last_try}) < ATTEMPT_INTERVAL_SEC:
                return

    lock_path = path.with_suffix(".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lf, fcntl.LOCK_UN)
            except BlockingIOError:
                return  # another background ranking refresh is already running
    except Exception:
        pass

    pkg_root = str(Path(__file__).resolve().parents[2])
    code = (
        "import fcntl, pathlib, sys, time\n"
        f"sys.path.insert(0, {pkg_root!r})\n"
        "from opsb_core.services import ranking_service\n"
        f"ws = pathlib.Path({str(ws)!r})\n"
        f"lp = pathlib.Path({str(lock_path)!r})\n"
        "lp.parent.mkdir(parents=True, exist_ok=True)\n"
        "with lp.open('a') as lf:\n"
        "    try:\n"
        "        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "    except BlockingIOError:\n"
        "        sys.exit(0)\n"
        "    try:\n"
        f"        if {bool(force)!r}: time.sleep(1.0)\n"
        f"        ranking_service.team_ranking(ws, contest_id={contest_id!r}, team_name={team_name!r}, force=True)\n"
        "    finally:\n"
        "        fcntl.flock(lf, fcntl.LOCK_UN)\n"
    )
    try:
        subprocess.Popen(
            [sys.executable, "-c", code],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception:
        pass


def summary_fields(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten a ranking payload into the additive `contest` keys the app decodes."""
    if not payload:
        return {}
    return {
        "team_rank": payload.get("rank"),
        "total_teams": payload.get("total"),
        "team_rank_score": payload.get("score"),
        "team_rank_pass_count": payload.get("pass_count"),
        "team_rank_fetched_at": payload.get("fetched_at"),
    }
