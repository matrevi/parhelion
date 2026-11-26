"""Configuration repository, workspace discovery, ~/.parhelion/ migration, and atomic JSON/JSONL I/O."""
from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .device_env import device_text

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


def canonical_workspace(workspace: Path | str) -> Path:
    """Map a worker worktree (``<contest>/<problem>/.worktrees/<track>``) to its contest root.

    Workers run tools from their worktree; keying platform state by that path would split
    a contest's state across one data directory per worktree.
    """
    resolved = Path(workspace).expanduser().resolve()
    parts = resolved.parts
    if ".worktrees" in parts:
        for parent in Path(*parts[:parts.index(".worktrees")]).parents:
            if (parent / "opsb.workspace.json").is_file():
                return parent
    return resolved


def workspace_id(workspace: Path | str) -> str:
    resolved = canonical_workspace(workspace)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", resolved.name).strip("-") or "workspace"
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def workspace_data_dir(workspace: Path | str) -> Path:
    return _resolve_parhelion_home() / "workspaces" / workspace_id(workspace)


def workspace_bench_specs_dir(workspace: Path | str) -> Path:
    return workspace_data_dir(workspace) / "bench-specs"


def workspace_state_dir(workspace: Path | str) -> Path:
    return workspace_data_dir(workspace) / "state"


def breakthrough_dir() -> Path:
    return _resolve_parhelion_home() / "breakthrough"


def _is_workspace_root(path: Path) -> bool:
    return (path / "opsb.workspace.json").is_file() or (path / device_text("work_prefix") / "problems.json").is_file()


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
    "npu_ssh_hosts": [],  # 4.3.2: extra device hosts; see opsb_core.device_hosts
    "custom_ssh_hosts": [],
    "scheduler": {
        "auto_consumer_enabled": False,
        "daily_quota_limit": 50,
        "closing_reserve": 2,
        "noise_floor_delta": 2.5,
        "breakthrough_threshold": 5.0,
        "rate_limit_gap_sec": 190,
        "circuit_breaker_limit": 6,
    },
    # 4.2 E: the MCP output bound. The cap is the largest reply returned whole; a bigger result is
    # paged or spilled under <workspace state>/spill and reaped by age and total size.
    "mcp": {
        "output_cap_bytes": 5000,
        "spill_keep_days": 7,
        "spill_max_mb": 200,
    },
    # 4.3 P26: replace a stagnant seat automatically after the six-attempt stagnation event. Off by
    # default (00-PLAN D4): the platform prepares the handover and the orchestrator calls
    # `parhelion_sprint_start(replace=[...])`; turning this on lets the platform run that call itself.
    "agents": {"auto_replace_on_stagnation": False},
    # 4.2 G worker start-up pack. ``tokens`` caps the whole pack (~6K tokens by default) and
    # ``repo_map`` stays off until a replay shows the generated map pays for itself (research R11).
    "startup_pack": {"tokens": 6000, "repo_map": False},
    # 4.4 P30-P32 stuck guidance (docs/v4.4/stuck-guidance.md). ``enabled`` runs the detector and the
    # dossier; ``cards`` delivers guide cards (test_wait reply, idle inbox). Thresholds per class.
    "guidance": {
        "enabled": True, "cards": True, "tick_sec": 30,
        "build_loop_runs": 3, "wa_loop_runs": 3,
        "plateau_runs": 8, "plateau_minutes": 40, "plateau_min_runs": 2, "plateau_noise": 0.03,
        "no_candidate_minutes": 60,
        "scale_ratio": 50, "scale_top_cases": 3, "scale_min_outliers": 1, "scale_small_change": 0.10,
        "scale_recent_runs": 3,
        "tier_bounds": [3, 50], "tier_factors": {"algorithm": 10, "datapath": 2, "tuning": 1.3},
        "repeat_minutes": 30, "inbox_after_sec": 600, "outcome_window_minutes": 120,
        "card_max_bytes": 800,
    },
}


_MIGRATE_LOCK = threading.Lock()
_MIGRATED_HOMES: set[Path] = set()


