"""Configuration repository, workspace discovery, ~/.parhelion/ migration, and atomic JSON/JSONL I/O."""
from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

CLI_DIR = Path(__file__).resolve().parents[1]
SUPERVISOR_ROOT = CLI_DIR.parent


def _resolve_parhelion_home() -> Path:
    override = os.environ.get("PARHELION_HOME") or os.environ.get("OPSB_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".parhelion").resolve()


PARHELION_HOME = _resolve_parhelion_home()
CONFIG_DIR = PARHELION_HOME
CONFIG_PATH = PARHELION_HOME / "config.json"
CACHE_DIR = PARHELION_HOME / "cache"
LOGS_DIR = PARHELION_HOME / "logs"
SESSIONS_DIR = PARHELION_HOME / "sessions"
BREAKTHROUGH_DIR = PARHELION_HOME / "breakthrough"
WORKSPACES_DATA_DIR = PARHELION_HOME / "workspaces"
DAEMON_STATE_PATH = PARHELION_HOME / "daemon.json"
DAEMON_LOCK_PATH = PARHELION_HOME / "daemon.lock"
DAEMON_LOG_PATH = LOGS_DIR / "daemon.log"


def workspace_id(workspace: Path | str) -> str:
    resolved = Path(workspace).expanduser().resolve()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", resolved.name).strip("-") or "workspace"
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def workspace_data_dir(workspace: Path | str) -> Path:
    return PARHELION_HOME / "workspaces" / workspace_id(workspace)


def workspace_bench_specs_dir(workspace: Path | str) -> Path:
    return workspace_data_dir(workspace) / "bench-specs"


def workspace_state_dir(workspace: Path | str) -> Path:
    return workspace_data_dir(workspace) / "state"


def breakthrough_dir() -> Path:
    return PARHELION_HOME / "breakthrough"


def _is_workspace_root(path: Path) -> bool:
    return (path / "opsb.workspace.json").is_file() or (path / "bench" / "problems.json").is_file()


def discover_default_workspace(start: Path | None = None) -> Path | None:
    """Resolve active contest workspace from env, cwd ancestors, or dev checkout (never hardcoded paths)."""
    env_ws = os.environ.get("OPSB_WORKSPACE")
    if env_ws:
        return Path(env_ws).expanduser().resolve()

    search_roots: list[Path] = []
    if start is not None:
        search_roots.append(start.expanduser().resolve())
    try:
        search_roots.append(Path.cwd().resolve())
    except OSError:
        pass
    if SUPERVISOR_ROOT.name != "Resources":
        search_roots.append(SUPERVISOR_ROOT.parent)

    seen: set[Path] = set()
    for base in search_roots:
        for anc in [base, *base.parents]:
            if anc in seen:
                continue
            seen.add(anc)
            if _is_workspace_root(anc):
                return anc
    return None


DEFAULT_BENCH_ROOT: Path | None = discover_default_workspace()

DEFAULT_DIFFICULTIES: dict[str, int] = {
    "ConcatD": 3,
    "ConfusionTransposeD": 3,
    "CTCLossV2": 2,
    "LayerNormV3": 2,
    "MultilabelMarginLoss": 2,
    "PadV3Grad": 3,
    "PReluGradUpdate": 1,
    "QuantUpdateScatter": 2,
    "StridedSliceGrad": 3,
    "SyncBatchNormGatherStats": 2,
}

DEFAULT_CONFIG: dict[str, Any] = {
    "language": "zh",
    "active_workspace": "",
    "workspaces": [],
    "npu_ssh_host": "npu",
    "custom_ssh_hosts": [],
    "scheduler": {
        "auto_consumer_enabled": False,
        "daily_quota_limit": 50,
        "closing_reserve": 12,
        "noise_floor_delta": 2.7,
        "breakthrough_threshold": 5.0,
        "rate_limit_gap_sec": 190,
        "circuit_breaker_limit": 6,
    },
}


def _copy_if_missing(src: Path, dst: Path, mode: int | None = None) -> None:
    if not src.is_file() or dst.exists():
        return
    dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.migtmp")
    shutil.copy2(src, tmp)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, dst)


