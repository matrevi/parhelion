"""Configuration repository, workspace discovery, and atomic JSON/JSONL I/O."""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any

CLI_DIR = Path(__file__).resolve().parents[1]
SUPERVISOR_ROOT = CLI_DIR.parent
DEFAULT_BENCH_ROOT = SUPERVISOR_ROOT.parent
CONFIG_DIR = Path.home() / ".config" / "opsb"
CONFIG_PATH = CONFIG_DIR / "config.json"

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
    "active_workspace": str(DEFAULT_BENCH_ROOT),
    "workspaces": [str(DEFAULT_BENCH_ROOT)],
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
    p = CONFIG_DIR / f".cache_{name}.json"
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
        CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        p = CONFIG_DIR / f".cache_{name}.json"
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
        (CONFIG_DIR / f".cache_{name}.json").unlink(missing_ok=True)
    except Exception:
        pass


def resolve_python_exe(workspace: Path | None = None) -> str:
    candidates = []
    if workspace is not None:
        candidates.append(workspace / ".venv" / "bin" / "python")
    candidates.append(DEFAULT_BENCH_ROOT / ".venv" / "bin" / "python")
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    import sys
    return sys.executable


class ConfigRepository:
    @staticmethod
    def load() -> dict[str, Any]:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        raw = read_json(CONFIG_PATH)
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k == "scheduler" and isinstance(v, dict):
                    cfg["scheduler"].update(v)
                else:
                    cfg[k] = v
        ws_list = cfg.get("workspaces") or []
        if cfg["active_workspace"] and cfg["active_workspace"] not in ws_list:
            ws_list.insert(0, cfg["active_workspace"])
        cfg["workspaces"] = ws_list
        return cfg

    @staticmethod
    def save(cfg: dict[str, Any]) -> None:
        CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)

    @staticmethod
    def resolve_workspace(explicit: str | None, cfg: dict[str, Any]) -> Path:
        if explicit:
            return Path(explicit).expanduser().resolve()
        env_ws = os.environ.get("OPSB_WORKSPACE")
        if env_ws:
            return Path(env_ws).expanduser().resolve()
        cwd = Path.cwd().resolve()
        for anc in [cwd, *cwd.parents]:
            if (anc / "opsb.workspace.json").is_file() or (anc / "bench" / "problems.json").is_file():
                return anc
        return Path(cfg.get("active_workspace") or str(DEFAULT_BENCH_ROOT)).expanduser().resolve()
