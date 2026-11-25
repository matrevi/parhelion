"""Persistent multi-turn headless worker process adapters with strict worker-role MCP isolation."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .. import config as _cfg
from ..drivers.antigravity_acp import AcpSessionClient, build_acp_mcp_servers
from ..drivers.base import resolve_parhelion_home, resolve_workspace_state_dir
from ..drivers.claude_driver import ClaudeStreamJsonClient, coerce_claude_uuid
from ..drivers.codex_appserver import CodexAppServerClient, render_codex_config_toml
from ..drivers.macos_gui import write_mcp_json_file
from ..drivers.registry import get_driver

EventCallback = Callable[[str, str], None]


def terminate_process_group(proc: subprocess.Popen[Any] | None, pgid: int | None, grace_sec: float = 2.0) -> None:
    """Terminate an entire child process group (`-pgid`) to prevent orphan compiler/test processes."""
    if proc is None and not pgid:
        return
    target_pgid = pgid
    if target_pgid is None and proc is not None:
        try:
            target_pgid = os.getpgid(proc.pid)
        except OSError:
            target_pgid = None
    if target_pgid:
        try:
            os.killpg(target_pgid, signal.SIGTERM)
        except OSError:
            pass
    elif proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
    if proc is not None:
        deadline = time.monotonic() + grace_sec
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc.poll() is None:
            if target_pgid:
                try:
                    os.killpg(target_pgid, signal.SIGKILL)
                except OSError:
                    pass
            try:
                proc.kill()
            except OSError:
                pass


def worker_runtime_dir(workspace: Path, agent_id: str) -> Path:
    d = resolve_workspace_state_dir(workspace.expanduser().resolve()).parent / "agents" / agent_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _daemon_worker_mcp_url(agent_id: str) -> str | None:
    # Prefer stdio `opsb mcp serve --role worker --agent-id <id>` (proxying to daemon) for universal compatibility
    return None


class BaseWorkerAdapter:
    """Unified persistent worker runtime interface managed by `AgentMeshManager`."""

    def __init__(self, workspace: Path, worktree: Path, driver_kind: str, agent_id: str, log_path: Path) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.worktree = worktree.expanduser().resolve()
        self.driver_kind = driver_kind
        self.agent_id = agent_id
        self.log_path = log_path
        self.runtime_dir = worker_runtime_dir(self.workspace, self.agent_id)
        self.proc: subprocess.Popen[str] | None = None
        self.pid: int | None = None
        self.pgid: int | None = None
        self.native_session_id: str = ""
        self._lock = threading.Lock()
        self._closed = False

    def _spawn_proc(self, cmd: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        child_env = {**os.environ, **env}
        if self.driver_kind == "deepseek":
            # DSH filters indexed Git keys from its shell tool but retains COUNT.
            # Use the worker account's normal Git configuration and credential store.
            child_env = {k: v for k, v in child_env.items() if k != "GIT_CONFIG_COUNT"
                         and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
        proc = subprocess.Popen(
            cmd,
            cwd=str(self.worktree),
            env=child_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            start_new_session=True,
        )
        self.proc = proc
        self.pid = proc.pid
        try:
            self.pgid = os.getpgid(proc.pid)
        except OSError:
            self.pgid = proc.pid
        threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True).start()
        return proc

    def _drain_stderr(self, proc: subprocess.Popen[str]) -> None:
        if proc.stderr is None:
            return
        try:
            with self.log_path.open("a", encoding="utf-8") as lf:
                for line in proc.stderr:
                    s = line.strip()
                    if s:
                        lf.write(json.dumps({"event": "stderr", "text": s}, ensure_ascii=False) + "\n")
                        lf.flush()
        except Exception:
            pass

    def send_turn(self, prompt: str, on_event: EventCallback | None = None) -> str:
        raise NotImplementedError

    def interrupt(self) -> None:
        pass

    def stop(self) -> None:
        self._closed = True
        with self._lock:
            proc, pgid = self.proc, self.pgid
            self.proc = None
        terminate_process_group(proc, pgid)


class AcpWorkerAdapter(BaseWorkerAdapter):
    """Persistent ACP v1 stdio adapter for DeepSeek Harness (`dsh --profile acp`) & Antigravity (`agy acp`)."""

    def __init__(self, workspace: Path, worktree: Path, driver_kind: str, agent_id: str, log_path: Path) -> None:
        super().__init__(workspace, worktree, driver_kind, agent_id, log_path)
        self._client: AcpSessionClient | None = None
        self._log_fh: Any = None

    def _ensure_started(self, on_event: EventCallback | None) -> AcpSessionClient:
        if self._client is not None and self.proc is not None and self.proc.poll() is None:
            self._client._on_event = lambda u: self._forward_event(u, on_event)
            return self._client
        drv = get_driver(self.driver_kind)
        diag = drv.discover()
        if not diag.binary_path:
            raise RuntimeError(f"Driver '{self.driver_kind}' CLI binary not available ({diag.detail})")
        cmd, env = drv.build_launch_command(self.worktree, "", self.agent_id)
        env["PARHELION_MCP_ROLE"] = "worker"
        env["PARHELION_AGENT_ID"] = self.agent_id
        if self.driver_kind == "deepseek":
            env["DSH_PERMISSION_MODE"] = "danger-full-access"
        proc = self._spawn_proc(cmd, env)
        assert proc.stdin is not None and proc.stdout is not None
        self._log_fh = self.log_path.open("a", encoding="utf-8")
        client = AcpSessionClient(
            proc.stdin, proc.stdout, log_stream=self._log_fh,
            on_event=lambda u: self._forward_event(u, on_event),
        )
        client.initialize()
        mcp_servers = build_acp_mcp_servers(
            self.workspace, mcp_url=_daemon_worker_mcp_url(self.agent_id), role="worker", agent_id=self.agent_id
        )
        sid, _ = client.open_session(cwd=self.worktree, mcp_servers=mcp_servers, resume_id=self.native_session_id or None)
        self.native_session_id = sid or self.native_session_id or self.agent_id
        self._client = client
        return client

    @staticmethod
    def _forward_event(update: dict[str, Any], on_event: EventCallback | None) -> None:
        if not callable(on_event):
            return
        u_type = str(update.get("sessionUpdate") or update.get("type") or "")
        if u_type in ("tool_call", "tool_call_update"):
            tool = str(update.get("title") or update.get("name") or update.get("toolName") or u_type)
            on_event("tool", tool[:48])

    def send_turn(self, prompt: str, on_event: EventCallback | None = None) -> str:
        client = self._ensure_started(on_event)
        res = client.send_prompt(self.native_session_id, prompt)
        return str(res.get("text") or res.get("stopReason") or "Turn completed.")

    def interrupt(self) -> None:
        if self._client and self.native_session_id:
            try:
                self._client.cancel_session(self.native_session_id)
            except Exception:
                pass

    def stop(self) -> None:
        super().stop()
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None


class CodexWorkerAdapter(BaseWorkerAdapter):
    """Persistent `codex app-server --listen stdio://` worker with isolated per-worker `CODEX_HOME`."""

    def __init__(self, workspace: Path, worktree: Path, driver_kind: str, agent_id: str, log_path: Path) -> None:
        super().__init__(workspace, worktree, driver_kind, agent_id, log_path)
        self._client: CodexAppServerClient | None = None
        self._log_fh: Any = None

    def _prepare_worker_codex_home(self) -> Path:
        c_home = self.runtime_dir / "codex_home"
        c_home.mkdir(parents=True, exist_ok=True)
        toml_path = c_home / "config.toml"
        toml_path.write_text(
            render_codex_config_toml(self.workspace, role="worker", agent_id=self.agent_id, tool_timeout_sec=120),
            encoding="utf-8",
        )
        host_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
        host_auth = host_home / "auth.json"
        worker_auth = c_home / "auth.json"
        if host_auth.is_file() and not worker_auth.exists() and not worker_auth.is_symlink():
            try:
                worker_auth.symlink_to(host_auth)
            except OSError:
                pass
        return c_home

    def _ensure_started(self, on_event: EventCallback | None) -> CodexAppServerClient:
        if self._client is not None and self.proc is not None and self.proc.poll() is None:
            self._client._on_event = lambda m: self._forward_event(m, on_event)
            return self._client
        drv = get_driver("codex")
        diag = drv.discover()
        if not diag.binary_path:
            raise RuntimeError(f"Codex CLI binary not available ({diag.detail})")
        worker_home = self._prepare_worker_codex_home()
        cmd = [diag.binary_path, "app-server", "--listen", "stdio://"]
        env = {
            "CODEX_HOME": str(worker_home),
            "PARHELION_HOME": str(resolve_parhelion_home()),
            "PARHELION_MCP_ROLE": "worker",
            "PARHELION_AGENT_ID": self.agent_id,
        }
        proc = self._spawn_proc(cmd, env)
        assert proc.stdin is not None and proc.stdout is not None
        self._log_fh = self.log_path.open("a", encoding="utf-8")
        client = CodexAppServerClient(
            proc.stdin, proc.stdout, log_stream=self._log_fh,
            on_event=lambda m: self._forward_event(m, on_event),
        )
        client.initialize()
        tid = client.open_thread(
            self.worktree, resume_id=self.native_session_id or None,
            approval_policy="never", sandbox="workspace-write",
        )
        self.native_session_id = tid or self.native_session_id or self.agent_id
        self._client = client
        return client

    @staticmethod
    def _forward_event(msg: dict[str, Any], on_event: EventCallback | None) -> None:
        if not callable(on_event):
            return
        method = str(msg.get("method") or "")
        if method in ("item/started", "item/completed"):
            item = (msg.get("params") or {}).get("item") if isinstance(msg.get("params"), dict) else {}
            if isinstance(item, dict):
                t = str(item.get("name") or item.get("type") or "")
                if t:
                    on_event("tool", t[:48])

    def send_turn(self, prompt: str, on_event: EventCallback | None = None) -> str:
        client = self._ensure_started(on_event)
        res = client.start_turn(
            self.native_session_id, prompt, approval_policy="never", sandbox="workspace-write"
        )
        return str(res.get("text") or "Codex turn completed.")

    def interrupt(self) -> None:
        if self._client and self.native_session_id:
            try:
                self._client.interrupt_turn(self.native_session_id)
            except Exception:
                pass

    def stop(self) -> None:
        super().stop()
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None


class ClaudeWorkerAdapter(BaseWorkerAdapter):
    """Persistent `claude -p --input-format stream-json --output-format stream-json` worker with strict worker MCP."""

    def __init__(self, workspace: Path, worktree: Path, driver_kind: str, agent_id: str, log_path: Path) -> None:
        super().__init__(workspace, worktree, driver_kind, agent_id, log_path)
        self.native_session_id = coerce_claude_uuid(agent_id)
        self._has_started_once = False

    def _ensure_started(self) -> subprocess.Popen[str]:
        if self.proc is not None and self.proc.poll() is None:
            return self.proc
        drv = get_driver("claude")
        diag = drv.discover()
        if not diag.binary_path:
            raise RuntimeError(f"Claude Code binary not available ({diag.detail})")
        mcp_cfg = write_mcp_json_file(
            self.runtime_dir / "mcp_config.json", self.workspace, role="worker", agent_id=self.agent_id
        )
        cmd, env = drv.build_launch_command(
            self.worktree, "", self.agent_id,
            resume_id=self.native_session_id if self._has_started_once else None,
            mcp_config_path=mcp_cfg,
        )
        env["PARHELION_MCP_ROLE"] = "worker"
        env["PARHELION_AGENT_ID"] = self.agent_id
        self._has_started_once = True
        return self._spawn_proc(cmd, env)

    def send_turn(self, prompt: str, on_event: EventCallback | None = None) -> str:
        proc = self._ensure_started()
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(ClaudeStreamJsonClient.encode_user_message(prompt))
        proc.stdin.flush()
        chunks: list[str] = []
        result_text = ""
        with self.log_path.open("a", encoding="utf-8") as lf:
            while True:
                raw = proc.stdout.readline()
                if raw == "":
                    break
                s = raw.strip()
                if not s:
                    continue
                lf.write(s + "\n")
                lf.flush()
                try:
                    ev = json.loads(s)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                ev_type = str(ev.get("type") or "")
                if ev_type == "assistant":
                    msg_obj = ev.get("message") if isinstance(ev.get("message"), dict) else {}
                    for blk in msg_obj.get("content") or []:
                        if not isinstance(blk, dict):
                            continue
                        if blk.get("type") == "text" and blk.get("text"):
                            chunks.append(str(blk["text"]))
                        elif blk.get("type") == "tool_use" and callable(on_event):
                            on_event("tool", str(blk.get("name") or "tool")[:48])
                elif ev_type == "result":
                    if ev.get("result"):
                        result_text = str(ev["result"])
                    if ev.get("is_error"):
                        raise RuntimeError(result_text or "Claude turn failed")
                    break
        return (result_text or "\n".join(chunks)).strip() or "Claude turn completed."

    def interrupt(self) -> None:
        if self.proc is not None and self.proc.poll() is None and self.proc.stdin is not None:
            try:
                ctrl = {"type": "control_request", "request_id": f"int-{int(time.time())}", "request": {"subtype": "interrupt"}}
                self.proc.stdin.write(json.dumps(ctrl) + "\n")
                self.proc.stdin.flush()
            except Exception:
                pass


class CliTurnWorkerAdapter(BaseWorkerAdapter):
    """Per-turn fallback adapter for `gemini` / `pi` with worker-scoped MCP config."""

    def send_turn(self, prompt: str, on_event: EventCallback | None = None) -> str:
        drv = get_driver(self.driver_kind)
        diag = drv.discover()
        if not diag.binary_path:
            raise RuntimeError(f"Driver '{self.driver_kind}' CLI not available ({diag.detail})")
        mcp_cfg = write_mcp_json_file(
            self.runtime_dir / "mcp_config.json", self.workspace, role="worker", agent_id=self.agent_id
        )
        cmd, env = drv.build_launch_command(
            self.worktree, prompt, self.agent_id,
            resume_id=self.native_session_id or None, mcp_config_path=mcp_cfg,
        )
        env["PARHELION_MCP_ROLE"] = "worker"
        env["PARHELION_AGENT_ID"] = self.agent_id
        proc = self._spawn_proc(cmd, env)
        assert proc.stdout is not None
        out = proc.stdout.read()
        rc = proc.wait()
        self.native_session_id = self.native_session_id or self.agent_id
        if rc != 0:
            raise RuntimeError(f"{self.driver_kind} exited with rc={rc}: {out[-240:]}")
        return out.strip()[-2000:] or f"{self.driver_kind} turn completed."


def create_worker_adapter(
    workspace: Path, worktree: Path, driver_kind: str, agent_id: str, log_path: Path
) -> BaseWorkerAdapter:
    kind = driver_kind.strip().lower()
    if kind in ("deepseek", "antigravity"):
        return AcpWorkerAdapter(workspace, worktree, kind, agent_id, log_path)
    if kind == "codex":
        return CodexWorkerAdapter(workspace, worktree, kind, agent_id, log_path)
    if kind == "claude":
        return ClaudeWorkerAdapter(workspace, worktree, kind, agent_id, log_path)
    return CliTurnWorkerAdapter(workspace, worktree, kind, agent_id, log_path)
