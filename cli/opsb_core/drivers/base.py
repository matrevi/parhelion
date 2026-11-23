"""Base abstractions, data models, and workspace/GUI helpers for Parhelion Harness Drivers."""
from __future__ import annotations

import errno
import fcntl
import json
import os
import plistlib
import shlex
import shutil
import sqlite3
import subprocess
import urllib.parse
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


def inspect_macos_app_bundle(
    candidates: list[Path],
    expected_bundle_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Read macOS `.app/Contents/Info.plist` via stdlib `plistlib` (zero subprocess overhead)."""
    empty = {
        "gui_installed": False,
        "gui_app_path": "",
        "gui_bundle_id": "",
        "gui_version": "",
        "url_scheme": "",
    }
    for bundle in candidates:
        plist_path = bundle / "Contents" / "Info.plist"
        if not plist_path.is_file():
            continue
        try:
            with plist_path.open("rb") as fh:
                info = plistlib.load(fh)
        except Exception:
            continue
        if not isinstance(info, dict):
            continue
        bundle_id = str(info.get("CFBundleIdentifier") or "").strip()
        if expected_bundle_ids and bundle_id not in expected_bundle_ids:
            continue
        version = str(info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or "").strip()
        url_scheme = ""
        url_types = info.get("CFBundleURLTypes") or []
        if isinstance(url_types, list):
            for ut in url_types:
                if not isinstance(ut, dict):
                    continue
                schemes = ut.get("CFBundleURLSchemes") or []
                if isinstance(schemes, list) and schemes:
                    cand_s = str(schemes[0] or "").strip()
                    if cand_s and cand_s not in ("http", "https"):
                        url_scheme = cand_s
                        break
                    if not url_scheme and cand_s:
                        url_scheme = cand_s
        return {
            "gui_installed": True,
            "gui_app_path": str(bundle),
            "gui_bundle_id": bundle_id,
            "gui_version": version,
            "url_scheme": url_scheme,
        }
    return empty


def open_sqlite_readonly_wal(db_path: Path, timeout: float = 1.0) -> sqlite3.Connection:
    """Open a WAL-mode SQLite DB read-only (`mode=ro` first so uncheckpointed `-wal` is visible)."""
    resolved = Path(os.path.realpath(db_path))
    quoted = urllib.parse.quote(str(resolved), safe="/:")
    uri_ro = f"file:{quoted}?mode=ro"
    try:
        conn = sqlite3.connect(uri_ro, uri=True, timeout=timeout)
        conn.execute("PRAGMA query_only = ON;")
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1;").fetchone()
        return conn
    except sqlite3.OperationalError:
        try:
            conn.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        uri_imm = f"file:{quoted}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri_imm, uri=True, timeout=timeout)
        conn.execute("PRAGMA query_only = ON;")
        return conn


def is_flock_held(lock_path: Path) -> bool:
    """Non-blocking `fcntl.flock` probe returning True if an exclusive lock is held elsewhere."""
    if not lock_path.exists():
        return False
    fd: int | None = None
    try:
        fd = os.open(str(lock_path), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                return True
            return False
    except OSError:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def stage_brief_and_clipboard(
    worktree: Path,
    brief: str,
    copy_clipboard: bool = True,
) -> tuple[Path, bool]:
    """Atomically write `<worktree>/.parhelion/brief.md` and optionally copy via `pbcopy`."""
    wt = Path(os.path.realpath(worktree))
    brief_dir = wt / ".parhelion"
    brief_dir.mkdir(parents=True, exist_ok=True)
    brief_path = brief_dir / "brief.md"
    tmp = brief_path.with_suffix(".tmp")
    tmp.write_text(brief, encoding="utf-8")
    os.replace(tmp, brief_path)

    copied = False
    if copy_clipboard and shutil.which("pbcopy"):
        try:
            proc = subprocess.run(["pbcopy"], input=brief, text=True, timeout=1.5, check=False)
            copied = proc.returncode == 0
        except Exception:
            copied = False
    return brief_path, copied


def compact_prompt_for_url(
    brief: str,
    brief_path: Path,
    base_url_len: int,
    max_url_len: int = 6000,
) -> str:
    """Return URL-safe prompt within `max_url_len`, falling back to `.parhelion/brief.md` pointer."""
    if not brief:
        return ""
    encoded = urllib.parse.quote(brief, safe="")
    if base_url_len + len("&prompt=") + len(encoded) <= max_url_len:
        return brief
    return f"Read {brief_path} for the full Parhelion brief and execute."


def build_parhelion_mcp_command(workspace: Path) -> tuple[str, list[str], dict[str, str]]:
    """Return `(command, args, env)` to run `opsb mcp serve` for a workspace."""
    ws = Path(os.path.realpath(workspace.expanduser()))
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

    if binary_path and shutil.which("osascript"):
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
    native_session_id: str     # e.g. Codex threadId, Claude UUID, Antigravity sessionId, Pi sessionId
    status: str                # "running" | "idle" | "completed" | "failed"
    pid: int | None
    created_at: str
    updated_at: str
    brief_preview: str
    launch_mode: str = "headless"   # "headless" | "gui" | "gui-preseed" | "dry-run"
    deep_link: str = ""
    project_id: str = ""
    brief_path: str = ""
    observed_steps: int = 0
    tokens_used: int = 0

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
            launch_mode=str(data.get("launch_mode") or "headless"),
            deep_link=str(data.get("deep_link") or ""),
            project_id=str(data.get("project_id") or ""),
            brief_path=str(data.get("brief_path") or ""),
            observed_steps=int(data.get("observed_steps") or 0),
            tokens_used=int(data.get("tokens_used") or 0),
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

    def discover_gui(self) -> dict[str, Any]:
        """Inspect installed macOS GUI bundle via Info.plist (read-only)."""
        return {
            "gui_installed": False,
            "gui_app_path": "",
            "gui_bundle_id": "",
            "gui_version": "",
            "url_scheme": "",
        }

    def build_gui_deep_link(
        self,
        worktree: Path,
        prompt: str = "",
        project_id: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        """Build native URL scheme deep link (`codex://` or `antigravity://`)."""
        return ""

    def observe_gui_sessions(
        self,
        workspace: Path | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Read-only observation of live & recent GUI sessions from native local stores."""
        return []

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
        dry_run: bool = False,
        preseed_thread: bool = False,
    ) -> SessionRecord:
        from .session_store import save_session

        ws = workspace.expanduser().resolve()
        wt = worktree.expanduser().resolve()
        norm_mode = "dry-run" if mode in ("dry-run", "dry_run") else mode
        if norm_mode not in ("dry-run", "headless", "background", "gui"):
            raise ValueError(f"Unsupported launch mode '{mode}'. Use headless, gui, background, or dry-run.")
        is_dry = dry_run or norm_mode == "dry-run"

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
        brief_path, copied = stage_brief_and_clipboard(wt, brief, copy_clipboard=(not is_dry and norm_mode == "gui"))

        pid: int | None = None
        status = "idle"
        launch_mode = "dry-run" if norm_mode == "dry-run" else norm_mode

        if is_dry:
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
            launch_mode=launch_mode,
            brief_path=str(brief_path),
        )
        save_session(ws, record)
        self.last_launch_meta = {
            "mode": norm_mode,
            "launch_mode": launch_mode,
            "dry_run": is_dry,
            "command": cmd,
            "env": env_overrides,
            "mcp_config_path": str(mcp_cfg_path),
            "log_path": str(log_path),
            "worktree": str(wt),
            "brief_path": str(brief_path),
            "clipboard_copied": copied,
            "deep_link": "",
            "project_id": "",
            "sync": sync_res,
        }
        return record
