"""Durable stdlib SQLite (WAL) store for Multi-Harness Agent Mesh state & message bus."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .. import config as _cfg
from ..drivers.base import resolve_workspace_state_dir


def agent_bus_db_path(workspace: Path) -> Path:
    state_dir = resolve_workspace_state_dir(workspace.expanduser().resolve())
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "agent_bus.db"


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    driver TEXT NOT NULL,
    role_label TEXT NOT NULL DEFAULT 'worker',
    problem TEXT NOT NULL DEFAULT '',
    collab_mode TEXT NOT NULL DEFAULT 'solo',
    axis TEXT NOT NULL DEFAULT '',
    worktree TEXT NOT NULL DEFAULT '',
    worktree_mode TEXT NOT NULL DEFAULT 'isolated',
    branch TEXT NOT NULL DEFAULT '',
    native_session_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'spawning',
    pid INTEGER,
    pgid INTEGER,
    turn_count INTEGER NOT NULL DEFAULT 0,
    no_gain_streak INTEGER NOT NULL DEFAULT 0,
    unread_for_orch INTEGER NOT NULL DEFAULT 0,
    last_tool TEXT NOT NULL DEFAULT '',
    last_summary TEXT NOT NULL DEFAULT '',
    last_commit_sha TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_active_epoch REAL NOT NULL,
    last_msg_epoch REAL NOT NULL DEFAULT 0,
    last_msg_dir TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    problem TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL,
    kind TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    body TEXT NOT NULL,
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    commit_sha TEXT NOT NULL DEFAULT '',
    acked_by_orch INTEGER NOT NULL DEFAULT 0,
    acked_by_worker INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    epoch REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_agent_seq ON messages (agent_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_epoch ON messages (epoch);
"""