def _copy_if_missing(src: Path, dst: Path, mode: int | None = None) -> None:
    if not src.is_file() or dst.exists():
        return
    dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(3)}.migtmp")
    try:
        shutil.copy2(src, tmp)
        if mode is not None:
            os.chmod(tmp, mode)
        try:
            os.link(tmp, dst)
        except FileExistsError:
            return
        except OSError:
            if not dst.exists():
                os.replace(tmp, dst)
    except (FileNotFoundError, FileExistsError):
        return
    except OSError:
        if dst.exists() or not src.is_file():
            return
        raise
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_parhelion_migrated() -> None:
    """Losslessly migrate legacy ~/.config/parhelion and ~/.config/opsb state into ~/.parhelion/."""
    ph = _resolve_parhelion_home()
    with _MIGRATE_LOCK:
        if ph in _MIGRATED_HOMES:
            return
        logs_dir, sessions_dir = ph / "logs", ph / "sessions"
        sandboxed = False
        for sub in (ph, ph / "cache", logs_dir, sessions_dir):
            try:
                sub.mkdir(mode=0o700, parents=True, exist_ok=True)
            except FileExistsError:
                # The path exists but this process may not stat it: the worker sandbox (4.2 K)
                # denies the credential stores to every agent and to its MCP server.
                sandboxed = True
        if sandboxed:
            # A sandboxed process cannot see the credential stores, so it must not migrate them
            # either; the platform's own unsandboxed processes do that.
            _MIGRATED_HOMES.add(ph)
            return

        home = Path.home()
        if ph == (home / ".parhelion").resolve():
            sources = (home / ".config" / "parhelion", home / ".config" / "opsb")
            static_files = (
                ("config.json", ph / "config.json", 0o600),
                ("cj_accounts.json", ph / "cj_accounts.json", 0o600),
                ("daemon.json", ph / "daemon.json", 0o600),
                ("daemon.log", logs_dir / "daemon.log", None),
                ("last_jump_submit.log", logs_dir / "last_jump_submit.log", None),
            )
            for src_dir in sources:
                if not src_dir.is_dir():
                    continue
                for rel_name, dst_path, mode in static_files:
                    _copy_if_missing(src_dir / rel_name, dst_path, mode=mode)
                for p in src_dir.iterdir():
                    if p.is_file() and p.name.startswith("inflight") and p.suffix == ".json":
                        _copy_if_missing(p, ph / p.name, mode=0o600)
                if (src_dir / "sessions").is_dir():
                    for sp in (src_dir / "sessions").glob("*.json"):
                        _copy_if_missing(sp, sessions_dir / sp.name, mode=0o600)

        _MIGRATED_HOMES.add(ph)


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
    p = _resolve_parhelion_home() / "cache" / f".cache_{name}.json"
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
        cache_dir = _resolve_parhelion_home() / "cache"
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        p = cache_dir / f".cache_{name}.json"
        tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(
            json.dumps({"ts": time.time(), "fp": fingerprint, "data": data}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, p)
    except Exception:
        pass


def invalidate_ttl_cache(name: str) -> None:
    try:
        (_resolve_parhelion_home() / "cache" / f".cache_{name}.json").unlink(missing_ok=True)
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
        raw = read_json(_resolve_parhelion_home() / "config.json")
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
        ph = _resolve_parhelion_home()
        ph.mkdir(mode=0o700, parents=True, exist_ok=True)
        cfg_path = ph / "config.json"
        tmp = cfg_path.with_name(f".{cfg_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            # config.json carries SSH hosts and workspace paths; the migration table sets it
            # to 0600, so every later save must keep that instead of falling back to umask.
            os.chmod(tmp, 0o600)
            os.replace(tmp, cfg_path)
        finally:
            tmp.unlink(missing_ok=True)

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


def ensure_scheduler_path() -> str:
    """Put the repository root on ``sys.path`` so ``scheduler.*`` imports work from ``opsb``.

    ``cli/opsb`` only adds ``cli/``, so a daemon or MCP process started through it cannot import the
    sibling ``scheduler`` package until the root is added; ``agents.judge_feedback`` already did this
    by hand. Returns the root for callers that want to log it.
    """
    root = str(SUPERVISOR_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return root
