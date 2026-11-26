"""Read-only access to the platform records a replay needs besides the mesh logs.

Orchestrator DSH conversations, the agent bus database, the test-run registry (with device
hold seconds from each run's ``report.json``), gate verdicts in ``results/feedback-log*.jsonl``
and canonical judge receipts. Nothing here writes; the bus database is opened ``mode=ro``.
"""
from __future__ import annotations

import collections
import json
import sqlite3
from pathlib import Path
from typing import Any

from ..drivers.base import resolve_workspace_state_dir
from ..metrics import usage

#: Orchestrator polling as counted in the 4.2 baseline; ``wait`` (``parhelion_wait``, 4.2 B) replaces it.
POLL_TOOLS = ("agent_wait", "test_wait", "agent_send", "status", "wait")
#: Worker-side waiting calls (workers never call agent_wait or agent_send).
WORKER_POLL_TOOLS = ("test_wait", "status", "agent_inbox", "wait")


def _in(ts: str | None, since: str | None, until: str | None) -> bool:
    if not ts:
        return True
    return not ((since and ts < since) or (until and ts > until))


def orchestrator_sessions(workspace: Path, since: str | None, until: str | None, *,
                          dsh_home: Path | None = None, paths: list[Path] | None = None) -> list[dict[str, Any]]:
    """DSH conversations whose cwd is the workspace root and that called platform tools in the window.

    Returns ``[{path, session_id, tool_counts, calls}]`` where ``calls`` are usage records.
    """
    if paths is None:
        root = usage.dsh_session_path(str(workspace), "x", dsh_home).parent.parent
        paths = sorted(root.glob("*/session.v4.jsonl.zstd")) if root.is_dir() else []
    out: list[dict[str, Any]] = []
    for p in paths:
        try:
            raw = usage.decompress_zstd(Path(p).read_bytes())
        except OSError:
            continue
        if raw is None:
            continue
        events = usage.dsh_events(raw)
        counts: collections.Counter[str] = collections.Counter()
        for ev in events:
            if ev.get("type") != "tool/call" or not _in(usage.iso_from_ms(ev.get("time")), since, until):
                continue
            name = str((ev.get("data") or {}).get("name") or "")
            counts[name.split("__")[-1].removeprefix("parhelion_") if "parhelion_" in name else name] += 1
        if not any(k in counts for k in POLL_TOOLS):
            continue
        sid = Path(p).parent.name
        calls = [c for c in usage.DshParser("orchestrator", sid).feed(events) if _in(c.get("ts"), since, until)]
        out.append({"path": str(p), "session_id": sid, "tool_counts": dict(counts), "calls": calls})
    return out


def bus_message_stats(workspace: Path, since: str | None, until: str | None) -> dict[str, Any] | None:
    db = resolve_workspace_state_dir(Path(workspace).expanduser().resolve()) / "agent_bus.db"
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        rows = conn.execute(
            "SELECT kind, direction, length(CAST(body AS BLOB)), created_at FROM messages "
            "WHERE created_at >= ? AND created_at <= ?", (since or "", until or "9999")).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    by_kind: dict[str, dict[str, int]] = {}
    for kind, direction, size, _ in rows:
        slot = by_kind.setdefault(str(kind), {"count": 0, "bytes": 0, "max_bytes": 0})
        slot["count"] += 1
        slot["bytes"] += int(size or 0)
        slot["max_bytes"] = max(slot["max_bytes"], int(size or 0))
    return dict(sorted(by_kind.items(), key=lambda kv: -kv[1]["bytes"]))


def _report_hold_sec(report_path: Any) -> float | None:
    if not report_path:
        return None
    try:
        rep = json.loads(Path(str(report_path)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    batch = rep.get("device_batch") if isinstance(rep, dict) else None
    return float(batch.get("hold_sec") or 0.0) if isinstance(batch, dict) else None


def test_records(workspace: Path, since: str | None, until: str | None) -> list[dict[str, Any]]:
    """Registry records created in the window, each with ``hold_sec`` (device lock time) when known."""
    root = resolve_workspace_state_dir(Path(workspace).expanduser().resolve()) / "test_runs"
    out = []
    for p in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or not _in(str(rec.get("created_at") or ""), since, until):
            continue
        rec["hold_sec"] = _report_hold_sec(rec.get("report"))
        out.append(rec)
    return out


def gate_rows(workspace: Path, since: str | None, until: str | None) -> list[dict[str, Any]]:
    results = Path(workspace).expanduser() / "results"
    rows = []
    for name in ("feedback-log.1.jsonl", "feedback-log.jsonl"):
        rows.extend(r for r in usage.read_jsonl(results / name)
                    if r.get("kind") == "gate" and _in(str(r.get("ts") or ""), since, until))
    return rows


def receipt_rows(workspace: Path) -> list[dict[str, Any]]:
    """Canonical judged receipts (all time, file order) with ``official_score`` resolved."""
    try:
        from ..services.judge_score_fetch import receipts_module
        rec = receipts_module()
        rows = rec.load(Path(workspace).expanduser())
    except Exception:
        return []
    out = []
    for r in rows:
        if rec.judged(r):
            out.append({**r, "official_score": rec.official_score(r)})
    return out
