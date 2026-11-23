"""Locked atomic persistence for per-workspace Harness Driver sessions."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any

from .. import config as _cfg
from .base import SessionRecord, resolve_workspace_state_dir


def sessions_file_path(workspace: Path) -> Path:
    return resolve_workspace_state_dir(workspace) / "driver_sessions.json"


def _lock_file_path(workspace: Path) -> Path:
    return resolve_workspace_state_dir(workspace) / ".driver_sessions.lock"


def _read_raw_sessions(path: Path) -> list[dict[str, Any]]:
    data = _cfg.read_json(path, {})
    if isinstance(data, dict) and isinstance(data.get("sessions"), list):
        return [dict(x) for x in data["sessions"] if isinstance(x, dict)]
    if isinstance(data, list):
        return [dict(x) for x in data if isinstance(x, dict)]
    return []


def _write_raw_sessions(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "updated_at": _cfg.utc_now().isoformat(),
        "sessions": rows,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_sessions(workspace: Path) -> list[SessionRecord]:
    ws = workspace.expanduser().resolve()
    rows = _read_raw_sessions(sessions_file_path(ws))
    records = [SessionRecord.from_dict(r) for r in rows]
    records.sort(key=lambda r: r.updated_at or r.created_at, reverse=True)
    return records


def save_session(workspace: Path, record: SessionRecord) -> SessionRecord:
    ws = workspace.expanduser().resolve()
    lock_p = _lock_file_path(ws)
    target_p = sessions_file_path(ws)
    lock_p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_p.open("a", encoding="utf-8") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            rows = _read_raw_sessions(target_p)
            new_row = record.to_dict()
            replaced = False
            for idx, cur in enumerate(rows):
                if cur.get("session_id") == record.session_id:
                    rows[idx] = new_row
                    replaced = True
                    break
            if not replaced:
                rows.append(new_row)
            rows.sort(key=lambda r: str(r.get("updated_at") or r.get("created_at") or ""), reverse=True)
            _write_raw_sessions(target_p, rows)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    return record


def get_session(workspace: Path, session_id: str) -> SessionRecord | None:
    for rec in load_sessions(workspace):
        if rec.session_id == session_id or rec.native_session_id == session_id:
            return rec
    return None


def update_session(
    workspace: Path,
    session_id: str,
    *,
    status: str | None = None,
    pid: int | None = None,
    clear_pid: bool = False,
    native_session_id: str | None = None,
) -> SessionRecord | None:
    ws = workspace.expanduser().resolve()
    lock_p = _lock_file_path(ws)
    target_p = sessions_file_path(ws)
    lock_p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    updated_rec: SessionRecord | None = None
    with lock_p.open("a", encoding="utf-8") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            rows = _read_raw_sessions(target_p)
            for idx, cur in enumerate(rows):
                if cur.get("session_id") == session_id:
                    rec = SessionRecord.from_dict(cur)
                    if status is not None:
                        rec.status = status
                    if clear_pid:
                        rec.pid = None
                    elif pid is not None:
                        rec.pid = pid
                    if native_session_id:
                        rec.native_session_id = native_session_id
                    rec.updated_at = _cfg.utc_now().isoformat()
                    rows[idx] = rec.to_dict()
                    updated_rec = rec
                    break
            if updated_rec is not None:
                rows.sort(key=lambda r: str(r.get("updated_at") or r.get("created_at") or ""), reverse=True)
                _write_raw_sessions(target_p, rows)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    return updated_rec


def list_sessions(
    workspace: Path,
    *,
    problem: str | None = None,
    driver: str | None = None,
    status: str | None = None,
) -> list[SessionRecord]:
    out = load_sessions(workspace)
    if problem:
        p_low = problem.lower()
        out = [r for r in out if r.problem.lower() == p_low]
    if driver:
        d_low = driver.lower()
        out = [r for r in out if r.driver.lower() == d_low]
    if status:
        s_low = status.lower()
        out = [r for r in out if r.status.lower() == s_low]
    return out
