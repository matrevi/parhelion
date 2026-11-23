"""Atomic read/write/reap helpers for per-account `inflight*.json` submit trackers.

Why this exists: the hot frame (~1 Hz), `compute_account_slots`, and `_inspect_local_worker` all read
the inflight tracker while the detached worker rewrites it on every phase change. A plain
`write_text` truncates first, so readers could observe an empty/partial file, parse it as `{}`
(pid 0) and *unlink a live worker's tracker*, making the submission card vanish until the next phase
write (and dropping the single-operator inflight mutex). Writers now use tmp + `os.replace`, and
readers only reap a tracker whose pid was positively parsed and is dead, after re-validating that
the file was not replaced in between.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

STALE_GARBAGE_AGE_SEC = 30.0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_inflight(path: Path) -> dict[str, Any] | None:
    """Return the parsed tracker dict, or None when missing/unreadable/torn (never treat as `{}`)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def inflight_pid(data: dict[str, Any] | None) -> int:
    if not isinstance(data, dict):
        return 0
    try:
        return int(data.get("pid") or 0)
    except (TypeError, ValueError):
        return 0


def write_inflight_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_inflight_unless_owned(path: Path, payload: dict[str, Any]) -> bool:
    """Parent-side initial write: skip if the worker (same pid) already wrote its own phase.

    Prevents the spawner's late `packaging` write from regressing a worker that has already
    advanced to `waiting_cooldown`/`uploading` (phase + started_at would jump backwards).
    """
    cur = read_inflight(path)
    if cur is not None and inflight_pid(cur) == inflight_pid(payload) and inflight_pid(payload) > 0:
        return False
    write_inflight_atomic(path, payload)
    return True


def reap_inflight_if_stale(path: Path, observed: dict[str, Any] | None) -> bool:
    """Unlink `path` only if it still holds `observed` and that worker pid is provably dead.

    A tracker that is unreadable (possibly mid-write by a legacy writer) is never reaped; a parsed
    tracker without a pid is reaped only once it is older than `STALE_GARBAGE_AGE_SEC`.
    """
    if observed is None:
        return False
    cur = read_inflight(path)
    if cur is None:
        return False
    pid = inflight_pid(cur)
    if pid != inflight_pid(observed) or cur.get("started_at") != observed.get("started_at"):
        return False
    if pid > 0:
        if _pid_alive(pid):
            return False
    else:
        try:
            if (time.time() - path.stat().st_mtime) < STALE_GARBAGE_AGE_SEC:
                return False
        except OSError:
            return False
    path.unlink(missing_ok=True)
    return True


def live_inflight(path: Path) -> dict[str, Any] | None:
    """Return the tracker if its worker pid is alive; reap provably-stale trackers; else None."""
    if not path.is_file():
        return None
    data = read_inflight(path)
    if data is None:
        return None
    pid = inflight_pid(data)
    if pid > 0 and _pid_alive(pid):
        return data
    reap_inflight_if_stale(path, data)
    return None
