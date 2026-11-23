"""Base abstractions, data models, and workspace projection helpers for Parhelion Harness Drivers."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .. import config as _cfg

PARHELION_SKILLS_SUBDIR = "kernelc-deep-reference"


def resolve_parhelion_home() -> Path:
    """Resolve active PARHELION_HOME dynamically (honors env overrides in tests)."""
    override = os.environ.get("PARHELION_HOME") or os.environ.get("OPSB_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return _cfg.PARHELION_HOME


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
    cfg["active_workspace"] = active
    cfg["workspaces"] = ws_list
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
        found = shutil.which(name)
        if found:
            return found
    home = Path.home()
    extra_dirs = (
        home / ".local" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    )
    for d in extra_dirs:
        if not d.is_dir():
            continue
        for name in candidates:
            cand = d / name
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
    return ""


def probe_binary_version(binary_path: str, args: tuple[str, ...] = ("--version",), timeout: float = 2.0) -> str:
    """Run lightweight `--version` probe without network or stdin blocking."""
    if not binary_path:
        return ""
    try:
        proc = subprocess.run(
            [binary_path, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (proc.stdout or proc.stderr or "").strip()
        return out.splitlines()[0].strip()[:120] if out else ""
    except Exception:
        return ""


def build_parhelion_mcp_command(workspace: Path) -> tuple[str, list[str], dict[str, str]]:
    """Return `(command, args, env)` to run `opsb mcp serve` for a workspace."""
    ws = workspace.expanduser().resolve()
    py_exe = _cfg.resolve_python_exe(ws)
    opsb_script = str(_cfg.CLI_DIR / "opsb")
    env = {"PARHELION_HOME": str(resolve_parhelion_home())}
    return py_exe, [opsb_script, "mcp", "serve", "--workspace", str(ws)], env


def write_mcp_json_file(
    target_path: Path,
    workspace: Path,
    mcp_url: str | None = None,
    server_name: str = "parhelion",
) -> Path:
    """Write standard `mcpServers` JSON config file atomically."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if mcp_url:
        entry: dict[str, Any] = {"type": "http", "url": mcp_url}
    else:
        cmd, args, env = build_parhelion_mcp_command(workspace)
        entry = {"command": cmd, "args": args, "env": env}
    payload = {"mcpServers": {server_name: entry}}
    tmp = target_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, target_path)
    return target_path


def ensure_workspace_skills(workspace: Path) -> dict[str, Any]:
    """Ensure `<ws>/.agents/skills` exists and project read-only Triad KB skills if available."""
    ws = workspace.expanduser().resolve()
    skills_dir = ws / ".agents" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    projected: list[str] = []
    # Lazily integrate Track 2 Triad KB read-only skill projection when available
    try:
        from ..services import triad_kb_service  # type: ignore

        proj_fn = getattr(triad_kb_service, "project_readonly_skills", None)
        if callable(proj_fn):
            res = proj_fn(ws)
            if isinstance(res, dict):
                projected.append("triad-kb")
    except Exception:
        pass

    # Project bundled KernelC deep reference skill read-only (`0400`) if present
    src_skill = _cfg.SUPERVISOR_ROOT / "skills" / PARHELION_SKILLS_SUBDIR / "SKILL.md"
    if src_skill.is_file():
        dst_dir = skills_dir / PARHELION_SKILLS_SUBDIR
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst_file = dst_dir / "SKILL.md"
        content = src_skill.read_text(encoding="utf-8")
        if dst_file.exists():
            try:
                if dst_file.read_text(encoding="utf-8") != content:
                    os.chmod(dst_file, 0o600)
                    dst_file.write_text(content, encoding="utf-8")
                    os.chmod(dst_file, 0o400)
            except OSError:
                pass
        else:
            dst_file.write_text(content, encoding="utf-8")
            os.chmod(dst_file, 0o400)
        projected.append(PARHELION_SKILLS_SUBDIR)

    return {"skills_dir": str(skills_dir), "projected": projected}


