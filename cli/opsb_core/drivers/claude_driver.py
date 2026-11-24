"""Claude Code Harness Driver (`claude -p stream-json` + `.claude/skills` workspace symlink)."""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

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
from .macos_gui import inspect_macos_app_bundle


def coerce_claude_uuid(raw_id: str | None) -> str:
    """Return a valid UUID string required by `claude --session-id` / `--resume`."""
    if raw_id:
        try:
            return str(uuid.UUID(raw_id))
        except ValueError:
            return str(uuid.uuid5(uuid.NAMESPACE_URL, f"parhelion:claude:{raw_id}"))
    return str(uuid.uuid4())


class ClaudeStreamJsonClient:
    """NDJSON formatter and parser for `claude -p --input-format stream-json --output-format stream-json`."""

    @staticmethod
    def encode_user_message(prompt: str) -> str:
        payload = {
            "type": "user",
            "message": {"role": "user", "content": prompt},
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"

    @staticmethod
    def parse_output_lines(lines: list[str] | str) -> list[dict[str, Any]]:
        raw_lines = lines.splitlines() if isinstance(lines, str) else lines
        events: list[dict[str, Any]] = []
        for ln in raw_lines:
            s = ln.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                events.append(obj)
        return events


class ClaudeDriver(BaseDriver):
    """Unified Claude Code driver (`claude -p stream-json`)."""

    kind = "claude"
    display_name = "Claude Code"
    protocol = "stream-json"
    gui_apps = ("Claude",)

    def _find_binary(self) -> str:
        return which_binary(("claude",))

    def _new_native_id(self, session_id: str, resume_id: str | None) -> str:
        return coerce_claude_uuid(resume_id or session_id)

    def _gui_candidates(self) -> list[Path]:
        return [Path("/Applications/Claude.app"), Path.home() / "Applications" / "Claude.app"]

    def discover_gui(self) -> dict[str, Any]:
        return inspect_macos_app_bundle(self._gui_candidates(), expected_bundle_ids={"com.anthropic.claudefordesktop"})

    def discover(self) -> DriverDiagnostic:
        gui = self.discover_gui()
        bin_path = self._find_binary()
        installed = bool(bin_path) or bool(gui["gui_installed"])
        version = probe_binary_version(bin_path) if bin_path else str(gui.get("gui_version") or "")
        has_env = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
        claude_dir = Path.home() / ".claude"
        auth_ok = installed and (has_env or (claude_dir / ".credentials.json").is_file() or claude_dir.is_dir())
        if not installed:
            detail = "claude binary not found on PATH"
        elif not bin_path and gui["gui_installed"]:
            detail = "ready (Claude Desktop GUI; claude CLI not on PATH)"
        elif not auth_ok:
            detail = "installed; missing ANTHROPIC_API_KEY or ~/.claude session"
        else:
            detail = "ready (claude -p stream-json)"
        return DriverDiagnostic(
            kind=self.kind,
            display_name=self.display_name,
            installed=installed,
            binary_path=bin_path,
            version=version,
            auth_ok=auth_ok,
            protocol=self.protocol,
            detail=detail,
            gui_installed=bool(gui["gui_installed"]),
            gui_app_path=str(gui["gui_app_path"]),
            gui_bundle_id=str(gui["gui_bundle_id"]),
            gui_version=str(gui["gui_version"]),
            url_scheme=str(gui["url_scheme"]),
        )

    def sync_workspace(self, workspace: Path, mcp_url: str | None = None) -> dict[str, Any]:
        ws = workspace.expanduser().resolve()
        skills_res = ensure_workspace_skills(ws)

        # Symlink `<ws>/.claude/skills -> ../.agents/skills` inside the workspace only
        claude_ws_dir = ws / ".claude"
        claude_ws_dir.mkdir(parents=True, exist_ok=True)
        link_path = claude_ws_dir / "skills"
        rel_target = Path("..") / ".agents" / "skills"
        symlink_status = "preserved"
        if link_path.is_symlink():
            if link_path.readlink() != rel_target:
                link_path.unlink()
                link_path.symlink_to(rel_target)
                symlink_status = "updated"
        elif not link_path.exists():
            link_path.symlink_to(rel_target)
            symlink_status = "created"
        elif link_path.is_dir() and not any(link_path.iterdir()):
            link_path.rmdir()
            link_path.symlink_to(rel_target)
            symlink_status = "replaced-empty-dir"

        mcp_path = self._default_mcp_config_path(ws)
        write_mcp_json_file(mcp_path, ws, mcp_url=mcp_url)

        return {
            "ok": True,
            "driver": self.kind,
            "workspace": str(ws),
            "skills_dir": skills_res["skills_dir"],
            "claude_skills_symlink": str(link_path),
            "symlink_target": str(rel_target),
            "symlink_status": symlink_status,
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
        bin_path = self._find_binary() or "claude"
        cfg_path = mcp_config_path or self._default_mcp_config_path(worktree)
        claude_uuid = coerce_claude_uuid(resume_id or session_id)
        cmd = [
            bin_path,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "bypassPermissions",
            "--setting-sources",
            "local",
            "--strict-mcp-config",
            "--mcp-config",
            str(cfg_path),
        ]
        if resume_id:
            cmd.extend(["--resume", claude_uuid])
        else:
            cmd.extend(["--session-id", claude_uuid])

        env = {
            "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
            "PARHELION_HOME": str(resolve_parhelion_home()),
            "PARHELION_SESSION_ID": session_id,
            "PARHELION_NATIVE_SESSION_ID": claude_uuid,
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
        merged_env = {**os.environ, **env}
        in_msg = ClaudeStreamJsonClient.encode_user_message(brief)
        with log_path.open("a", encoding="utf-8") as lf:
            lf.write(json.dumps({"event": "launch", "driver": self.kind, "cmd": cmd}) + "\n")
            lf.flush()
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
            proc.stdin.write(in_msg)
            proc.stdin.flush()
            proc.stdin.close()
            rc = proc.wait()
            lf.write(json.dumps({"event": "exit", "driver": self.kind, "returncode": rc}) + "\n")
        return int(rc)
