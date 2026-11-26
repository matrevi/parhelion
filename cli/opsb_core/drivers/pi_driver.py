"""Pi Harness Driver (`pi --mode rpc` native JSON-line RPC adapter)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .. import worker_sandbox
from .base import (
    BaseDriver,
    DriverDiagnostic,
    ensure_workspace_skills,
    probe_binary_version,
    resolve_parhelion_home,
    resolve_workspace_driver_dir,
    which_binary,
    write_mcp_json_file,
)


class PiRpcClient:
    """JSON-line encoder/decoder for `pi --mode rpc`."""

    @staticmethod
    def encode_prompt(prompt: str) -> str:
        return json.dumps({"type": "prompt", "message": prompt}, ensure_ascii=False, separators=(",", ":")) + "\n"

    @staticmethod
    def encode_get_state() -> str:
        return json.dumps({"type": "get_state"}, separators=(",", ":")) + "\n"

    @staticmethod
    def parse_events(raw_lines: list[str] | str) -> list[dict[str, Any]]:
        lines = raw_lines.splitlines() if isinstance(raw_lines, str) else raw_lines
        out: list[dict[str, Any]] = []
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out


class PiDriver(BaseDriver):
    """Unified Pi driver (`pi --mode rpc`)."""

    kind = "pi"
    display_name = "Pi"
    protocol = "pi-rpc"
    gui_apps = ()

    def _find_binary(self) -> str:
        return which_binary(("pi",))

    def _session_dir(self, workspace: Path) -> Path:
        return resolve_workspace_driver_dir(workspace, self.kind) / "sessions"

    def discover(self) -> DriverDiagnostic:
        bin_path = self._find_binary()
        installed = bool(bin_path)
        version = probe_binary_version(bin_path) if bin_path else ""
        pi_dir = Path.home() / ".pi"
        has_env = bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("PI_API_KEY"))
        auth_ok = installed and (has_env or pi_dir.is_dir())
        detail = (
            "ready (pi --mode rpc)"
            if (installed and auth_ok)
            else ("installed; missing ~/.pi or API key env" if installed else "pi binary not found on PATH")
        )
        return DriverDiagnostic(
            kind=self.kind,
            display_name=self.display_name,
            installed=installed,
            binary_path=bin_path,
            version=version,
            auth_ok=auth_ok,
            protocol=self.protocol,
            detail=detail,
        )

    def sync_workspace(self, workspace: Path, mcp_url: str | None = None) -> dict[str, Any]:
        ws = workspace.expanduser().resolve()
        skills_res = ensure_workspace_skills(ws)
        sess_dir = self._session_dir(ws)
        sess_dir.mkdir(parents=True, exist_ok=True)
        mcp_path = self._default_mcp_config_path(ws)
        write_mcp_json_file(mcp_path, ws, mcp_url=mcp_url)
        return {
            "ok": True,
            "driver": self.kind,
            "workspace": str(ws),
            "skills_dir": skills_res["skills_dir"],
            "session_dir": str(sess_dir),
            "mcp_config_path": str(mcp_path),
        }

    def build_launch_command(
        self,
        worktree: Path,
        prompt: str,
        session_id: str,
        resume_id: str | None = None,
        mcp_config_path: Path | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        bin_path = self._find_binary() or "pi"
        wt = worktree.expanduser().resolve()
        cfg_path = mcp_config_path or self._default_mcp_config_path(wt)
        sess_dir = cfg_path.parent / "sessions"
        # Look for ancestor `.agents/skills` (handles worktree inside `<ws>/<op>/.worktrees/<track>`)
        skills_dir = wt / ".agents" / "skills"
        for cand_root in (wt, *wt.parents):
            cand_skills = cand_root / ".agents" / "skills"
            if cand_skills.is_dir():
                skills_dir = cand_skills
                break
        target_sid = resume_id or session_id
        cmd = [
            bin_path,
            "--mode",
            "rpc",
            "--no-context-files",
            "--session-dir",
            str(sess_dir),
            "--session-id",
            target_sid,
            "--skill",
            str(skills_dir),
        ]
        env = {
            "PARHELION_HOME": str(resolve_parhelion_home()),
            "PARHELION_MCP_CONFIG": str(cfg_path),
            "PARHELION_SESSION_ID": session_id,
        }
        return cmd, env

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
        log_path.parent.mkdir(parents=True, exist_ok=True)
        merged_env = worker_sandbox.worker_env(os.environ, env)
        with log_path.open("a", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                cmd,
                cwd=str(worktree),
                env=merged_env,
                stdin=subprocess.PIPE,
                stdout=lf,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
            )
            assert proc.stdin is not None
            proc.stdin.write(PiRpcClient.encode_prompt(brief))
            proc.stdin.flush()
            proc.stdin.close()
            rc = proc.wait()
            lf.write(json.dumps({"event": "exit", "driver": self.kind, "returncode": rc}) + "\n")
        return int(rc)