def ensure_parhelion_migrated() -> None:
    """Losslessly migrate legacy ~/.config/parhelion and ~/.config/opsb state into ~/.parhelion/."""
    PARHELION_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    LOGS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)

    # Only run implicit home migration when targeting the default ~/.parhelion (or when marker absent)
    home = Path.home()
    legacy_parhelion = home / ".config" / "parhelion"
    legacy_opsb = home / ".config" / "opsb"

    if PARHELION_HOME == (home / ".parhelion").resolve():
        # Migrate config & daemon state when missing in ~/.parhelion/
        if not CONFIG_PATH.exists():
            if (legacy_parhelion / "config.json").is_file():
                _copy_if_missing(legacy_parhelion / "config.json", CONFIG_PATH, mode=0o600)
            elif (legacy_opsb / "config.json").is_file():
                _copy_if_missing(legacy_opsb / "config.json", CONFIG_PATH, mode=0o600)

        for src_dir in (legacy_parhelion, legacy_opsb):
            if not src_dir.is_dir():
                continue
            _copy_if_missing(src_dir / "cj_accounts.json", PARHELION_HOME / "cj_accounts.json", mode=0o600)
            _copy_if_missing(src_dir / "daemon.json", DAEMON_STATE_PATH, mode=0o600)
            _copy_if_missing(src_dir / "daemon.log", DAEMON_LOG_PATH)
            _copy_if_missing(src_dir / "last_jump_submit.log", LOGS_DIR / "last_jump_submit.log")
            for p in src_dir.iterdir():
                if not p.is_file():
                    continue
                if p.name.startswith("inflight") and p.suffix == ".json":
                    _copy_if_missing(p, PARHELION_HOME / p.name, mode=0o600)
                elif p.name.startswith(".cache_") or p.name.startswith(".board_"):
                    _copy_if_missing(p, CACHE_DIR / p.name)
            legacy_sessions = src_dir / "sessions"
            if legacy_sessions.is_dir():
                for sp in legacy_sessions.glob("*.json"):
                    _copy_if_missing(sp, SESSIONS_DIR / sp.name, mode=0o600)


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def parse_iso(ts: str | None) -> datetime.datetime | None:
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s:
            try:
                rows.append(json.loads(s))
            except Exception:
                continue
    return rows


def append_jsonl_locked(lock_path: Path, target_path: Path, record: dict[str, Any]) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            with target_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def read_ttl_cache(name: str, ttl_sec: float, fingerprint: str = "") -> Any | None:
    p = CACHE_DIR / f".cache_{name}.json"
    if not p.is_file():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - float(obj.get("ts", 0)) <= ttl_sec:
            if not fingerprint or obj.get("fp") == fingerprint:
                return obj.get("data")
    except Exception:
        pass
    return None


def write_ttl_cache(name: str, data: Any, fingerprint: str = "") -> None:
    try:
        CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        p = CACHE_DIR / f".cache_{name}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"ts": time.time(), "fp": fingerprint, "data": data}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, p)
    except Exception:
        pass


def invalidate_ttl_cache(name: str) -> None:
    try:
        (CACHE_DIR / f".cache_{name}.json").unlink(missing_ok=True)
    except Exception:
        pass


def resolve_python_exe(workspace: Path | None = None) -> str:
    env_py = os.environ.get("OPSB_PYTHON")
    if env_py and Path(env_py).is_file() and os.access(env_py, os.X_OK):
        return env_py
    candidates: list[Path] = []
    if workspace is not None:
        candidates.append(workspace / ".venv" / "bin" / "python")
    candidates.append(PARHELION_HOME / "venv" / "bin" / "python")
    detected = discover_default_workspace()
    if detected is not None:
        candidates.append(detected / ".venv" / "bin" / "python")
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    import sys
    return sys.executable


class ConfigRepository:
    @staticmethod
    def load() -> dict[str, Any]:
        ensure_parhelion_migrated()
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        raw = read_json(CONFIG_PATH)
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k == "scheduler" and isinstance(v, dict):
                    cfg["scheduler"].update(v)
                else:
                    cfg[k] = v

        ws_list = [str(w) for w in (cfg.get("workspaces") or []) if str(w).strip()]
        active = str(cfg.get("active_workspace") or "").strip()
        if not active:
            detected = discover_default_workspace()
            if detected is not None:
                active = str(detected)
        if active and active not in ws_list:
            ws_list.insert(0, active)
        cfg["active_workspace"] = active
        cfg["workspaces"] = ws_list
        return cfg

    @staticmethod
    def save(cfg: dict[str, Any]) -> None:
        PARHELION_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)

    @staticmethod
    def resolve_workspace(explicit: str | None, cfg: dict[str, Any]) -> Path:
        if explicit and str(explicit).strip():
            return Path(explicit).expanduser().resolve()
        env_ws = os.environ.get("OPSB_WORKSPACE")
        if env_ws:
            return Path(env_ws).expanduser().resolve()
        cwd = Path.cwd().resolve()
        for anc in [cwd, *cwd.parents]:
            if _is_workspace_root(anc):
                return anc
        active = str(cfg.get("active_workspace") or "").strip()
        if active:
            return Path(active).expanduser().resolve()
        detected = discover_default_workspace()
        if detected is not None:
            return detected
        return cwd
