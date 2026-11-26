"""One bound for every MCP tool reply (4.2 E): a byte cap, page-first lists and a spill file.

``mcp.server`` calls :func:`bound_output` once per ``tools/call`` result, so paging, spilling and
read-back share one exit instead of growing a second:

* a reply at or under ``mcp.output_cap_bytes`` (default 5000) is returned unchanged;
* a pageable reply above the cap is first cut to a smaller page with ``next_cursor`` (``paging``);
* otherwise the complete JSON is written to
  ``<workspace state>/spill/<agent_id>/<ts>-<tool>.json`` and the reply becomes
  ``{spilled, path, total_bytes, head, tail}``; ``parhelion_spill_read`` reads the rest.

``head`` is a structured excerpt: top-level scalars and short lists are copied whole, then whatever
else still fits the budget. ``tail`` is the end of the serialized JSON cut at a token boundary, so an
error message at the end survives. Spill files are reap-able copies (7 days, 200 MB); peer
attachments share the listing in ``opsb doctor clean`` but are never deleted implicitly.
"""
from __future__ import annotations

import datetime
import json
import re
import time
from pathlib import Path
from typing import Any

from ..drivers.base import load_parhelion_config, resolve_workspace_state_dir
from .paging import PAGE_KEYS, repage, size_of

CAP_DEFAULT = 5000
CAP_MIN = 256
HEAD_BYTES = 1024
TAIL_BYTES = 1024
SHORT_LIST_BYTES = 240
READ_DEFAULT = 3000
READ_HEADROOM = 700
SPILL_KEEP_DAYS = 7
SPILL_MAX_MB = 200
SPILL_SECTIONS = ("spill", "peer")
NO_SECOND_SPILL = ("parhelion_spill_read",)
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


# ---------------------------------------------------------------- settings and paths

def _mcp_section() -> dict[str, Any]:
    cfg = load_parhelion_config()
    section = cfg.get("mcp") if isinstance(cfg, dict) else None
    return section if isinstance(section, dict) else {}


def _setting(name: str, default: int, floor: int = 1) -> int:
    try:
        value = int(_mcp_section().get(name))
    except (TypeError, ValueError):
        return default
    return max(floor, value)


def output_cap() -> int:
    """``mcp.output_cap_bytes``: the largest reply, in UTF-8 bytes, returned whole."""
    return _setting("output_cap_bytes", CAP_DEFAULT, CAP_MIN)


def spill_keep_days() -> int:
    return _setting("spill_keep_days", SPILL_KEEP_DAYS)


def spill_max_bytes() -> int:
    return _setting("spill_max_mb", SPILL_MAX_MB) * 1024 * 1024


def _safe(value: Any, fallback: str = "orchestrator") -> str:
    return _SAFE_NAME.sub("_", str(value or "")).strip("._-")[:64] or fallback


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def spill_root(workspace: Path | str) -> Path:
    """``<workspace state>/spill``; the state dir resolves exactly like ``test_runs.registry_dir``."""
    return resolve_workspace_state_dir(Path(workspace).expanduser()) / "spill"


def agent_spill_dir(workspace: Path | str, agent_id: str | None) -> Path:
    return spill_root(workspace) / _safe(agent_id)


# ---------------------------------------------------------------- excerpts

def _clip_text(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def head_sample(payload: Any, budget: int = HEAD_BYTES) -> Any:
    """Start of a spilled result: top-level scalars and short lists first, then what still fits."""
    if not isinstance(payload, dict):
        return _clip_text(compact(payload), budget)
    sampled: dict[str, Any] = {}
    keys = list(payload)
    left = budget - 2
    for key in keys:  # short values and short lists are kept whole before anything is cut
        value = payload[key]
        if isinstance(value, list) and size_of({key: value}) > SHORT_LIST_BYTES:
            continue
        cost = size_of({key: value})
        if cost <= left:
            sampled[key] = value
            left -= cost
    for key in keys:
        if key in sampled:
            continue
        value = payload[key]
        if isinstance(value, list):
            taken: list[Any] = []
            for item in value:
                cost = size_of(item) + 2
                if cost > left:
                    break
                taken.append(item)
                left -= cost
            if taken:
                sampled[key] = taken
        elif isinstance(value, dict):
            scalars = {name: sub for name, sub in value.items() if not isinstance(sub, (list, dict))}
            cost = size_of({key: scalars})
            if cost <= left:
                sampled[key] = scalars
                left -= cost
        else:
            room = max(0, left - len(key) - 6)
            if room >= 24:
                sampled[key] = _clip_text(compact(value), room)
                left -= room
    omitted = [key for key in keys if key not in sampled]
    if omitted:
        sampled["_more_keys"] = omitted[:24]
    return sampled


def _splits_escape(text: str, index: int) -> bool:
    """True when ``index`` would start the tail in the second half of a JSON escape."""
    backslashes = 0
    probe = index - 1
    while probe >= 0 and text[probe] == "\\":
        backslashes += 1
        probe -= 1
    return backslashes % 2 == 1


def tail_sample(text: str, budget: int = TAIL_BYTES) -> str:
    """End of a spilled result (errors live there): a whole number of JSON escapes and characters."""
    if len(text.encode("utf-8")) <= budget:
        return text
    cut = max(0, len(text) - budget)
    while cut < len(text) and len(text[cut:].encode("utf-8")) > budget:
        cut += 1
    while cut < len(text) and _splits_escape(text, cut):
        cut += 1
    return "…" + text[cut:]


# ---------------------------------------------------------------- spill and retention

def _stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def write_spill(workspace: Path | str, tool: str, text: str, *, agent_id: str | None = None) -> Path:
    """Write the complete result under its agent and return the path (never reuses a name)."""
    directory = agent_spill_dir(workspace, agent_id)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{_stamp()}-{_safe(tool, 'tool')}"
    path = directory / f"{stem}.json"
    index = 0
    while path.exists():
        index += 1
        path = directory / f"{stem}-{index}.json"
    path.write_text(text, encoding="utf-8")
    return path


def stale_files(folder: Path, cutoff: float, budget: int, now: float) -> dict[Path, str]:
    """Files beyond the age window or the total size budget (newest kept first)."""
    entries: list[tuple[Path, float, int]] = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file() and not p.is_symlink()):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((path, stat.st_mtime, stat.st_size))
    stale = {path: f"older than {max(0, int((now - cutoff) / 86400))}d"
             for path, mtime, _ in entries if mtime <= cutoff}
    used = 0
    for path, _mtime, size in sorted((entry for entry in entries if entry[0] not in stale),
                                     key=lambda entry: entry[1], reverse=True):
        used += size
        if used > budget:
            stale[path] = f"over {max(1, budget // (1024 * 1024))} MB budget"
    return stale


