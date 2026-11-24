"""macOS GUI bundle inspection, WAL-safe SQLite/flock helpers, clipboard staging, and MCP/skills sync."""
from __future__ import annotations

import errno
import fcntl
import importlib
import json
import os
import plistlib
import shlex
import shutil
import sqlite3
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any

from .. import config as _cfg

PARHELION_SKILLS_SUBDIR = "kernelc-deep-reference"


def _parhelion_home() -> Path:
    override = os.environ.get("PARHELION_HOME") or os.environ.get("OPSB_HOME")
    return Path(override).expanduser().resolve() if override else _cfg.PARHELION_HOME


def inspect_macos_app_bundle(
    candidates: list[Path],
    expected_bundle_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Read macOS `.app/Contents/Info.plist` via stdlib `plistlib` (zero subprocess overhead)."""
    empty = {"gui_installed": False, "gui_app_path": "", "gui_bundle_id": "", "gui_version": "", "url_scheme": ""}
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
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=timeout)
        conn.execute("PRAGMA query_only = ON;")
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1;").fetchone()
        return conn
    except sqlite3.OperationalError:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        conn = sqlite3.connect(f"file:{quoted}?mode=ro&immutable=1", uri=True, timeout=timeout)
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
            return exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK)
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


def infer_problem_from_worktree(worktree_str: str, real_ws: Path | None) -> str:
    """Derive operator problem name from a worktree/cwd path relative to a workspace."""
    problem_name = Path(worktree_str).name if worktree_str else ""
    if real_ws is not None and worktree_str:
        try:
            rel_parts = Path(os.path.realpath(worktree_str)).relative_to(real_ws).parts
            if len(rel_parts) >= 2 and rel_parts[0] == "bench":
                return rel_parts[1]
            if rel_parts and rel_parts[0] not in (".worktrees",):
                return rel_parts[0]
        except Exception:
            pass
    return problem_name


def launch_macos_deep_link(url: str, gui_app_path: str, fallback_args: list[str], label: str) -> None:
    """Open a macOS URL scheme deep link with graceful fallback to `open -a <app>`."""
    proc = subprocess.run(["open", url], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        if gui_app_path:
            subprocess.run(["open", "-a", gui_app_path, *fallback_args], check=True)
        else:
            raise RuntimeError(f"Failed to launch {label} GUI URL: {(proc.stderr or proc.stdout).strip()}")


def build_parhelion_mcp_command(
    workspace: Path,
    *,
    role: str | None = None,
    agent_id: str | None = None,
) -> tuple[str, list[str], dict[str, str]]:
    """Return `(command, args, env)` to run `opsb mcp serve` for a workspace."""
    ws = Path(os.path.realpath(workspace.expanduser()))
    py_exe = _cfg.resolve_python_exe(ws)
    opsb_script = str(_cfg.CLI_DIR / "opsb")
    args = [opsb_script, "mcp", "serve", "--workspace", str(ws)]
    env = {"PARHELION_HOME": str(_parhelion_home())}
    if role:
        args.extend(["--role", role])
        env["PARHELION_MCP_ROLE"] = role
    if agent_id:
        args.extend(["--agent-id", agent_id])
        env["PARHELION_AGENT_ID"] = agent_id
    return py_exe, args, env


def write_mcp_json_file(
    target_path: Path,
    workspace: Path,
    mcp_url: str | None = None,
    server_name: str = "parhelion",
    *,
    role: str | None = None,
    agent_id: str | None = None,
) -> Path:
    """Write standard `mcpServers` JSON config file atomically."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if mcp_url:
        entry: dict[str, Any] = {"type": "http", "url": mcp_url}
    else:
        cmd, args, env = build_parhelion_mcp_command(workspace, role=role, agent_id=agent_id)
        entry = {"command": cmd, "args": args, "env": env}
    tmp = target_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"mcpServers": {server_name: entry}}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, target_path)
    return target_path


def ensure_workspace_skills(workspace: Path) -> dict[str, Any]:
    """Ensure `<ws>/.agents/skills` exists and project read-only Triad KB skills if available."""
    ws = workspace.expanduser().resolve()
    skills_dir = ws / ".agents" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    projected: list[str] = []
    try:
        kb_mod = importlib.import_module("opsb_core.services.triad_kb_service")
        proj_fn = getattr(kb_mod, "project_readonly_skills", None)
        if callable(proj_fn) and isinstance(proj_fn(ws), dict):
            projected.append("triad-kb")
    except Exception:
        pass

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


def open_gui_or_terminal(
    binary_path: str,
    app_names: tuple[str, ...],
    worktree: Path,
    brief: str,
    log_dir: Path,
    session_id: str,
) -> int | None:
    """Open desktop harness or macOS Terminal at `worktree` and copy brief when possible."""
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{session_id}.brief.md").write_text(brief, encoding="utf-8")
    if shutil.which("pbcopy"):
        try:
            subprocess.run(["pbcopy"], input=brief, text=True, timeout=1.5, check=False)
        except Exception:
            pass
    if shutil.which("open"):
        for app in app_names:
            if (Path("/Applications") / f"{app}.app").exists():
                proc = subprocess.Popen(
                    ["open", "-a", app, str(worktree)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                )
                return proc.pid
    if binary_path and shutil.which("osascript"):
        cmd_line = f"cd {shlex.quote(str(worktree))} && {shlex.quote(binary_path)}"
        osa = f'tell application "Terminal" to do script {json.dumps(cmd_line)}'
        proc = subprocess.Popen(
            ["osascript", "-e", osa],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        return proc.pid
    return None
