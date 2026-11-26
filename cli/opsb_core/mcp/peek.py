"""``parhelion_agent_peek`` (4.2 G): the orchestrator reads a worker's worktree and session tail.

The orchestrator used to ``cat`` a worker's files, ``tail`` its session log and run ``git diff --stat``
by hand; every one of those calls re-sends the whole result and often needs a ``cd`` first. This module
answers the same three questions with one bounded call and no shell:

- ``files``     a two-level listing of the worker's worktree;
- ``file``      one file, by **relative path inside that worktree**, with an optional line range;
- ``log_tail``  the tail of the worker's session log (read-only; secrets are redacted);
- ``diff_stat`` ``git diff --stat`` and ``--shortstat`` in the worktree.

Bounds: the answer is capped at ``MAX_OUT_BYTES`` (8 KB) and a single file at ``MAX_FILE_BYTES``.
P13 replaces this cap with the shared ``bound_output`` at the one ``tools/call`` exit of
``mcp/server.py``; this module only needs to stay below the current ceiling until then.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

MAX_OUT_BYTES = 8000
MAX_FILE_BYTES = 256 * 1024
MAX_LIST_ENTRIES = 120
MAX_LIST_DEPTH = 2
DEFAULT_LINES = 200
MAX_LINES = 800
LOG_LINES = 40
MAX_LOG_LINES = 200
LOG_LINE_CHARS = 400
LOG_READ_BYTES = 96 * 1024
LIST_SKIP = frozenset({".git", "__pycache__", "node_modules", ".pytest_cache", ".venv", "build"})
SECRET_RE = re.compile(
    r"(?i)\b(set-cookie|cookie|authorization|bearer|token|api[_-]?key|password|secret|"
    r"session[_-]?id|account[_-]?id)\b\s*[:=]\s*[^\s,;\"']+")
RANGE_RE = re.compile(r"^\s*(\d+)\s*(?:[-:,]\s*(\d+))?\s*$")


class PeekError(ValueError):
    """A request outside the tool's contract (unknown agent, escaping path, unreadable file)."""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def redact(text: str) -> str:
    """Replace credential-shaped ``key: value`` pairs; the peek never echoes a token."""
    return SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)


def bus_db(workspace: Path) -> Path:
    """``<workspace>/.../state/agent_bus.db`` without creating the directory (read-only peeks)."""
    from ..drivers.base import resolve_workspace_state_dir
    return resolve_workspace_state_dir(Path(workspace).expanduser().resolve()) / "agent_bus.db"


def agent_row(workspace: Path, agent_id: str) -> dict[str, Any]:
    """The worker's bus row, or an empty dict when the bus or the agent is missing."""
    db = bus_db(workspace)
    if not db.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return {}
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT agent_id, problem, worktree, branch, status FROM agents WHERE agent_id = ?",
            (str(agent_id),)).fetchone()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return dict(row) if row else {}


def worktree_of(workspace: Path, agent_id: str) -> Path:
    row = agent_row(workspace, agent_id)
    raw = str(row.get("worktree") or "").strip()
    if not raw:
        raise PeekError(f"no worktree for agent {agent_id!r}; unknown or unstarted worker")
    return Path(raw).expanduser().resolve()


def resolve_inside(worktree: Path, rel: str) -> Path:
    """Resolve ``rel`` inside ``worktree``; absolute paths and ``..`` escapes are refused."""
    if not str(rel or "").strip():
        raise PeekError("path is required for what=file")
    candidate = Path(str(rel))
    if candidate.is_absolute():
        raise PeekError("path must be relative to the worker's worktree, not absolute")
    root = Path(worktree).expanduser().resolve()
    target = (root / candidate).resolve()
    if target != root and root not in target.parents:
        raise PeekError("path escapes the worker's worktree")
    return target


def _lines_arg(lines: Any) -> tuple[int, int] | None:
    if lines in (None, ""):
        return None
    if isinstance(lines, int):
        start = max(1, int(lines))
        return start, start + DEFAULT_LINES - 1
    m = RANGE_RE.match(str(lines))
    if not m:
        raise PeekError("lines must be a number or a range such as 40-120")
    start = max(1, int(m.group(1)))
    end = int(m.group(2)) if m.group(2) else start + DEFAULT_LINES - 1
    if end < start:
        raise PeekError("lines range ends before it starts")
    return start, min(end, start + MAX_LINES - 1)


def list_files(worktree: Path) -> dict[str, Any]:
    root = Path(worktree)
    entries: list[str] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if len(rel.parts) > MAX_LIST_DEPTH:
            continue
        if any(part in LIST_SKIP for part in rel.parts):
            continue
        if path.is_dir():
            entries.append(f"{rel.as_posix()}/")
        elif path.is_file():
            try:
                size = path.stat().st_size
            except OSError:
                continue
            entries.append(f"{rel.as_posix()} ({size}B)")
        if len(entries) >= MAX_LIST_ENTRIES:
            break
    return {"worktree": str(root), "entries": entries, "count": len(entries),
            "truncated": len(entries) >= MAX_LIST_ENTRIES}


def read_file(worktree: Path, rel: str, lines: Any = None) -> dict[str, Any]:
    target = resolve_inside(worktree, rel)
    if not target.is_file():
        raise PeekError(f"not a file inside the worktree: {rel}")
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise PeekError(f"cannot stat {rel}: {exc}") from exc
    if size > MAX_FILE_BYTES:
        raise PeekError(f"{rel} is {size}B; peeking stops at {MAX_FILE_BYTES}B — read a line range instead")
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise PeekError(f"cannot read {rel}: {exc}") from exc
    if b"\x00" in raw[:2048]:
        raise PeekError(f"{rel} looks binary")
    text = raw.decode("utf-8", errors="replace")
    all_lines = text.splitlines()
    span = _lines_arg(lines) or (1, DEFAULT_LINES)
    start, end = span
    picked = all_lines[start - 1:end]
    body = "\n".join(f"{start + i}: {line}" for i, line in enumerate(picked))
    return {"worktree": str(Path(worktree)), "path": Path(rel).as_posix(), "total_lines": len(all_lines),
            "from": start, "to": min(end, len(all_lines)), "text": redact(body)}


