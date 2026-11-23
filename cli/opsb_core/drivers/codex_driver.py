"""Codex Harness Driver (`codex app-server --listen stdio://` + isolated CODEX_HOME)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import IO, Any

from .. import config as _cfg
from .base import (
    BaseDriver,
    DriverDiagnostic,
    build_parhelion_mcp_command,
    ensure_workspace_skills,
    probe_binary_version,
    resolve_parhelion_home,
    resolve_workspace_driver_dir,
    which_binary,
    write_mcp_json_file,
)


class CodexAppServerClient:
    """Native `codex app-server --listen stdio://` JSON-RPC 2.0 client."""

    def __init__(self, stdin_stream: IO[str], stdout_stream: IO[str], log_stream: IO[str] | None = None) -> None:
        self._stdin = stdin_stream
        self._stdout = stdout_stream
        self._log = log_stream
        self._next_id = 1
        self.events: list[dict[str, Any]] = []

    def _log_msg(self, direction: str, msg: Any) -> None:
        if self._log is None:
            return
        self._log.write(json.dumps({"ts": _cfg.utc_now().isoformat(), "dir": direction, "msg": msg}, ensure_ascii=False) + "\n")
        self._log.flush()

    def _write_msg(self, msg: dict[str, Any]) -> None:
        self._log_msg("out", msg)
        self._stdin.write(json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._stdin.flush()

    def _read_until_id(self, req_id: int) -> dict[str, Any]:
        while True:
            raw = self._stdout.readline()
            if raw == "":
                raise RuntimeError(f"Codex app-server stdout closed before id={req_id}")
            s = raw.strip()
            if not s:
                continue
            try:
                msg = json.loads(s)
            except json.JSONDecodeError:
                continue
            self._log_msg("in", msg)
            if isinstance(msg, dict) and msg.get("id") == req_id:
                return msg
            if isinstance(msg, dict):
                self.events.append(msg)

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        req_id = self._next_id
        self._next_id += 1
        self._write_msg({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return self._read_until_id(req_id)

    def initialize(self) -> dict[str, Any]:
        resp = self.request(
            "initialize",
            {"clientInfo": {"name": "parhelion", "title": "Parhelion", "version": "3.0.0"}},
        )
        if "error" in resp:
            raise RuntimeError(f"Codex initialize failed: {resp['error']}")
        self._write_msg({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        return resp.get("result") or {}

    def open_thread(self, cwd: Path, resume_id: str | None = None) -> str:
        if resume_id:
            resp = self.request("thread/resume", {"threadId": resume_id, "cwd": str(cwd)})
            res = resp.get("result") if isinstance(resp.get("result"), dict) else None
            if "error" not in resp and res is not None:
                thread = res.get("thread") if isinstance(res.get("thread"), dict) else {}
                tid = thread.get("id") or res.get("threadId") or resume_id
                return str(tid)
        resp = self.request("thread/start", {"cwd": str(cwd)})
        if "error" in resp:
            raise RuntimeError(f"Codex thread/start failed: {resp['error']}")
        res = resp.get("result") or {}
        thread = res.get("thread") if isinstance(res.get("thread"), dict) else {}
        return str(thread.get("id") or res.get("threadId") or "")

    def start_turn(self, thread_id: str, prompt_text: str) -> dict[str, Any]:
        resp = self.request(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": prompt_text}]},
        )
        if "error" in resp:
            raise RuntimeError(f"Codex turn/start failed: {resp['error']}")
        # Drain turn events until turn/completed or EOF
        while True:
            raw = self._stdout.readline()
            if raw == "":
                break
            s = raw.strip()
            if not s:
                continue
            try:
                msg = json.loads(s)
            except json.JSONDecodeError:
                continue
            self._log_msg("in", msg)
            if isinstance(msg, dict):
                self.events.append(msg)
                if msg.get("method") in ("turn/completed", "turn/failed"):
                    break
        return resp.get("result") or {}


def _toml_quote(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def render_codex_config_toml(workspace: Path, mcp_url: str | None = None) -> str:
    if mcp_url:
        lines = [
            "# Managed by Parhelion (isolated workspace CODEX_HOME; never edits ~/.codex/config.toml)",
            "[mcp_servers.parhelion]",
            f"url = {_toml_quote(mcp_url)}",
        ]
    else:
        cmd, args, env = build_parhelion_mcp_command(workspace)
        args_toml = ", ".join(_toml_quote(a) for a in args)
        lines = [
            "# Managed by Parhelion (isolated workspace CODEX_HOME; never edits ~/.codex/config.toml)",
            "[mcp_servers.parhelion]",
            f"command = {_toml_quote(cmd)}",
            f"args = [{args_toml}]",
            "[mcp_servers.parhelion.env]",
            *(f"{k} = {_toml_quote(v)}" for k, v in env.items()),
        ]
    return "\n".join(lines) + "\n"


class CodexDriver(BaseDriver):
    """Unified Codex driver (`codex app-server --listen stdio://` + isolated CODEX_HOME)."""

    kind = "codex"
    display_name = "Codex"
    protocol = "app-server-rpc"
    gui_apps = ("Codex",)

    def _find_binary(self) -> str:
        return which_binary(("codex",))

    def _codex_home_dir(self, workspace: Path) -> Path:
        return resolve_workspace_driver_dir(workspace, self.kind)

    def discover(self) -> DriverDiagnostic:
        bin_path = self._find_binary()
        installed = bool(bin_path)
        version = probe_binary_version(bin_path) if bin_path else ""
        auth_file = Path.home() / ".codex" / "auth.json"
        has_env = bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY"))
        auth_ok = installed and (has_env or auth_file.is_file())
        if not installed:
            detail = "codex binary not found on PATH"
        elif not auth_ok:
            detail = "installed; missing ~/.codex/auth.json or OPENAI_API_KEY"
        else:
            detail = "ready (codex app-server stdio://)"
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

        codex_home = self._codex_home_dir(ws)
        codex_home.mkdir(parents=True, exist_ok=True)
        config_toml_path = codex_home / "config.toml"
        tmp = config_toml_path.with_suffix(".tmp")
        tmp.write_text(render_codex_config_toml(ws, mcp_url=mcp_url), encoding="utf-8")
        os.replace(tmp, config_toml_path)

        # Optionally symlink read-only auth.json into isolated CODEX_HOME without touching ~/.codex/config.toml
        global_auth = Path.home() / ".codex" / "auth.json"
        isolated_auth = codex_home / "auth.json"
        if global_auth.is_file() and not isolated_auth.exists() and not isolated_auth.is_symlink():
            try:
                isolated_auth.symlink_to(global_auth)
            except OSError:
                pass

        mcp_json_path = codex_home / "mcp_config.json"
        write_mcp_json_file(mcp_json_path, ws, mcp_url=mcp_url)

        return {
            "ok": True,
            "driver": self.kind,
            "workspace": str(ws),
            "skills_dir": skills_res["skills_dir"],
            "projected_skills": skills_res["projected"],
            "codex_home": str(codex_home),
            "codex_config_toml": str(config_toml_path),
            "mcp_config_path": str(mcp_json_path),
        }

    def build_launch_command(
        self,
        worktree: Path,
        prompt: str,
        session_id: str,
        resume_id: str | None = None,
        mcp_config_path: Path | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        bin_path = self._find_binary() or "codex"
        codex_home = mcp_config_path.parent if mcp_config_path is not None else self._codex_home_dir(worktree)
        cmd = [bin_path, "app-server", "--listen", "stdio://"]
        env = {
            "CODEX_HOME": str(codex_home),
            "PARHELION_HOME": str(resolve_parhelion_home()),
            "PARHELION_SESSION_ID": session_id,
            "PARHELION_WORKTREE": str(worktree.expanduser().resolve()),
        }
        if resume_id:
            env["PARHELION_RESUME_ID"] = resume_id
        return cmd, env

    def build_exec_command(self, worktree: Path, prompt: str, session_id: str) -> tuple[list[str], dict[str, str]]:
        """Build one-shot `codex exec` command for non-interactive runs."""
        bin_path = self._find_binary() or "codex"
        cmd = [bin_path, "exec", "--cd", str(worktree.expanduser().resolve()), "--json", prompt]
        env = {
            "CODEX_HOME": str(self._codex_home_dir(worktree)),
            "PARHELION_HOME": str(resolve_parhelion_home()),
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
        from .session_store import update_session

        log_path.parent.mkdir(parents=True, exist_ok=True)
        merged_env = {**os.environ, **env}
        with log_path.open("a", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                cmd,
                cwd=str(worktree),
                env=merged_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            assert proc.stdin is not None and proc.stdout is not None
            client = CodexAppServerClient(proc.stdin, proc.stdout, log_stream=lf)
            try:
                client.initialize()
                thread_id = client.open_thread(worktree, resume_id=resume_id)
                if thread_id:
                    update_session(workspace, session_id, native_session_id=thread_id)
                client.start_turn(thread_id or native_session_id, brief)
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                rc = proc.wait(timeout=10)
                if proc.stderr is not None:
                    err_txt = proc.stderr.read()
                    if err_txt:
                        lf.write(json.dumps({"event": "stderr", "text": err_txt}) + "\n")
            return int(rc)
