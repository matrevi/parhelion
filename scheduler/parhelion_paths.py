"""Unified `~/.parhelion/` data-path resolution shared by `scheduler` and `opsb_core`.

Directory layout under `~/.parhelion/` (overridable via `PARHELION_HOME` / `OPSB_HOME`):
  ~/.parhelion/
  ├── config.json
  ├── cj_accounts.json
  ├── daemon.json / daemon.lock
  ├── cache/
  ├── logs/
  ├── sessions/
  ├── breakthrough/                      # Cross-contest shared structured playbook
  │   ├── schema.json
  │   ├── README.md
  │   ├── index.json
  │   └── entries/*.md
  └── workspaces/
      └── <slug>-<sha1[:8]>/             # Per-workspace isolated runtime data
          ├── meta.json
          ├── bench-specs/               # Per-problem cases_*.json & bench_feedback.json
          └── state/                     # scheduler_state.json, backfill_ledger.jsonl, code_fp_cache.json, etc.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re


def get_parhelion_home() -> pathlib.Path:
    override = os.environ.get("PARHELION_HOME") or os.environ.get("OPSB_HOME")
    if override:
        return pathlib.Path(override).expanduser().resolve()
    return (pathlib.Path.home() / ".parhelion").resolve()


def workspace_id(workspace: pathlib.Path | str) -> str:
    resolved = pathlib.Path(workspace).expanduser().resolve()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", resolved.name).strip("-") or "workspace"
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def workspace_data_dir(workspace: pathlib.Path | str) -> pathlib.Path:
    return get_parhelion_home() / "workspaces" / workspace_id(workspace)


def workspace_bench_specs_dir(workspace: pathlib.Path | str) -> pathlib.Path:
    return workspace_data_dir(workspace) / "bench-specs"


def workspace_state_dir(workspace: pathlib.Path | str) -> pathlib.Path:
    return workspace_data_dir(workspace) / "state"


def breakthrough_dir() -> pathlib.Path:
    return get_parhelion_home() / "breakthrough"


def ensure_workspace_data_dirs(workspace: pathlib.Path | str) -> pathlib.Path:
    resolved = pathlib.Path(workspace).expanduser().resolve()
    ws_dir = workspace_data_dir(resolved)
    (ws_dir / "bench-specs").mkdir(mode=0o700, parents=True, exist_ok=True)
    (ws_dir / "state").mkdir(mode=0o700, parents=True, exist_ok=True)
    meta_path = ws_dir / "meta.json"
    if not meta_path.is_file():
        payload = {
            "workspace": str(resolved),
            "workspace_id": ws_dir.name,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        try:
            meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
    return ws_dir
