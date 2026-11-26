"""MCP tool-call metering: one JSONL row per ``tools/call`` and hourly per-tool aggregates.

Rows go to ``<workspace state>/metrics/mcp-calls.jsonl``:
``{ts, agent_id, role, tool, args_bytes, result_bytes, ms, is_error, spilled, bound_bytes}``.
``result_bytes`` is what the caller received after ``mcp.bound_output``; ``bound_bytes`` is the
complete pre-bound reply, so the two together measure what the output cap removed. The live file
rotates to ``mcp-calls.1.jsonl`` above ``ROTATE_BYTES``. Recording never raises into the tool call.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable

from .. import config as _cfg
from .store import metrics_dir

ROTATE_BYTES = 8 * 1024 * 1024
LOG_NAME = "mcp-calls.jsonl"
ROTATED_NAME = "mcp-calls.1.jsonl"


def log_path(workspace: Path | str) -> Path:
    return metrics_dir(workspace) / LOG_NAME


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def _rotate_if_needed(path: Path, limit: int) -> None:
    lock = _lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            if path.stat().st_size > limit:
                os.replace(path, path.with_name(ROTATED_NAME))
        except OSError:
            pass
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def utf8_len(value: Any) -> int:
    if value is None:
        return 0
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return len(value.encode("utf-8", "replace"))


def record_call(workspace: Path | str, *, agent_id: str | None, role: str, tool: str, args: Any,
                result_text: str, ms: float, is_error: bool, spilled: bool = False,
                bound_bytes: int | None = None, limit: int = ROTATE_BYTES,
                extra: dict[str, Any] | None = None) -> bool:
    """Append one metering row; returns False (never raises) when it could not be written.

    ``extra`` carries the size breakdown a caller already knows (4.3 P27: ``intel_bytes`` of the
    start-up pack). It is stored as-is and ignored by ``summarize``, so an old reader still parses
    the row and the totals keep counting bytes, not fields.
    """
    try:
        path = log_path(workspace)
        served = utf8_len(result_text)
        row = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "agent_id": agent_id or "", "role": role, "tool": tool,
            "args_bytes": utf8_len(args or {}), "result_bytes": served,
            "ms": round(float(ms), 1), "is_error": bool(is_error),
            "spilled": bool(spilled), "bound_bytes": served if bound_bytes is None else int(bound_bytes),
        }
        if extra:
            row["extra"] = dict(extra)
        _rotate_if_needed(path, limit)
        _cfg.append_jsonl_locked(_lock_path(path), path, row)
        return True
    except Exception:
        return False


def read_rows(workspace: Path | str, *, include_rotated: bool = True) -> list[dict[str, Any]]:
    base = metrics_dir(workspace)
    paths = ([base / ROTATED_NAME] if include_rotated else []) + [base / LOG_NAME]
    rows: list[dict[str, Any]] = []
    for p in paths:
        try:
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict):
                        rows.append(obj)
        except OSError:
            continue
    return rows


def p95(values: list[int]) -> int:
    if not values:
        return 0
    vals = sorted(values)
    return int(vals[max(0, math.ceil(0.95 * len(vals)) - 1)])


def summarize(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Per tool and per UTC hour: calls, errors, mean and p95 result bytes, args bytes, ms."""
    buckets: dict[tuple[str, str], dict[str, list[Any]]] = {}
    totals: dict[str, dict[str, list[Any]]] = {}
    for r in rows:
        tool = str(r.get("tool") or "?")
        hour = str(r.get("ts") or "")[:13]
        for key, target in (((tool, hour), buckets), (tool, totals)):
            b = target.setdefault(key, {"result": [], "args": [], "ms": [], "err": []})
            b["result"].append(int(r.get("result_bytes") or 0))
            b["args"].append(int(r.get("args_bytes") or 0))
            b["ms"].append(float(r.get("ms") or 0.0))
            b["err"].append(bool(r.get("is_error")))

    def view(b: dict[str, list[Any]]) -> dict[str, Any]:
        n = len(b["result"])
        return {"calls": n, "errors": sum(b["err"]), "result_bytes_total": sum(b["result"]),
                "result_bytes_mean": round(sum(b["result"]) / n, 1) if n else 0.0,
                "result_bytes_p95": p95(b["result"]), "args_bytes_mean": round(sum(b["args"]) / n, 1) if n else 0.0,
                "ms_mean": round(sum(b["ms"]) / n, 1) if n else 0.0}

    hourly = [{"tool": t, "hour": h, **view(b)} for (t, h), b in sorted(buckets.items(), key=lambda kv: (kv[0][1], kv[0][0]))]
    per_tool = {t: view(b) for t, b in sorted(totals.items(), key=lambda kv: -len(kv[1]["result"]))}
    return {"hourly": hourly, "per_tool": per_tool}
