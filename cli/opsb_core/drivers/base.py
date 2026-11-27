"""Base abstractions, data models, and core config/binary helpers for Parhelion Harness Drivers."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .. import config as _cfg
from .. import worker_sandbox
from .macos_gui import (
    PARHELION_SKILLS_SUBDIR,
    build_parhelion_mcp_command,
    compact_prompt_for_url,
    ensure_workspace_skills,
    inspect_macos_app_bundle,
    is_flock_held,
    open_gui_or_terminal,
    open_sqlite_readonly_wal,
    stage_brief_and_clipboard,
    write_mcp_json_file,
)


def resolve_parhelion_home() -> Path:
    """Resolve active PARHELION_HOME dynamically (honors env overrides in tests)."""
    override = os.environ.get("PARHELION_HOME") or os.environ.get("OPSB_HOME")
    return Path(override).expanduser().resolve() if override else _cfg.PARHELION_HOME


def resolve_workspace_data_dir(workspace: Path | str) -> Path:
    return resolve_parhelion_home() / "workspaces" / _cfg.workspace_id(workspace)


def resolve_workspace_state_dir(workspace: Path | str) -> Path:
    return resolve_workspace_data_dir(workspace) / "state"


def resolve_workspace_driver_dir(workspace: Path | str, kind: str) -> Path:
    return resolve_workspace_data_dir(workspace) / "drivers" / kind


def resolve_session_logs_dir() -> Path:
    return resolve_parhelion_home() / "logs" / "sessions"


def load_parhelion_config() -> dict[str, Any]:
    """Load config.json dynamically respecting runtime PARHELION_HOME overrides."""
    ph = resolve_parhelion_home()
    if ph == _cfg.PARHELION_HOME:
        return _cfg.ConfigRepository.load()
    cfg = json.loads(json.dumps(_cfg.DEFAULT_CONFIG))
    raw = _cfg.read_json(ph / "config.json")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k == "scheduler" and isinstance(v, dict):
                cfg["scheduler"].update(v)
            else:
                cfg[k] = v
    ws_list = [str(w) for w in (cfg.get("workspaces") or []) if str(w).strip()]
    active = str(cfg.get("active_workspace") or "").strip()
    if active and active not in ws_list:
        ws_list.insert(0, active)
    cfg["active_workspace"], cfg["workspaces"] = active, ws_list
    return cfg


def save_parhelion_config(cfg: dict[str, Any]) -> None:
    """Persist config.json atomically respecting runtime PARHELION_HOME overrides."""
    ph = resolve_parhelion_home()
    if ph == _cfg.PARHELION_HOME:
        _cfg.ConfigRepository.save(cfg)
        return
    ph.mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg_path = ph / "config.json"
    tmp = cfg_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, cfg_path)


def which_binary(candidates: tuple[str, ...] | list[str]) -> str:
    """Locate first installed executable on PATH or common user/system bin dirs."""
    for name in candidates:
        if found := shutil.which(name):
            return found
    for d in (Path.home() / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
        if not d.is_dir():
            continue
        for name in candidates:
            cand = d / name
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
    return ""


def probe_binary_version(binary_path: str, args: tuple[str, ...] = ("--version",), timeout: float = 5.0) -> str:
    """Run lightweight `--version` probe without network or stdin blocking."""
    if not binary_path:
        return ""
    try:
        proc = subprocess.run([binary_path, *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or proc.stderr or "").strip()
        return out.splitlines()[0].strip()[:120] if out else ""
    except Exception:
        return ""


@dataclass
class DriverDiagnostic:
    kind: str
    display_name: str
    installed: bool
    binary_path: str
    version: str
    auth_ok: bool
    protocol: str
    detail: str
    gui_installed: bool = False
    gui_app_path: str = ""
    gui_bundle_id: str = ""
    gui_version: str = ""
    url_scheme: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SessionRecord:
    session_id: str
    driver: str
    workspace: str
    problem: str
    worktree: str
    native_session_id: str
    status: str
    pid: int | None
    created_at: str
    updated_at: str
    brief_preview: str
    launch_mode: str = "headless"
    deep_link: str = ""
    project_id: str = ""
    brief_path: str = ""
    observed_steps: int = 0
    tokens_used: int = 0
    #: 4.4.8: why the session ended (first 300 chars), ``provider`` when it was a provider fault
    #: (``provider-resumed`` once the platform relaunched it), and whether ``--resume`` reloaded the
    #: old conversation (``loaded``) or fell back to a fresh session with a handoff (``fresh``).
    error: str = ""
    fault: str = ""
    resume: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionRecord:
        return cls(
            session_id=str(data.get("session_id") or ""), driver=str(data.get("driver") or ""),
            workspace=str(data.get("workspace") or ""), problem=str(data.get("problem") or ""),
            worktree=str(data.get("worktree") or ""), native_session_id=str(data.get("native_session_id") or ""),
            status=str(data.get("status") or "idle"),
            pid=int(data["pid"]) if data.get("pid") is not None else None,
            created_at=str(data.get("created_at") or ""), updated_at=str(data.get("updated_at") or ""),
            brief_preview=str(data.get("brief_preview") or ""),
            launch_mode=str(data.get("launch_mode") or "headless"),
            deep_link=str(data.get("deep_link") or ""), project_id=str(data.get("project_id") or ""),
            brief_path=str(data.get("brief_path") or ""),
            observed_steps=int(data.get("observed_steps") or 0), tokens_used=int(data.get("tokens_used") or 0),
            error=str(data.get("error") or ""), fault=str(data.get("fault") or ""),
            resume=str(data.get("resume") or ""),
        )


#: The problem label of the contest-wide orchestrator session (``opsb driver launch ALL``).
ORCHESTRATOR_PROBLEM = "ALL-10"


class BaseDriver(ABC):
    """Single unified interface for all Parhelion Harness Drivers."""

    kind: str = ""
    display_name: str = ""
    protocol: str = ""
    gui_apps: tuple[str, ...] = ()
    #: Set for the contest orchestrator's background job: how many sprint seats are live. An ACP
    #: driver keeps the orchestrator's turn going while it is positive (``antigravity_acp.continue_turns``).
    live_seats: Callable[[], int] | None = None
    #: Set for the orchestrator's background job (4.4.8): the sprint handoff a fresh ACP session is
    #: prompted with when ``session/load`` could not reload the old conversation.
    resume_handoff: Callable[[], str] | None = None
    #: The model the orchestrator's ACP session binds (``worker_models`` of its driver); None keeps the
    #: harness default. Set by the background job, like ``live_seats``.
    session_model: str | None = None

    def __init__(self) -> None:
        self.last_launch_meta: dict[str, Any] = {}

    @abstractmethod
    def discover(self) -> DriverDiagnostic: ...

    def discover_gui(self) -> dict[str, Any]:
        return {"gui_installed": False, "gui_app_path": "", "gui_bundle_id": "", "gui_version": "", "url_scheme": ""}

    def build_gui_deep_link(self, worktree: Path, prompt: str = "", project_id: str | None = None, thread_id: str | None = None) -> str:
        return ""

    def observe_gui_sessions(self, workspace: Path | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return []

    @abstractmethod
    def sync_workspace(self, workspace: Path, mcp_url: str | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def build_launch_command(
        self, worktree: Path, prompt: str, session_id: str, resume_id: str | None = None, mcp_config_path: Path | None = None,
    ) -> tuple[list[str], dict[str, str]]: ...

    def _default_mcp_config_path(self, workspace: Path) -> Path:
        return resolve_workspace_driver_dir(workspace, self.kind) / "mcp_config.json"

    def _new_session_id(self) -> str:
        return f"{self.kind}-{_cfg.utc_now().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"

    def _new_native_id(self, session_id: str, resume_id: str | None) -> str:
        return resume_id or session_id

    def _execute_headless_turn(
        self, workspace: Path, worktree: Path, brief: str, session_id: str, native_session_id: str,
        resume_id: str | None, cmd: list[str], env: dict[str, str], log_path: Path,
    ) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as lf:
            lf.write(json.dumps({"event": "launch", "driver": self.kind, "cmd": cmd, "ts": _cfg.utc_now().isoformat()}) + "\n")
            lf.flush()
            proc = subprocess.Popen(cmd, cwd=str(worktree), env=worker_sandbox.worker_env(os.environ, env),
                                    stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT)
            rc = proc.wait()
            lf.write(json.dumps({"event": "exit", "driver": self.kind, "returncode": rc, "ts": _cfg.utc_now().isoformat()}) + "\n")
        return int(rc)

    def launch_session(
        self, workspace: Path, problem: str, worktree: Path, brief: str, mode: str = "background",
        resume_id: str | None = None, dry_run: bool = False, preseed_thread: bool = False,
    ) -> SessionRecord:
        from .session_store import save_session

        ws, wt = workspace.expanduser().resolve(), worktree.expanduser().resolve()
        norm_mode = "dry-run" if mode in ("dry-run", "dry_run") else mode
        if norm_mode not in ("dry-run", "headless", "background", "gui"):
            raise ValueError(f"Unsupported launch mode '{mode}'. Use headless, gui, background, or dry-run.")
        is_dry = dry_run or norm_mode == "dry-run"

        sync_res = self.sync_workspace(ws)
        mcp_cfg_path = Path(sync_res["mcp_config_path"]) if sync_res.get("mcp_config_path") else self._default_mcp_config_path(ws)
        session_id = self._new_session_id()
        native_id = self._new_native_id(session_id, resume_id)
        cmd, env_overrides = self.build_launch_command(wt, brief, session_id, resume_id=resume_id, mcp_config_path=mcp_cfg_path)
        now_iso = _cfg.utc_now().isoformat()
        log_dir = resolve_session_logs_dir()
        log_path = log_dir / f"{session_id}.jsonl"
        brief_path, copied = stage_brief_and_clipboard(wt, brief, copy_clipboard=(not is_dry and norm_mode == "gui"))

        pid: int | None = None
        status, launch_mode = "idle", ("dry-run" if norm_mode == "dry-run" else norm_mode)
        if not is_dry:
            diag = self.discover()
            if norm_mode == "gui":
                pid = open_gui_or_terminal(diag.binary_path, self.gui_apps, wt, brief, log_dir, session_id)
                if pid is None and not diag.installed:
                    raise RuntimeError(f"Driver '{self.kind}' binary/app is not installed ({diag.detail})")
            else:
                if not diag.installed or not diag.binary_path:
                    raise RuntimeError(f"Driver '{self.kind}' binary not installed ({diag.detail})")
                log_dir.mkdir(parents=True, exist_ok=True)
                job_dir = resolve_workspace_driver_dir(ws, self.kind) / "jobs"
                job_dir.mkdir(parents=True, exist_ok=True)
                job_path = job_dir / f"{session_id}.json"
                job_path.write_text(json.dumps({
                    "driver": self.kind, "workspace": str(ws), "problem": problem, "worktree": str(wt),
                    "brief": brief, "session_id": session_id, "native_session_id": native_id,
                    "resume_id": resume_id, "cmd": cmd, "env": env_overrides, "log_path": str(log_path),
                }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                job_env = {**os.environ, "PARHELION_HOME": str(resolve_parhelion_home())}
                existing_pp = job_env.get("PYTHONPATH", "")
                job_env["PYTHONPATH"] = f"{_cfg.CLI_DIR}:{existing_pp}" if existing_pp else str(_cfg.CLI_DIR)
                runner = [_cfg.resolve_python_exe(ws), "-m", "opsb_core.drivers.registry", "--exec-job", str(job_path)]
                if problem != ORCHESTRATOR_PROBLEM:
                    # 4.2 K: the job runner needs the CLI on PYTHONPATH to start, so it is sandboxed
                    # instead: the driver child inherits the credential read denies, and
                    # ``_execute_headless_turn`` drops the platform paths from its environment again.
                    # The contest orchestrator is not a managed worker: its MCP server reads the
                    # daemon state to start seats, which the sandbox denies (4.3.0 fix).
                    runner = worker_sandbox.wrap_command(runner)
                with log_path.open("a", encoding="utf-8") as err_fh:
                    proc = subprocess.Popen(
                        runner, cwd=str(wt), env=job_env, stdin=subprocess.DEVNULL, stdout=err_fh,
                        stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
                    )
                pid = proc.pid
            status = "running"

        record = SessionRecord(
            session_id=session_id, driver=self.kind, workspace=str(ws), problem=problem, worktree=str(wt),
            native_session_id=native_id, status=status, pid=pid, created_at=now_iso, updated_at=now_iso,
            brief_preview=brief.strip().replace("\n", " ")[:180], launch_mode=launch_mode, brief_path=str(brief_path),
        )
        save_session(ws, record)
        self.last_launch_meta = {
            "mode": norm_mode, "launch_mode": launch_mode, "dry_run": is_dry, "command": cmd, "env": env_overrides,
            "mcp_config_path": str(mcp_cfg_path), "log_path": str(log_path), "worktree": str(wt),
            "brief_path": str(brief_path), "clipboard_copied": copied, "deep_link": "", "project_id": "", "sync": sync_res,
        }
        return record

    def _finalize_gui_launch(
        self, ws: Path, wt: Path, problem: str, brief: str, *, session_id: str, native_id: str,
        status: str, launch_mode: str, url: str, project_id: str, brief_path: Path,
        copied: bool, dry_run: bool, mcp_config_path: str | None, sync_res: dict[str, Any],
    ) -> SessionRecord:
        from .session_store import save_session

        now_iso = _cfg.utc_now().isoformat()
        record = SessionRecord(
            session_id=session_id, driver=self.kind, workspace=str(ws), problem=problem, worktree=str(wt),
            native_session_id=native_id, status=status, pid=None, created_at=now_iso, updated_at=now_iso,
            brief_preview=brief.strip().replace("\n", " ")[:180], launch_mode=launch_mode,
            deep_link=url, project_id=project_id, brief_path=str(brief_path),
        )
        save_session(ws, record)
        self.last_launch_meta = {
            "mode": "gui", "launch_mode": launch_mode, "dry_run": dry_run, "command": ["open", url], "env": {},
            "mcp_config_path": mcp_config_path, "log_path": str(resolve_session_logs_dir() / f"{session_id}.jsonl"),
            "worktree": str(wt), "deep_link": url, "project_id": project_id, "brief_path": str(brief_path),
            "clipboard_copied": copied, "sync": sync_res,
        }
        return record
