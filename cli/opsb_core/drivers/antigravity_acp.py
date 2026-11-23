"""Pure ACP JSON-RPC 2.0 client over line-delimited stdio for `agy acp`."""
from __future__ import annotations

import json
import os
import select
import subprocess
import time
from pathlib import Path
from typing import IO, Any

from .. import config as _cfg
from .macos_gui import build_parhelion_mcp_command


class AcpSessionClient:
    """Native ACP JSON-RPC 2.0 client over line-delimited stdio."""

    def __init__(
        self,
        stdin_stream: IO[str],
        stdout_stream: IO[str],
        log_stream: IO[str] | None = None,
        quiet_window_sec: float = 0.25,
    ) -> None:
        self._stdin = stdin_stream
        self._stdout = stdout_stream
        self._log = log_stream
        self.quiet_window_sec = quiet_window_sec
        self._next_id = 1
        self.notifications: list[dict[str, Any]] = []
        self.open_tool_calls: set[str] = set()

    def _log_line(self, direction: str, payload: Any) -> None:
        if self._log is None:
            return
        row = {"ts": _cfg.utc_now().isoformat(), "dir": direction, "msg": payload}
        self._log.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._log.flush()

    def _write_msg(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
        self._log_line("out", msg)
        self._stdin.write(line + "\n")
        self._stdin.flush()

    def _track_notification(self, msg: dict[str, Any]) -> None:
        self.notifications.append(msg)
        if msg.get("method") != "session/update":
            return
        params = msg.get("params") or {}
        update = params.get("update") if isinstance(params.get("update"), dict) else params
        if not isinstance(update, dict):
            return
        u_type = str(update.get("sessionUpdate") or update.get("type") or "")
        tc_id = str(update.get("toolCallId") or update.get("id") or "")
        status = str(update.get("status") or "").lower()
        if u_type == "tool_call" and tc_id and status in ("", "pending", "in_progress", "running"):
            self.open_tool_calls.add(tc_id)
        elif u_type in ("tool_call_update", "tool_call") and tc_id:
            if status in ("completed", "failed", "error", "cancelled", "done"):
                self.open_tool_calls.discard(tc_id)
            elif status in ("pending", "in_progress", "running"):
                self.open_tool_calls.add(tc_id)

    def _read_until_id(self, target_id: int) -> dict[str, Any]:
        while True:
            raw = self._stdout.readline()
            if raw == "":
                raise RuntimeError(f"ACP stdout closed before response id={target_id}")
            s = raw.strip()
            if not s:
                continue
            try:
                msg = json.loads(s)
            except json.JSONDecodeError:
                continue
            self._log_line("in", msg)
            if isinstance(msg, dict) and msg.get("id") == target_id:
                return msg
            if isinstance(msg, dict) and "method" in msg:
                self._track_notification(msg)

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        req_id = self._next_id
        self._next_id += 1
        self._write_msg({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return self._read_until_id(req_id)

    def initialize(self) -> dict[str, Any]:
        resp = self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
                "clientInfo": {"name": "parhelion", "version": "3.0.3"},
            },
        )
        if "error" in resp:
            raise RuntimeError(f"ACP initialize failed: {resp['error']}")
        return resp.get("result") or {}

    def open_session(
        self,
        cwd: Path,
        mcp_servers: list[dict[str, Any]],
        system_append: str = "",
        resume_id: str | None = None,
    ) -> tuple[str, bool]:
        """Open ACP session via `session/load` (checking `replayedPrompts > 0`) or `session/new`."""
        meta_block = {"antigravity": {"systemPrompt": {"append": system_append}}} if system_append else {}
        if resume_id:
            load_params: dict[str, Any] = {"sessionId": resume_id, "cwd": str(cwd), "mcpServers": mcp_servers}
            if meta_block:
                load_params["_meta"] = meta_block
            resp = self.request("session/load", load_params)
            res = resp.get("result") if isinstance(resp.get("result"), dict) else None
            if "error" not in resp and res is not None and int(res.get("replayedPrompts") or 0) > 0:
                return str(res.get("sessionId") or resume_id), True

        new_params: dict[str, Any] = {"cwd": str(cwd), "mcpServers": mcp_servers}
        if meta_block:
            new_params["_meta"] = meta_block
        resp = self.request("session/new", new_params)
        if "error" in resp:
            raise RuntimeError(f"ACP session/new failed: {resp['error']}")
        return str((resp.get("result") or {}).get("sessionId") or ""), False

    def drain_quiet_window(self) -> None:
        """Wait bounded quiet window if async tool calls are still marked open."""
        if not self.open_tool_calls or self.quiet_window_sec <= 0:
            return
        try:
            fd = self._stdout.fileno()
        except Exception:
            return
        deadline = time.monotonic() + self.quiet_window_sec
        while self.open_tool_calls and time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], max(0.01, deadline - time.monotonic()))
            if not ready:
                break
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
            self._log_line("in", msg)
            if isinstance(msg, dict) and "method" in msg:
                self._track_notification(msg)

    def send_prompt(self, session_id: str, prompt_text: str) -> dict[str, Any]:
        resp = self.request("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt_text}]})
        self.drain_quiet_window()
        if "error" in resp:
            raise RuntimeError(f"ACP session/prompt failed: {resp['error']}")
        return resp.get("result") or {}


def build_acp_mcp_servers(workspace: Path, mcp_url: str | None = None) -> list[dict[str, Any]]:
    if mcp_url:
        return [{"name": "parhelion", "type": "http", "url": mcp_url}]
    cmd, args, env = build_parhelion_mcp_command(workspace)
    return [{"name": "parhelion", "command": cmd, "args": args, "env": [{"name": k, "value": v} for k, v in env.items()]}]


def run_acp_headless_turn(
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
    mcp_servers = build_acp_mcp_servers(workspace)
    with log_path.open("a", encoding="utf-8") as lf:
        proc = subprocess.Popen(
            cmd, cwd=str(worktree), env={**os.environ, **env},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        assert proc.stdin is not None and proc.stdout is not None
        client = AcpSessionClient(proc.stdin, proc.stdout, log_stream=lf)
        try:
            client.initialize()
            active_id, _ = client.open_session(cwd=worktree, mcp_servers=mcp_servers, resume_id=resume_id)
            if active_id:
                update_session(workspace, session_id, native_session_id=active_id)
            client.send_prompt(active_id or native_session_id, brief)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            rc = proc.wait(timeout=10)
            if proc.stderr is not None and (err_txt := proc.stderr.read()):
                lf.write(json.dumps({"event": "stderr", "text": err_txt}) + "\n")
        return int(rc)
