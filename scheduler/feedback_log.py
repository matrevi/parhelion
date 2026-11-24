"""Durable, append-only feedback log for a live contest run.

The 2026 mat-chem contest is a rare end-to-end signal source: gate verdicts, probe
spending, submit rejections with the judge's own wording, and remote build/run timing.
This module records those events as JSONL under `results/feedback-log.jsonl` so a later
pass can reconstruct exactly what happened without re-running anything.

Rules:
  * append-only and never raise into the caller's critical path;
  * one JSON object per line, UTF-8, `ts` first;
  * large blobs (tracebacks, judge bodies) are truncated at LOG_TEXT_LIMIT.
"""
from __future__ import annotations

import datetime
import json
import pathlib
from typing import Any

LOG_TEXT_LIMIT = 4000


def feedback_log_path(workspace: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(workspace).expanduser() / "results" / "feedback-log.jsonl"


def _clip(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= LOG_TEXT_LIMIT else value[:LOG_TEXT_LIMIT] + "…[truncated]"
    if isinstance(value, dict):
        return {str(k): _clip(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clip(v) for v in value]
    return value


def log_feedback(workspace: pathlib.Path | str, kind: str, **fields: Any) -> bool:
    """Append one event; returns True when written. Never raises."""
    try:
        path = feedback_log_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "kind": str(kind),
            **{k: _clip(v) for k, v in fields.items() if v is not None},
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except Exception:
        return False


def tail_feedback(workspace: pathlib.Path | str, limit: int = 100, kind: str = "") -> list[dict[str, Any]]:
    """Read the last `limit` events (optionally filtered by kind), newest last."""
    path = feedback_log_path(workspace)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(limit, 1) * 4:]:
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if kind and doc.get("kind") != kind:
                continue
            rows.append(doc)
    except OSError:
        return []
    return rows[-limit:]
