"""Durable, append-only feedback log for a live contest run.

The 2026 mat-chem contest is a rare end-to-end signal source: gate verdicts, probe
spending, submit rejections with the judge's own wording, and remote build/run timing.
This module records those events as JSONL under `results/feedback-log.jsonl` so a later
pass can reconstruct exactly what happened without re-running anything.

Rules:
  * append-only and never raise into the caller's critical path;
  * one JSON object per line, UTF-8, `ts` first;
  * large blobs (tracebacks, judge bodies) are truncated at LOG_TEXT_LIMIT;
  * the live file rotates to `feedback-log.1.jsonl` once it exceeds LOG_ROTATE_BYTES;
  * high-frequency producers (gate refreshes) use `log_feedback_on_change`, which only
    appends when a per-(kind, key) signature changes.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import pathlib
from typing import Any

LOG_TEXT_LIMIT = 4000
#: Size above which the live log is renamed to `feedback-log.1.jsonl` before the next write.
LOG_ROTATE_BYTES = 8 * 1024 * 1024
SIGNATURES_NAME = ".feedback-signatures.json"


def feedback_log_path(workspace: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(workspace).expanduser() / "results" / "feedback-log.jsonl"


def _rotated_path(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(path.name[: -len(".jsonl")] + ".1.jsonl") if path.name.endswith(".jsonl") else path.with_name(path.name + ".1")


def _rotate_if_needed(path: pathlib.Path) -> None:
    try:
        if path.stat().st_size <= LOG_ROTATE_BYTES:
            return
    except OSError:
        return
    try:
        os.replace(path, _rotated_path(path))
    except FileNotFoundError:
        pass  # a concurrent writer rotated first


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
        _rotate_if_needed(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except Exception:
        return False


def feedback_signatures_path(workspace: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(workspace).expanduser() / "results" / SIGNATURES_NAME


def log_feedback_on_change(
    workspace: pathlib.Path | str, kind: str, key: str, signature: str, **fields: Any
) -> bool:
    """Append one event only when `signature` differs from the last one recorded for
    (`kind`, `key`); returns True when written. Never raises.

    The last signature per key lives in `results/.feedback-signatures.json` as
    `{kind: {key: signature}}`; the read-compare-append-update cycle runs under an
    exclusive `fcntl.flock` on `<same name>.lock` so concurrent evaluators log once.
    """
    try:
        state_path = feedback_signatures_path(workspace)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = state_path.with_name(state_path.name + ".lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    state = {}
                if not isinstance(state, dict):
                    state = {}
                bucket = state.get(str(kind))
                if not isinstance(bucket, dict):
                    bucket = {}
                if bucket.get(str(key)) == signature:
                    return False
                if not log_feedback(workspace, kind, **fields):
                    return False
                bucket[str(key)] = signature
                state[str(kind)] = bucket
                tmp = state_path.with_name(f"{state_path.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
                os.replace(tmp, state_path)
                return True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
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
