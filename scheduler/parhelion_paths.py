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
import math
import os
import pathlib
import re
import time
import uuid
from typing import Any


def sanitize_json_value(obj: Any) -> Any:
    """Recursively convert non-finite floats (NaN/Inf) to None for strict RFC-8259 JSON."""
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: sanitize_json_value(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_json_value(v) for v in obj]
    if isinstance(obj, tuple):
        return [sanitize_json_value(v) for v in obj]
    return obj


def atomic_write_text(path: pathlib.Path, content: str, encoding: str = "utf-8") -> None:
    """Atomically write text via a PID-unique sibling temp file + fsync + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with tmp.open("w", encoding=encoding) as fh:
            fh.write(content)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(
    path: pathlib.Path,
    payload: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = False,
    separators: tuple[str, str] | None = None,
    ensure_ascii: bool = False,
    trailing_newline: bool = True,
) -> None:
    """Atomically serialize JSON (RFC-8259, no NaN/Inf) via PID-unique tmp + os.replace."""
    clean = sanitize_json_value(payload)
    body = json.dumps(
        clean,
        indent=indent,
        sort_keys=sort_keys,
        separators=separators,
        ensure_ascii=ensure_ascii,
        allow_nan=False,
    )
    if trailing_newline:
        body += "\n"
    atomic_write_text(path, body, encoding="utf-8")


def read_json_safe(
    path: pathlib.Path,
    *,
    retries: int = 1,
    retry_delay_sec: float = 0.015,
) -> tuple[Any, bool]:
    """Read a JSON file while distinguishing missing from corrupt/half-written.

    Returns:
      - `(None, True)` when the file does not exist (genuinely absent).
      - `(obj, True)` when parsed cleanly.
      - `(None, False)` when the file exists on disk but is empty/half-written/unreadable.
        Callers MUST NOT treat `(None, False)` as empty state and overwrite or delete `path`.
    """
    if not path.is_file():
        return None, True
    attempts = max(1, int(retries) + 1)
    for idx in range(attempts):
        try:
            raw = path.read_text(encoding="utf-8")
            if raw.strip():
                return json.loads(raw), True
        except (OSError, ValueError):
            pass
        if idx + 1 < attempts:
            time.sleep(max(0.0, retry_delay_sec))
            if not path.is_file():
                return None, True
    return None, False


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
            atomic_write_json(meta_path, payload, indent=2, ensure_ascii=False)
        except OSError:
            pass
    return ws_dir


def read_parhelion_scheduler_config() -> dict[str, Any]:
    """Read `scheduler` section from `~/.parhelion/config.json` (pure stdlib)."""
    cfg_path = get_parhelion_home() / "config.json"
    raw, ok = read_json_safe(cfg_path)
    if not ok or not isinstance(raw, dict):
        return {}
    sched = raw.get("scheduler")
    return dict(sched) if isinstance(sched, dict) else {}


def write_parhelion_scheduler_config(updates: dict[str, Any]) -> None:
    """Sync scheduler config updates back into `~/.parhelion/config.json` when present."""
    cfg_path = get_parhelion_home() / "config.json"
    if not cfg_path.is_file():
        return
    try:
        raw, ok = read_json_safe(cfg_path)
        if not ok or not isinstance(raw, dict):
            return
        sched = raw.setdefault("scheduler", {})
        if not isinstance(sched, dict):
            return
        sched.update(updates)
        atomic_write_json(cfg_path, raw, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _read_session_logged_in(path: pathlib.Path, now_ep: float) -> tuple[bool, str]:
    if not path.is_file():
        return False, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        uid = str(data.get("user_id") or "")
        cookies = data.get("cookies") or []
        raw_la = data.get("logged_in_at")
        ts = float(raw_la) if isinstance(raw_la, (int, float)) else path.stat().st_mtime
        if uid and cookies and (now_ep - ts) < 7 * 86400:
            return True, uid
    except Exception:
        pass
    return False, ""


def list_eligible_account_slots() -> list[dict[str, str]]:
    """Return enabled, logged-in Judge submitter accounts from `~/.parhelion/`."""
    ph = get_parhelion_home()
    now_ep = datetime.datetime.now(datetime.timezone.utc).timestamp()
    reg_path = ph / "cj_accounts.json"
    reg_items: list[dict[str, Any]] = []
    if reg_path.is_file():
        try:
            raw = json.loads(reg_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("accounts"), list):
                reg_items = [x for x in raw["accounts"] if isinstance(x, dict) and x.get("id")]
        except Exception:
            reg_items = []

    slots: list[dict[str, str]] = []
    prim_meta = next((x for x in reg_items if x.get("id") == "primary"), None)
    if prim_meta is None or bool(prim_meta.get("enabled", True)):
        env_p = os.environ.get("JUDGE_SESSION_FILE")
        cands = [pathlib.Path(env_p).expanduser()] if env_p else []
        cands.extend([ph / "sessions" / "primary.json", pathlib.Path.home() / ".judge" / "session.json"])
        for c in cands:
            ok, uid = _read_session_logged_in(c, now_ep)
            if ok:
                slots.append({"id": "primary", "user_id": uid})
                break

    for item in reg_items:
        aid = str(item.get("id") or "")
        if not aid or aid == "primary" or not bool(item.get("enabled", True)):
            continue
        sf = pathlib.Path(str(item.get("session_file") or (ph / "sessions" / f"{aid}.json"))).expanduser()
        if not sf.is_file():
            for alt in (ph / "sessions" / f"{aid}.json", pathlib.Path.home() / ".judge" / "sessions" / f"{aid}.json"):
                if alt.is_file():
                    sf = alt
                    break
        ok, uid = _read_session_logged_in(sf, now_ep)
        if ok:
            slots.append({"id": aid, "user_id": uid})
    return slots