def reap(workspace: Path | str, *, keep_days: int | None = None, max_bytes: int | None = None,
         now: float | None = None) -> list[str]:
    """Delete spill files past the retention policy; returns the removed paths."""
    root = spill_root(workspace)
    if not root.is_dir():
        return []
    keep = spill_keep_days() if keep_days is None else int(keep_days)
    budget = spill_max_bytes() if max_bytes is None else int(max_bytes)
    clock = time.time() if now is None else float(now)
    removed: list[str] = []
    for path in sorted(stale_files(root, clock - keep * 86400, budget, clock)):
        try:
            path.unlink()
            removed.append(str(path))
        except OSError:
            continue
    return removed


def scan_retention(home: Path | str, *, keep_days: int | None = None, max_bytes: int | None = None,
                   now: float | None = None) -> list[dict[str, Any]]:
    """Spill and peer artifacts beyond the retention policy, for ``opsb doctor clean`` to list."""
    root = Path(home).expanduser() / "workspaces"
    if not root.is_dir():
        return []
    keep = spill_keep_days() if keep_days is None else int(keep_days)
    budget = spill_max_bytes() if max_bytes is None else int(max_bytes)
    clock = time.time() if now is None else float(now)
    hits: list[dict[str, Any]] = []
    for state in sorted(path for path in root.glob("*/state") if path.is_dir()):
        for section in SPILL_SECTIONS:
            folder = state / section
            if not folder.is_dir():
                continue
            for path, reason in sorted(stale_files(folder, clock - keep * 86400, budget, clock).items()):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                hits.append({"path": str(path), "workspace": state.parent.name, "section": section,
                             "reason": reason, "age_days": round((clock - stat.st_mtime) / 86400.0, 1),
                             "size_bytes": stat.st_size, "applied": False})
    return hits


def purge_retention(hits: list[dict[str, Any]]) -> None:
    """Delete listed retention hits (the doctor's explicit ``--purge-retention --apply``)."""
    for hit in hits:
        try:
            Path(str(hit["path"])).unlink()
            hit["applied"] = True
        except FileNotFoundError:
            hit["applied"] = True
        except OSError as exc:
            hit["error"] = str(exc)


# ---------------------------------------------------------------- the bound itself

def bound_output(tool: str, payload: Any, workspace: Path | str, *, agent_id: str | None = None,
                 cap: int | None = None) -> tuple[str, dict[str, Any]]:
    """Bound one tool reply: return ``(text, meta)`` with ``spilled`` and the pre-bound bytes."""
    text = compact(payload)
    total = len(text.encode("utf-8"))
    limit = output_cap() if cap is None else max(CAP_MIN, int(cap))
    if total <= limit:
        return text, {"spilled": False, "bound_bytes": total}
    if tool in PAGE_KEYS:
        paged = repage(tool, payload, limit)
        if paged is not None:
            return compact(paged), {"spilled": False, "bound_bytes": total}
    if tool in NO_SECOND_SPILL:
        return text, {"spilled": False, "bound_bytes": total}
    head, tail = head_sample(payload), tail_sample(text)
    try:
        path = write_spill(workspace, tool, text, agent_id=agent_id)
        reap(workspace)
        envelope: dict[str, Any] = {"spilled": True, "path": str(path), "total_bytes": total,
                                    "head": head, "tail": tail}
    except OSError as exc:  # a full disk must not fail the tool call
        envelope = {"spilled": False, "truncated": True, "spill_error": str(exc),
                    "total_bytes": total, "head": head, "tail": tail}
    return compact(envelope), {"spilled": bool(envelope.get("spilled")), "bound_bytes": total}
