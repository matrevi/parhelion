"""Extended CLI-exec Harness Drivers: Gemini CLI (`gemini`) and re-exported DeepSeek Harness (`dsh`)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .base import (
    BaseDriver,
    DriverDiagnostic,
    ensure_workspace_skills,
    probe_binary_version,
    resolve_parhelion_home,
    which_binary,
    write_mcp_json_file,
)
from .dsh_driver import DeepSeekDriver

__all__ = ["GeminiDriver", "DeepSeekDriver"]


class GeminiDriver(BaseDriver):
    """Unified Gemini CLI driver (`gemini`)."""

    kind = "gemini"
    display_name = "Gemini CLI"
    protocol = "cli-exec"
    gui_apps = ()

    def _find_binary(self) -> str:
        return which_binary(("gemini",))

    def discover(self) -> DriverDiagnostic:
        bin_path = self._find_binary()
        installed = bool(bin_path)
        version = probe_binary_version(bin_path) if bin_path else ""
        has_env = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
        gemini_dir = Path.home() / ".gemini"
        auth_ok = installed and (has_env or gemini_dir.is_dir())
        detail = (
            "ready (gemini -p)"
            if (installed and auth_ok)
            else ("installed; missing GEMINI_API_KEY or ~/.gemini" if installed else "gemini binary not found on PATH")
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
        mcp_path = self._default_mcp_config_path(ws)
        write_mcp_json_file(mcp_path, ws, mcp_url=mcp_url)
        return {
            "ok": True,
            "driver": self.kind,
            "workspace": str(ws),
            "skills_dir": skills_res["skills_dir"],
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
        bin_path = self._find_binary() or "gemini"
        cfg_path = mcp_config_path or self._default_mcp_config_path(worktree)
        cmd = [bin_path, "-p", prompt]
        if resume_id:
            cmd.extend(["--resume", resume_id])
        env = {
            "PARHELION_HOME": str(resolve_parhelion_home()),
            "PARHELION_MCP_CONFIG": str(cfg_path),
            "PARHELION_SESSION_ID": session_id,
            "PARHELION_WORKTREE": str(worktree.expanduser().resolve()),
        }
        return cmd, env
