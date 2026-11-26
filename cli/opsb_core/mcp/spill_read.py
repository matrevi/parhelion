"""Read one window of a spilled tool result: ``parhelion_spill_read`` (4.2 E).

A result above ``mcp.output_cap_bytes`` is written whole under
``<workspace state>/spill/<agent_id>/`` and only a head and a tail travel. This tool reads the rest
by byte window. A worker may open the files of its own spill directory only; the orchestrator may
open every file under the workspace's spill root. The reply is a window, never a second spill.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .bound import CAP_MIN, READ_DEFAULT, READ_HEADROOM, agent_spill_dir, output_cap, spill_root


def tool_parhelion_spill_read(args: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Read ``limit`` bytes at ``offset`` of one spilled result; ``next_offset`` walks the file."""
    ws = Path(workspace).expanduser().resolve()
    raw = str(args.get("path") or "").strip()
    if not raw:
        raise ValueError("'path' is required: pass the path of a spilled result")
    root = spill_root(ws).resolve()
    target = Path(raw).expanduser()
    if not target.is_absolute():
        target = root / target
    try:
        resolved = target.resolve(strict=True)
    except OSError:
        raise ValueError(f"no spilled result at {raw}") from None
    if not resolved.is_file():
        raise ValueError(f"no spilled result at {raw}")
    if root not in resolved.parents:
        raise PermissionError("the path is outside this workspace's spill directory")
    if args.get("_worker_request"):
        agent = str(args.get("_caller_agent_id") or "").strip()
        if not agent:
            raise PermissionError("A bound worker identity is required")
        if agent_spill_dir(ws, agent).resolve() not in resolved.parents:
            raise PermissionError("a worker reads only its own spilled results")
    ceiling = max(CAP_MIN, output_cap() - READ_HEADROOM)
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(args.get("limit") or min(READ_DEFAULT, ceiling))
    except (TypeError, ValueError):
        limit = min(READ_DEFAULT, ceiling)
    limit = max(1, min(limit, ceiling))
    try:
        with resolved.open("rb") as handle:
            handle.seek(offset)
            window = handle.read(limit)
    except OSError as exc:
        raise ValueError(f"cannot read {resolved}: {exc}") from None
    size = resolved.stat().st_size
    return {"path": str(resolved), "total_bytes": size, "offset": offset, "limit": limit,
            "text": window.decode("utf-8", "replace"),
            "next_offset": offset + len(window) if offset + len(window) < size else None}