def session_log_path(agent_id: str) -> Path:
    from ..drivers.base import resolve_session_logs_dir
    return resolve_session_logs_dir() / f"mesh-{agent_id}.jsonl"


def _text_of(value: Any, depth: int = 0) -> str:
    """Unwrap the text of one log payload (ACP nests it as ``update.content.text``)."""
    if depth > 3:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_text_of(v, depth + 1) for v in value)
    if isinstance(value, dict):
        for key in ("text", "content", "message", "title", "name"):
            piece = _text_of(value.get(key), depth + 1)
            if piece.strip():
                return piece
    return ""


def _log_line(raw: str) -> str:
    try:
        row = json.loads(raw)
    except ValueError:
        return _clip(raw.strip(), LOG_LINE_CHARS)
    msg = row.get("msg") if isinstance(row, dict) else None
    ts = str((row or {}).get("ts") or "")[:19]
    if not isinstance(msg, dict):
        return _clip(f"{ts} {raw.strip()}", LOG_LINE_CHARS)
    method = str(msg.get("method") or msg.get("type") or "?")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    body = ""
    for key in ("update", "content", "message", "result", "title"):
        body = _text_of(params.get(key))
        if body.strip():
            break
    return _clip(" ".join(p for p in (ts, method, " ".join(body.split())) if p), LOG_LINE_CHARS)


def log_tail(agent_id: str, lines: int = LOG_LINES) -> dict[str, Any]:
    path = session_log_path(agent_id)
    if not path.is_file():
        raise PeekError(f"no session log for agent {agent_id!r} at {path}")
    count = max(1, min(int(lines or LOG_LINES), MAX_LOG_LINES))
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - LOG_READ_BYTES))
            chunk = fh.read()
    except OSError as exc:
        raise PeekError(f"cannot read the session log: {exc}") from exc
    raw_lines = chunk.decode("utf-8", errors="replace").splitlines()
    if size > LOG_READ_BYTES:
        raw_lines = raw_lines[1:]  # a partial first line from the tail read
    tail = [_log_line(line) for line in raw_lines if line.strip()][-count:]
    return {"agent_id": agent_id, "path": str(path), "lines": len(tail),
            "truncated": size > LOG_READ_BYTES or len(raw_lines) > count, "text": redact("\n".join(tail))}


def diff_stat(worktree: Path) -> dict[str, Any]:
    from ..ledger.gitref import git_out
    root = Path(worktree)
    stat = git_out(root, "diff", "--stat") or ""
    short = (git_out(root, "diff", "--shortstat") or "").strip()
    return {"worktree": str(root), "stat": redact(stat.strip()), "shortstat": short,
            "dirty": bool(stat.strip())}


def _max_out() -> int:
    """MAX_OUT_BYTES, held under the MCP output cap (4.2 E) so a peek is answered inline, not spilled."""
    try:
        from .bound import output_cap
        return min(MAX_OUT_BYTES, max(1024, output_cap() - 400))
    except Exception:
        return MAX_OUT_BYTES


def _fit(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the JSON answer under the peek budget by trimming its longest text field."""
    budget = _max_out()
    text = json.dumps(payload, ensure_ascii=False)
    if len(text.encode("utf-8")) <= budget:
        return payload
    for key in ("text", "stat", "entries"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            keep = max(120, len(value) - (len(text.encode("utf-8")) - budget))
            payload[key] = _clip(value, keep)
            payload["truncated"] = True
        elif isinstance(value, list) and value:
            payload[key] = value[: max(1, len(value) // 2)]
            payload["truncated"] = True
        text = json.dumps(payload, ensure_ascii=False)
        if len(text.encode("utf-8")) <= budget:
            break
    return payload


def peek(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """``parhelion_agent_peek(agent_id, what=files|file|log_tail|diff_stat, path?, lines?)``."""
    agent_id = str(args.get("agent_id") or "").strip()
    if not agent_id:
        raise PeekError("agent_id is required")
    what = str(args.get("what") or "files").strip().lower()
    worktree: Path | None = None
    if what != "log_tail":
        worktree = worktree_of(Path(workspace), agent_id)
    if what == "files":
        result = list_files(worktree)  # type: ignore[arg-type]
    elif what == "file":
        result = read_file(worktree, str(args.get("path") or ""), args.get("lines"))  # type: ignore[arg-type]
    elif what == "log_tail":
        result = log_tail(agent_id, int(args.get("lines") or LOG_LINES))
    elif what == "diff_stat":
        result = diff_stat(worktree)  # type: ignore[arg-type]
    else:
        raise PeekError(f"what must be files, file, log_tail or diff_stat, not {what!r}")
    row = agent_row(Path(workspace), agent_id)
    out = {"ok": True, "agent_id": agent_id, "what": what,
           "problem": str(row.get("problem") or ""), "status": str(row.get("status") or "")}
    out.update(result)
    return _fit(out)


__all__ = [
    "MAX_OUT_BYTES",
    "PeekError",
    "agent_row",
    "diff_stat",
    "list_files",
    "log_tail",
    "peek",
    "read_file",
    "redact",
    "resolve_inside",
    "worktree_of",
]
