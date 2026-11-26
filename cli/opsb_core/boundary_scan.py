"""Read-only replay scan for the 4.2 K worker boundary classes.

Splits the analysis path out of ``worker_boundary`` (which owns the live classification and the
alert): a replay walks the two event shapes once and reports ``(worker, time, class)`` rows,
never the tool input or anything it read.
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Iterable

from .worker_boundary import classify_dsh_event, classify_tool


def _epoch(ts: str | None) -> float | None:
    """Seconds since the epoch of an ISO timestamp; None when it cannot be parsed."""
    if not ts:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _row(worker: str, ts: str, tool: str, categories: Iterable[str], source: str) -> dict[str, Any]:
    return {"worker": str(worker), "ts": str(ts), "tool": str(tool), "categories": list(categories), "source": source}


def scan_acp(paths: Iterable[Path], *, since: str | None = None, until: str | None = None,
             worktree: Path | str | None = None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """``(hits, session_id -> agent_id)`` for ACP mesh logs; read-only."""
    from .replay.loader import load_agent

    rows: list[dict[str, Any]] = []
    agents: dict[str, str] = {}
    for path in paths:
        log = load_agent(path, since=since, until=until)
        seat = worktree or next((cwd for _sid, cwd, _ts in log.sessions if cwd), None)
        for session_id, _cwd, _ts in log.sessions:
            agents[session_id] = log.agent_id
        for call in log.tool_calls():
            categories = classify_tool(call.name, call.args or {}, worktree=seat)
            if categories:
                rows.append(_row(log.agent_id, call.ts, call.name, categories, "acp"))
    return rows, agents


def scan_dsh(*, since: str | None = None, until: str | None = None, dsh_home: Path | str | None = None,
             agents_by_session: dict[str, str] | None = None,
             worktrees_only: bool = True) -> list[dict[str, Any]]:
    """Hits in DSH native worker sessions (a session whose cwd is a ``.worktrees/`` seat)."""
    from .metrics import usage

    from .drivers.dsh_driver import _resolve_dsh_home

    home = Path(dsh_home) if dsh_home is not None else _resolve_dsh_home()
    root = home / "sessions"
    if not root.is_dir():
        return []
    since_epoch = _epoch(since)
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/*/session.v4.jsonl.zstd")):
        try:
            if since_epoch and path.stat().st_mtime < since_epoch:
                continue  # last write predates the window: no event of this session can be in it
            raw = usage.decompress_zstd(path.read_bytes())
        except OSError:
            continue
        if not raw:
            continue
        events = usage.dsh_events(raw)
        session_id = path.parent.name
        cwd = next((str(e.get("cwd") or "") for e in events if isinstance(e, dict) and e.get("type") == "session"), "")
        if worktrees_only and ".worktrees" not in cwd:
            continue
        seat = f"{Path(cwd).parent.parent.name}/{Path(cwd).name}" if cwd else session_id
        worker = str((agents_by_session or {}).get(session_id) or seat)
        for event in events:
            ts = usage.iso_from_ms(event.get("time")) if isinstance(event, dict) else None
            if not ts or (since and ts < since) or (until and ts > until):
                continue
            categories = classify_dsh_event(event, worktree=cwd or None)
            if not categories:
                continue
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            rows.append(_row(worker, ts, str(data.get("name") or "tool/call"), categories, "dsh"))
    return rows


def scan(*, since: str | None = None, until: str | None = None, mesh_paths: Iterable[Path] | None = None,
         dsh_home: Path | str | None = None, include_dsh: bool = True) -> list[dict[str, Any]]:
    """All hits of a replay window from both event shapes, deduped per ``(worker, ts, category)``."""
    from .replay.loader import expand_sessions

    acp_rows, agents = scan_acp(list(mesh_paths) if mesh_paths is not None else expand_sessions(),
                                since=since, until=until)
    rows = acp_rows
    if include_dsh:
        rows = rows + scan_dsh(since=since, until=until, dsh_home=dsh_home, agents_by_session=agents)
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        # The two logs carry the same call at different resolutions (ACP row timestamp vs event
        # milliseconds), so a hit is identified to the second; that is also the alert granularity.
        for category in row["categories"]:
            key = (row["worker"], row["ts"][:19], category)
            hit = merged.get(key)
            if hit is None:
                merged[key] = {**row, "ts": row["ts"][:19], "categories": [category], "sources": [row["source"]]}
            elif row["source"] not in hit["sources"]:
                hit["sources"].append(row["source"])
    out = sorted(merged.values(), key=lambda r: (r["ts"], r["worker"], r["categories"][0]))
    for row in out:
        row.pop("source", None)
    return out
