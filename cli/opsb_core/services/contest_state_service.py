"""Per-contest runtime state reset.

A workspace folder that is re-initialised for a *different* contest used to keep the old
contest's score snapshot, board summary, submission queue and scheduler cache, so the new
contest's leaderboard/ranking still showed the previous one. Reset archives that state
instead of deleting it, so an accidental re-init never destroys evidence.
"""
from __future__ import annotations

import datetime
import json
import pathlib
import shutil
from typing import Any

CONTEST_LOCAL_FILES = (
    "results/score-snapshot.json",
    "results/submit-queue.jsonl",
    "results/submit-queue-outcome.jsonl",
    "results/.judge-cooldown.json",
    "results/.submit-freeze",
)
CONTEST_LOCAL_DIRS = (
    "results/scoring-evidence",
)


def _workspace_runtime_dir(workspace: pathlib.Path) -> pathlib.Path:
    try:
        from scheduler.parhelion_paths import workspace_data_dir  # type: ignore

        return pathlib.Path(workspace_data_dir(workspace))
    except Exception:
        return pathlib.Path.home() / ".parhelion" / "workspaces" / workspace.name


def existing_contest_id(workspace: pathlib.Path) -> str:
    path = workspace / "opsb.workspace.json"
    if not path.is_file():
        return ""
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("contest_id") or "")
    except (OSError, ValueError):
        return ""


def reset_contest_state(workspace: pathlib.Path, contest_id: str) -> dict[str, Any]:
    """Archive score/queue/board state so the new contest starts from a clean board."""
    workspace = workspace.expanduser().resolve()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    result: dict[str, Any] = {"archived": [], "runtime": [], "contest_id": contest_id}
    archive = workspace / "results" / f"_archive-{stamp}"
    for rel in CONTEST_LOCAL_FILES:
        src = workspace / rel
        if src.exists():
            archive.mkdir(parents=True, exist_ok=True)
            dest = archive / src.name
            shutil.move(str(src), str(dest))
            result["archived"].append(rel)
    for rel in CONTEST_LOCAL_DIRS:
        src = workspace / rel
        if src.is_dir():
            archive.mkdir(parents=True, exist_ok=True)
            dest = archive / src.name
            shutil.move(str(src), str(dest))
            result["archived"].append(rel)
    runtime = _workspace_runtime_dir(workspace) / "state"
    if runtime.is_dir():
        keep = runtime / f"_archive-{stamp}"
        keep.mkdir(parents=True, exist_ok=True)
        for item in runtime.iterdir():
            if item == keep or item.name.startswith("_archive-"):
                continue
            shutil.move(str(item), str(keep / item.name))
            result["runtime"].append(item.name)
    try:
        from ..ipc_bridge import invalidate_ttl_cache  # type: ignore

        for prefix in ("remote_judge", "scheduler_bridge", "contest_meta", "board"):
            invalidate_ttl_cache(prefix)
    except Exception:
        pass
    return result