def open_gui_or_terminal(binary_path: str, app_names: tuple[str, ...], worktree: Path, brief: str, log_dir: Path, session_id: str) -> int | None:
    """Open desktop harness or macOS Terminal at `worktree` and copy brief when possible."""
    log_dir.mkdir(parents=True, exist_ok=True)
    brief_path = log_dir / f"{session_id}.brief.md"
    brief_path.write_text(brief, encoding="utf-8")

    # Best-effort clipboard handoff on macOS without failing headless/Linux tests
    if shutil.which("pbcopy"):
        try:
            subprocess.run(["pbcopy"], input=brief, text=True, timeout=1.5, check=False)
        except Exception:
            pass

    if shutil.which("open"):
        for app in app_names:
            app_bundle = Path("/Applications") / f"{app}.app"
            if app_bundle.exists():
                proc = subprocess.Popen(
                    ["open", "-a", app, str(worktree)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return proc.pid

    if binary_path:
        if shutil.which("osascript"):
            cmd_line = f"cd {shlex.quote(str(worktree))} && {shlex.quote(binary_path)}"
            osa = f'tell application "Terminal" to do script {json.dumps(cmd_line)}'
            proc = subprocess.Popen(
                ["osascript", "-e", osa],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return proc.pid
    return None


@dataclass
class DriverDiagnostic:
    kind: str                  # "antigravity" | "codex" | "claude" | "pi" | "gemini" | "deepseek"
    display_name: str
    installed: bool
    binary_path: str
    version: str
    auth_ok: bool
    protocol: str              # "acp-stdio" | "app-server-rpc" | "stream-json" | "pi-rpc" | "cli-exec"
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SessionRecord:
    session_id: str
    driver: str
    workspace: str
    problem: str
    worktree: str
    native_session_id: str     # e.g. Codex threadId, Claude UUID, Antigravity sessionId, Pi sessionId
    status: str                # "running" | "idle" | "completed" | "failed"
    pid: int | None
    created_at: str
    updated_at: str
    brief_preview: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionRecord:
        return cls(
            session_id=str(data.get("session_id") or ""),
            driver=str(data.get("driver") or ""),
            workspace=str(data.get("workspace") or ""),
            problem=str(data.get("problem") or ""),
            worktree=str(data.get("worktree") or ""),
            native_session_id=str(data.get("native_session_id") or ""),
            status=str(data.get("status") or "idle"),
            pid=int(data["pid"]) if data.get("pid") is not None else None,
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            brief_preview=str(data.get("brief_preview") or ""),
        )


class BaseDriver(ABC):
    """Single unified interface for all Parhelion Harness Drivers."""

    kind: str = ""
    display_name: str = ""
    protocol: str = ""
    gui_apps: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.last_launch_meta: dict[str, Any] = {}

    @abstractmethod
    def discover(self) -> DriverDiagnostic: ...

    @abstractmethod
    def sync_workspace(self, workspace: Path, mcp_url: str | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def build_launch_command(
        self,
        worktree: Path,
        prompt: str,
        session_id: str,
        resume_id: str | None = None,
        mcp_config_path: Path | None = None,
    ) -> tuple[list[str], dict[str, str]]: ...

    def _default_mcp_config_path(self, workspace: Path) -> Path:
        return resolve_workspace_driver_dir(workspace, self.kind) / "mcp_config.json"

    def _new_session_id(self) -> str:
        stamp = _cfg.utc_now().strftime("%Y%m%dT%H%M%SZ")
        return f"{self.kind}-{stamp}-{uuid.uuid4().hex[:6]}"

    def _new_native_id(self, session_id: str, resume_id: str | None) -> str:
        return resume_id or session_id

    def _execute_headless_turn(
        self,
        workspace: Path,
        worktree: Path,
        brief: str,
        session_id: str,
        native_session_id: str,
        resume_id: str | None,
        cmd: list[str],
        env: dict[str, str],
        log_path: Path,
    ) -> int:
        """Execute a driver turn inside the detached background worker. Subclasses may override."""
        log_path.parent.mkdir(parents=True, exist_ok=True)
        merged_env = {**os.environ, **env}
        with log_path.open("a", encoding="utf-8") as lf:
            lf.write(json.dumps({"event": "launch", "driver": self.kind, "cmd": cmd, "ts": _cfg.utc_now().isoformat()}) + "\n")
            lf.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=str(worktree),
                env=merged_env,
                stdin=subprocess.DEVNULL,
                stdout=lf,
                stderr=subprocess.STDOUT,
            )
            rc = proc.wait()
            lf.write(json.dumps({"event": "exit", "driver": self.kind, "returncode": rc, "ts": _cfg.utc_now().isoformat()}) + "\n")
        return int(rc)

    def launch_session(
        self,
        workspace: Path,
        problem: str,
        worktree: Path,
        brief: str,
        mode: str = "background",
        resume_id: str | None = None,
    ) -> SessionRecord:
        from .session_store import save_session

        ws = workspace.expanduser().resolve()
        wt = worktree.expanduser().resolve()
        norm_mode = "dry-run" if mode in ("dry-run", "dry_run") else mode
        if norm_mode not in ("dry-run", "headless", "background", "gui"):
            raise ValueError(f"Unsupported launch mode '{mode}'. Use headless, gui, background, or dry-run.")

        sync_res = self.sync_workspace(ws)
        mcp_cfg_path_str = sync_res.get("mcp_config_path")
        mcp_cfg_path = Path(mcp_cfg_path_str) if mcp_cfg_path_str else self._default_mcp_config_path(ws)

        session_id = self._new_session_id()
        native_id = self._new_native_id(session_id, resume_id)
        cmd, env_overrides = self.build_launch_command(
            worktree=wt,
            prompt=brief,
            session_id=session_id,
            resume_id=resume_id,
            mcp_config_path=mcp_cfg_path,
        )
        now_iso = _cfg.utc_now().isoformat()
        log_dir = resolve_session_logs_dir()
        log_path = log_dir / f"{session_id}.jsonl"

        pid: int | None = None
        status = "idle"

        if norm_mode == "dry-run":
            status = "idle"
        elif norm_mode == "gui":
            diag = self.discover()
            pid = open_gui_or_terminal(
                binary_path=diag.binary_path,
                app_names=self.gui_apps,
                worktree=wt,
                brief=brief,
                log_dir=log_dir,
                session_id=session_id,
            )
            if pid is None and not diag.installed:
                raise RuntimeError(f"Driver '{self.kind}' binary/app is not installed ({diag.detail})")
            status = "running"
        else:
            diag = self.discover()
            if not diag.installed or not diag.binary_path:
                raise RuntimeError(f"Driver '{self.kind}' binary not installed ({diag.detail})")
            log_dir.mkdir(parents=True, exist_ok=True)
            job_dir = resolve_workspace_driver_dir(ws, self.kind) / "jobs"
            job_dir.mkdir(parents=True, exist_ok=True)
            job_path = job_dir / f"{session_id}.json"
            job_payload = {
                "driver": self.kind,
                "workspace": str(ws),
                "problem": problem,
                "worktree": str(wt),
                "brief": brief,
                "session_id": session_id,
                "native_session_id": native_id,
                "resume_id": resume_id,
                "cmd": cmd,
                "env": env_overrides,
                "log_path": str(log_path),
            }
            job_path.write_text(json.dumps(job_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            py_exe = _cfg.resolve_python_exe(ws)
            worker_env = {**os.environ, "PARHELION_HOME": str(resolve_parhelion_home())}
            existing_pp = worker_env.get("PYTHONPATH", "")
            worker_env["PYTHONPATH"] = f"{_cfg.CLI_DIR}:{existing_pp}" if existing_pp else str(_cfg.CLI_DIR)
            with log_path.open("a", encoding="utf-8") as err_fh:
                proc = subprocess.Popen(
                    [py_exe, "-m", "opsb_core.drivers.registry", "--exec-job", str(job_path)],
                    cwd=str(wt),
                    env=worker_env,
                    stdin=subprocess.DEVNULL,
                    stdout=err_fh,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
            pid = proc.pid
            status = "running"

        record = SessionRecord(
            session_id=session_id,
            driver=self.kind,
            workspace=str(ws),
            problem=problem,
            worktree=str(wt),
            native_session_id=native_id,
            status=status,
            pid=pid,
            created_at=now_iso,
            updated_at=now_iso,
            brief_preview=brief.strip().replace("\n", " ")[:180],
        )
        save_session(ws, record)
        self.last_launch_meta = {
            "mode": norm_mode,
            "command": cmd,
            "env": env_overrides,
            "mcp_config_path": str(mcp_cfg_path),
            "log_path": str(log_path),
            "worktree": str(wt),
            "sync": sync_res,
        }
        return record