def _row_to_agent(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _row_to_msg(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        arts = json.loads(d.pop("artifacts_json", "[]") or "[]")
        d["artifacts"] = arts if isinstance(arts, list) else []
    except Exception:
        d["artifacts"] = []
    d["acked_by_orch"] = bool(d.get("acked_by_orch"))
    d["acked_by_worker"] = bool(d.get("acked_by_worker"))
    return d


class AgentBusStore:
    """Thread-safe SQLite WAL message bus and agent state repository per workspace."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.db_path = agent_bus_db_path(self.workspace)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA_SQL)
                conn.execute("BEGIN IMMEDIATE")
                # Idempotent column migration for existing agent_bus.db
                a_cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall()}
                for col, ddl in (
                    ("collab_mode", "TEXT NOT NULL DEFAULT 'solo'"),
                    ("axis", "TEXT NOT NULL DEFAULT ''"),
                    ("no_gain_streak", "INTEGER NOT NULL DEFAULT 0"),
                ):
                    if col not in a_cols:
                        conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {ddl}")
                m_cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
                for col, ddl in (
                    ("problem", "TEXT NOT NULL DEFAULT ''"),
                    ("priority", "TEXT NOT NULL DEFAULT 'normal'"),
                    ("event_key", "TEXT"),
                ):
                    if col not in m_cols:
                        conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {ddl}")
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS messages_event ON messages(event_key)")
                conn.commit()
            finally:
                conn.close()

    def create_agent(self, record: dict[str, Any]) -> dict[str, Any]:
        now_iso = _cfg.utc_now().isoformat()
        now_ep = time.time()
        row = {
            "agent_id": str(record["agent_id"]),
            "workspace": str(self.workspace),
            "driver": str(record["driver"]).lower(),
            "role_label": str(record.get("role_label") or "worker")[:24],
            "problem": str(record.get("problem") or ""),
            "collab_mode": str(record.get("collab_mode") or "solo"),
            "axis": str(record.get("axis") or "")[:28],
            "worktree": str(record.get("worktree") or ""),
            "worktree_mode": str(record.get("worktree_mode") or "isolated"),
            "branch": str(record.get("branch") or ""),
            "native_session_id": str(record.get("native_session_id") or ""),
            "status": str(record.get("status") or "spawning"),
            "pid": record.get("pid"),
            "pgid": record.get("pgid"),
            "turn_count": int(record.get("turn_count") or 0),
            "no_gain_streak": int(record.get("no_gain_streak") or 0),
            "unread_for_orch": int(record.get("unread_for_orch") or 0),
            "last_tool": str(record.get("last_tool") or ""),
            "last_summary": str(record.get("last_summary") or ""),
            "last_commit_sha": str(record.get("last_commit_sha") or ""),
            "error": str(record.get("error") or ""),
            "created_at": str(record.get("created_at") or now_iso),
            "updated_at": str(record.get("updated_at") or now_iso),
            "last_active_epoch": float(record.get("last_active_epoch") or now_ep),
            "last_msg_epoch": float(record.get("last_msg_epoch") or 0.0),
            "last_msg_dir": str(record.get("last_msg_dir") or ""),
        }
        cols = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(f"INSERT OR REPLACE INTO agents ({cols}) VALUES ({placeholders})", tuple(row.values()))
                conn.commit()
            finally:
                conn.close()
        return row

    def update_agent(self, agent_id: str, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get_agent(agent_id)
        allowed = {
            "role_label", "problem", "collab_mode", "axis", "worktree", "branch", "native_session_id", "status",
            "pid", "pgid", "turn_count", "no_gain_streak", "unread_for_orch", "last_tool", "last_summary",
            "last_commit_sha", "error", "last_active_epoch", "last_msg_epoch", "last_msg_dir",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        updates["updated_at"] = _cfg.utc_now().isoformat()
        if "last_active_epoch" not in updates:
            updates["last_active_epoch"] = time.time()
        assigns = ", ".join(f"{k} = ?" for k in updates)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(f"UPDATE agents SET {assigns} WHERE agent_id = ?", (*updates.values(), agent_id))
                conn.commit()
                cur = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
                return _row_to_agent(cur) if cur else None
            finally:
                conn.close()

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
                return _row_to_agent(cur) if cur else None
            finally:
                conn.close()

    def list_agents(self, *, include_stopped: bool = True, limit: int = 40) -> list[dict[str, Any]]:
        where = "" if include_stopped else "WHERE status NOT IN ('stopped', 'error')"
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT * FROM agents {where} ORDER BY last_active_epoch DESC LIMIT ?",
                    (-1 if limit == -1 else max(1, int(limit)),),
                ).fetchall()
                return [_row_to_agent(r) for r in rows]
            finally:
                conn.close()

    def append_message(
        self,
        agent_id: str,
        direction: str,
        kind: str,
        body: str,
        *,
        artifacts: list[str] | None = None,
        commit_sha: str = "",
        priority: str = "normal",
        problem: str = "",
        event_key: str | None = None,
    ) -> dict[str, Any]:
        now_iso = _cfg.utc_now().isoformat()
        now_ep = time.time()
        arts = [str(a) for a in (artifacts or []) if str(a).strip()]
        with self._lock:
            conn = self._connect()
            try:
                prob_val = str(problem or "")
                if not prob_val and agent_id:
                    arow = conn.execute("SELECT problem FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
                    if arow:
                        prob_val = str(arow["problem"] or "")
                cur = conn.execute(
                    """
                    INSERT INTO messages (
                        agent_id, problem, direction, kind, priority, body, artifacts_json, commit_sha,
                        acked_by_orch, acked_by_worker, created_at, epoch, event_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(event_key) DO NOTHING
                    """,
                    (
                        agent_id, prob_val, direction, kind, str(priority or "normal"), str(body),
                        json.dumps(arts, ensure_ascii=False), str(commit_sha or ""),
                        1 if direction == "orch_to_worker" else 0,
                        1 if direction == "worker_to_orch" else 0,
                        now_iso, now_ep, event_key,
                    ),
                )
                if not cur.rowcount:
                    return {}
                seq = int(cur.lastrowid or 0)
                if direction == "worker_to_orch" and agent_id:
                    conn.execute(
                        """
                        UPDATE agents
                        SET unread_for_orch = unread_for_orch + 1,
                            last_msg_epoch = ?, last_msg_dir = 'inbound',
                            last_active_epoch = ?, updated_at = ?,
                            last_summary = ?, last_commit_sha = CASE WHEN ? != '' THEN ? ELSE last_commit_sha END
                        WHERE agent_id = ?
                        """,
                        (now_ep, now_ep, now_iso, str(body)[:240], commit_sha, commit_sha, agent_id),
                    )
                elif direction == "orch_to_worker" and agent_id:
                    conn.execute(
                        """
                        UPDATE agents
                        SET last_msg_epoch = ?, last_msg_dir = 'outbound',
                            last_active_epoch = ?, updated_at = ?
                        WHERE agent_id = ?
                        """,
                        (now_ep, now_ep, now_iso, agent_id),
                    )
                conn.commit()
                row = conn.execute("SELECT * FROM messages WHERE seq = ?", (seq,)).fetchone()
                return _row_to_msg(row) if row else {"seq": seq, "agent_id": agent_id, "problem": prob_val, "direction": direction, "kind": kind, "priority": priority, "body": body}
            finally:
                conn.close()

    def list_messages(
        self,
        *,
        agent_id: str | None = None,
        problem: str | None = None,
        since_seq: int = 0,
        direction: str | None = None,
        strategic_only: bool = False,
        recipients: set[str] | None = None,
        for_orchestrator: bool = False,
        limit: int = 60,
    ) -> list[dict[str, Any]]:
        clauses = ["seq > ?"]
        params: list[Any] = [int(since_seq)]
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if problem:
            clauses.append("LOWER(problem) = LOWER(?)")
            params.append(problem)
        if direction:
            clauses.append("direction = ?")
            params.append(direction)
        if recipients:
            clauses.append("agent_id IN (" + ",".join("?" for _ in recipients) + ")")
            params.extend(sorted(recipients))
        if for_orchestrator:
            clauses.append("(direction = 'worker_to_orch' OR kind = 'directive')")
        if strategic_only:
            clauses.append("kind IN ('breakthrough', 'stagnation', 'escape_zero', 'question', 'error', 'directive')")
        params.append(max(1, int(limit)))
        sql = f"SELECT * FROM messages WHERE {' AND '.join(clauses)} ORDER BY seq ASC LIMIT ?"
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, tuple(params)).fetchall()
                return [_row_to_msg(r) for r in rows]
            finally:
                conn.close()

    def ack_messages(self, agent_id: str, up_to_seq: int, reader: str = "orch") -> int:
        col = "acked_by_orch" if reader == "orch" else "acked_by_worker"
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    f"UPDATE messages SET {col} = 1 WHERE agent_id = ? AND seq <= ? AND {col} = 0",
                    (agent_id, int(up_to_seq)),
                )
                if reader == "orch":
                    unread = conn.execute(
                        "SELECT COUNT(*) FROM messages WHERE agent_id = ? AND direction = 'worker_to_orch' AND acked_by_orch = 0",
                        (agent_id,),
                    ).fetchone()[0]
                    conn.execute("UPDATE agents SET unread_for_orch = ? WHERE agent_id = ?", (int(unread), agent_id))
                conn.commit()
                return int(cur.rowcount or 0)
            finally:
                conn.close()

    def latest_seq(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM messages").fetchone()
                return int(row[0] or 0) if row else 0
            finally:
                conn.close()

    def recent_pulses(self, window_sec: float = 10.0, limit: int = 12) -> list[dict[str, Any]]:
        cutoff = time.time() - max(1.0, float(window_sec))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT m.seq, m.agent_id, COALESCE(NULLIF(m.problem, ''), a.problem, '') AS problem,
                           COALESCE(a.driver, '') AS driver,
                           m.direction, m.kind, m.priority, m.body, m.epoch
                    FROM messages m
                    LEFT JOIN agents a ON a.agent_id = m.agent_id
                    WHERE m.epoch >= ?
                    ORDER BY m.seq DESC LIMIT ?
                    """,
                    (cutoff, max(1, int(limit))),
                ).fetchall()
                out = []
                for r in rows:
                    preview = " ".join(str(r["body"] or "").split())[:64]
                    out.append({
                        "seq": int(r["seq"]),
                        "agent_id": str(r["agent_id"]),
                        "problem": str(r["problem"]),
                        "driver": str(r["driver"]),
                        "direction": "outbound" if r["direction"] == "orch_to_worker" else "inbound",
                        "kind": str(r["kind"]),
                        "priority": str(r["priority"] or "normal"),
                        "preview": preview,
                        "epoch": float(r["epoch"]),
                    })
                return out
            finally:
                conn.close()

    def sweep_stale_running(self, live_agent_ids: set[str]) -> int:
        now_iso = _cfg.utc_now().isoformat()
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT agent_id FROM agents WHERE status IN ('spawning', 'working', 'waiting_input', 'idle')"
                ).fetchall()
                stale = [str(r["agent_id"]) for r in rows if str(r["agent_id"]) not in live_agent_ids]
                for aid in stale:
                    conn.execute(
                        "UPDATE agents SET status = 'stopped', pid = NULL, pgid = NULL, updated_at = ? WHERE agent_id = ?",
                        (now_iso, aid),
                    )
                conn.commit()
                return len(stale)
            finally:
                conn.close()

    def cleanup_stopped(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT agent_id FROM agents WHERE status IN ('stopped', 'error')").fetchall()
                ids = [str(r["agent_id"]) for r in rows]
                for aid in ids:
                    conn.execute("DELETE FROM messages WHERE agent_id = ?", (aid,))
                    conn.execute("DELETE FROM agents WHERE agent_id = ?", (aid,))
                conn.commit()
                return len(ids)
            finally:
                conn.close()
