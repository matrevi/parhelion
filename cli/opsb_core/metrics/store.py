"""Incremental usage parsing with cursors under ``<workspace state>/metrics/usage/``.

Source logs are only ever opened for reading. A cursor remembers how far each source was
consumed (byte offset of the mesh JSONL; compressed bytes of each DSH zstd file, which DSH
grows by appending whole frames), the parser state, the last classified call and the running
summary, so a refresh only parses what was appended. Any inconsistency (a source shrank, a
frame does not decode, a version mismatch) resets the cursor and reparses from the start.

``UsageMonitor`` serves the 1 Hz hot mesh: it returns the cached fields immediately and
refreshes stale agents on one background thread, so building the mesh never decompresses.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from ..drivers.base import resolve_session_logs_dir, resolve_workspace_state_dir
from . import shape, usage

CURSOR_VERSION = 1
CONTINUITY_KEYS = ("ts", "driver", "input_miss", "cache_read", "cache_write", "system_fp", "tools_fp")


def metrics_dir(workspace: Path | str) -> Path:
    return resolve_workspace_state_dir(Path(workspace).expanduser().resolve()) / "metrics"


def _cursor_path(workspace: Path | str, key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)[:80]
    return metrics_dir(workspace) / "usage" / f"{safe}.json"


def _new_cursor(key: str, driver: str) -> dict[str, Any]:
    return {"version": CURSOR_VERSION, "key": key, "driver": driver, "log_offset": 0, "log_state": None,
            "dsh": {}, "last_call": None, "summary": shape.empty_summary(), "status": "unavailable", "reason": ""}


def _load_cursor(path: Path, key: str, driver: str) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and doc.get("version") == CURSOR_VERSION and doc.get("driver") == driver:
            return doc
    except (OSError, ValueError):
        pass
    return _new_cursor(key, driver)


def _save_cursor(path: Path, cur: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


class _Reset(Exception):
    pass


def _read_new_lines(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    """Complete JSONL rows appended after ``offset`` and the new offset (partial tail kept)."""
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset
    if size < offset:
        raise _Reset("log shrank")
    if size == offset:
        return [], offset
    with path.open("rb") as fh:
        fh.seek(offset)
        chunk = fh.read(size - offset)
    end = chunk.rfind(b"\n")
    if end < 0:
        return [], offset
    rows = []
    for line in chunk[: end + 1].splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows, offset + end + 1


def _consume(cur: dict[str, Any], calls: list[dict[str, Any]], ttl_sec: float | None) -> None:
    if not calls:
        return
    shape.classify(calls, ttl_sec=ttl_sec, prev=cur.get("last_call"))
    shape.add_to_summary(cur["summary"], calls)
    cur["last_call"] = {k: calls[-1].get(k) for k in CONTINUITY_KEYS}
    cur["status"], cur["reason"] = "ok", ""


def _advance_dsh(cur: dict[str, Any], key: str, sid: str, path: Path, ttl_sec: float | None) -> None:
    slot = cur["dsh"].setdefault(sid, {"path": str(path), "offset": 0, "state": None})
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < slot["offset"]:
        raise _Reset("dsh session shrank")
    if size == slot["offset"]:
        return
    with path.open("rb") as fh:
        fh.seek(slot["offset"])
        raw = usage.decompress_zstd(fh.read(size - slot["offset"]))
    if raw is None:
        return  # a frame is still being written; retry on the next refresh
    parser = usage.DshParser(key, sid, state=slot["state"], call_seq=int(cur["summary"]["calls"]))
    _consume(cur, parser.feed(usage.dsh_events(raw)), ttl_sec)
    slot["offset"], slot["state"] = size, parser.state


def _advance(cur: dict[str, Any], key: str, log_path: Path | None, dsh_paths: list[tuple[str, Path]],
             dsh_home: Path | None, ttl_sec: float | None) -> None:
    driver = cur["driver"]
    rows: list[dict[str, Any]] = []
    if log_path is not None:
        rows, cur["log_offset"] = _read_new_lines(log_path, int(cur["log_offset"]))
    if driver == "deepseek":
        if log_path is not None:
            tracker = usage.MeshSessions(cur["log_state"])
            for row in rows:
                tracker.feed(row)
            cur["log_state"] = tracker.state
            dsh_paths = dsh_paths + [(sid, usage.dsh_session_path(cwd, sid, dsh_home))
                                     for sid, cwd, _ in tracker.state["sessions"]]
        for sid, path in dsh_paths:
            _advance_dsh(cur, key, sid, path, ttl_sec)
        if cur["status"] != "ok":
            cur["reason"] = "no DSH native session log with usage yet"
    elif driver == "codex":
        parser = usage.CodexParser(key, cur["log_state"])
        _consume(cur, parser.feed(rows), ttl_sec)
        cur["log_state"] = parser.state
    elif driver == "claude":
        parser = usage.ClaudeParser(key, cur["log_state"])
        _consume(cur, parser.feed(rows), ttl_sec)
        cur["log_state"] = parser.state
    else:
        cur["status"], cur["reason"] = "unavailable", "driver logs carry no cache fields"


def refresh(workspace: Path | str, key: str, driver: str, *, log_path: Path | None = None,
            dsh_sessions: list[tuple[str, Path]] | None = None, dsh_home: Path | None = None,
            ttl_sec: float | None = None) -> dict[str, Any]:
    """Advance one agent's cursor and return ``{status, reason, summary}``.

    ``log_path`` is the platform mesh log (``mesh-<id>.jsonl``); ``dsh_sessions`` names DSH
    native session files directly (the orchestrator has no mesh log).
    """
    driver = str(driver or usage.driver_for_agent(key)).lower()
    cpath = _cursor_path(workspace, key)
    cur = _load_cursor(cpath, key, driver)
    sources = list(dsh_sessions or [])
    try:
        _advance(cur, key, log_path, sources, dsh_home, ttl_sec)
    except _Reset:
        cur = _new_cursor(key, driver)
        _advance(cur, key, log_path, sources, dsh_home, ttl_sec)
    cur["updated_at"] = time.time()
    try:
        _save_cursor(cpath, cur)
    except OSError:
        pass
    summary = dict(cur["summary"])
    summary["cache_hit_pct"] = shape.hit_pct(summary)
    return {"status": cur["status"], "reason": cur["reason"], "summary": summary}


def hot_fields(result: dict[str, Any] | None) -> dict[str, Any]:
    """Optional hot-mesh fields; empty when usage is unavailable."""
    if not result or result.get("status") != "ok":
        return {}
    s = result["summary"]
    return {"cache_hit_pct": s.get("cache_hit_pct"), "input_miss_tokens": int(s.get("input_miss") or 0),
            "cache_read_tokens": int(s.get("cache_read") or 0), "last_miss_reason": s.get("last_miss_reason") or ""}


class UsageMonitor:
    """Non-blocking per-agent usage cache refreshed on a single background thread."""

    def __init__(self, min_interval_sec: float = 20.0) -> None:
        self.min_interval_sec = float(min_interval_sec)
        self._lock = threading.Lock()
        self._fields: dict[tuple[str, str], dict[str, Any]] = {}
        self._stamp: dict[tuple[str, str], float] = {}
        self._busy = False

    def fields(self, workspace: Path, targets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """``targets``: ``[{key, driver, log_path?, dsh_sessions?}]``; returns cached fields by key."""
        ws = str(Path(workspace).expanduser().resolve())
        now = time.monotonic()
        with self._lock:
            out = {t["key"]: dict(self._fields.get((ws, t["key"]), {})) for t in targets}
            stale = [t for t in targets if now - self._stamp.get((ws, t["key"]), -1e9) >= self.min_interval_sec]
            start = bool(stale) and not self._busy
            if start:
                self._busy = True
                for t in stale:
                    self._stamp[(ws, t["key"])] = now
        if start:
            threading.Thread(target=self._run, args=(ws, stale), name="usage-monitor", daemon=True).start()
        return out

    def _run(self, ws: str, targets: list[dict[str, Any]]) -> None:
        try:
            for t in targets:
                try:
                    res = refresh(ws, t["key"], t.get("driver") or "", log_path=t.get("log_path"),
                                  dsh_sessions=t.get("dsh_sessions"))
                    with self._lock:
                        self._fields[(ws, t["key"])] = hot_fields(res)
                except Exception:
                    continue
        finally:
            with self._lock:
                self._busy = False


def worker_target(agent_id: str, driver: str) -> dict[str, Any]:
    return {"key": agent_id, "driver": driver, "log_path": resolve_session_logs_dir() / f"mesh-{agent_id}.jsonl"}


def orchestrator_target(session_row: dict[str, Any], driver: str) -> dict[str, Any] | None:
    """Target for the orchestrator's own DSH conversation, when the observer exposes it."""
    sid = str(session_row.get("native_session_id") or "")
    cwd = str(session_row.get("worktree") or session_row.get("workspace") or "")
    if str(driver).lower() != "deepseek" or not sid or not cwd:
        return None
    return {"key": f"orchestrator-{sid[:12]}", "driver": "deepseek",
            "dsh_sessions": [(sid, usage.dsh_session_path(cwd, sid))]}
