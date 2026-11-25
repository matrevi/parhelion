"""Pure `codex app-server --listen stdio://` JSON-RPC 2.0 client & headless turn runner."""
from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path
from typing import IO, Any

from .. import __version__ as _OPSB_VERSION
from .. import config as _cfg
from .macos_gui import build_parhelion_mcp_command


def _toml_quote(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_toml_quote(k)} = {_toml_value(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, (str, bool, int, float)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    raise ValueError("Unsupported value in Codex model provider configuration")


def _provider_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    profile = config.get("profiles", {}).get(config.get("profile"), {})
    provider = profile.get("model_provider", config.get("model_provider"))
    settings = {k: config[k] for k in ("openai_base_url",) if k in config}
    if provider:
        settings["model_provider"] = provider
        providers = config.get("model_providers", {})
        if provider in providers:
            settings["model_providers"] = {provider: providers[provider]}
    return settings


def _strip_toml_sections(text: str, section_headers: set[str]) -> str:
    """Remove exact `[header]` sections (preserving all other TOML lines & comments)."""
    out: list[str] = []
    skipping = False
    for ln in text.splitlines(keepends=True):
        stripped = ln.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if stripped in section_headers:
                skipping = True
                continue
            skipping = False
        if not skipping:
            out.append(ln)
    return "".join(out)


def render_codex_config_toml(
    workspace: Path,
    mcp_url: str | None = None,
    *,
    role: str | None = None,
    agent_id: str | None = None,
    tool_timeout_sec: int = 120,
    host_config: Path | None = None,
) -> str:
    if mcp_url:
        lines = [
            "# Managed by Parhelion",
            "[mcp_servers.parhelion]",
            f"url = {_toml_quote(mcp_url)}",
            f"tool_timeout_sec = {int(tool_timeout_sec)}",
        ]
    else:
        cmd, args, env = build_parhelion_mcp_command(workspace, role=role, agent_id=agent_id)
        args_toml = ", ".join(_toml_quote(a) for a in args)
        lines = [
            "# Managed by Parhelion",
            "[mcp_servers.parhelion]",
            f"command = {_toml_quote(cmd)}",
            f"args = [{args_toml}]",
            f"tool_timeout_sec = {int(tool_timeout_sec)}",
            "[mcp_servers.parhelion.env]",
            *(f"{k} = {_toml_quote(v)}" for k, v in env.items()),
        ]
    if host_config is not None:
        lines = [*(f"{k} = {_toml_value(v)}" for k, v in _provider_settings(host_config).items()), *lines]
    if role == "worker":
        from ..mcp.schemas import TOOL_SCHEMAS
        for schema in TOOL_SCHEMAS:
            if "worker" in schema.get("roles", ("orchestrator", "worker")):
                lines += [f"[mcp_servers.parhelion.tools.{schema['name']}]", 'approval_mode = "approve"']
    return "\n".join(lines) + "\n"


def upsert_workspace_codex_toml(ws_toml_path: Path, workspace: Path, mcp_url: str | None = None) -> None:
    ws_toml_path.parent.mkdir(parents=True, exist_ok=True)
    existing = ws_toml_path.read_text(encoding="utf-8") if ws_toml_path.is_file() else ""
    base = _strip_toml_sections(existing, {"[mcp_servers.parhelion]", "[mcp_servers.parhelion.env]"})
    block = render_codex_config_toml(workspace, mcp_url=mcp_url)
    merged = (base.rstrip() + "\n\n" + block) if base.strip() else block
    tomllib.loads(merged)
    tmp = ws_toml_path.with_suffix(".tmp")
    tmp.write_text(merged, encoding="utf-8")
    os.replace(tmp, ws_toml_path)


def ensure_codex_project_trusted(config_toml_path: Path, real_ws: str) -> bool:
    """Idempotently add `[projects."<real_ws>"] trust_level = "trusted"` without clobbering existing TOML."""
    config_toml_path.parent.mkdir(parents=True, exist_ok=True)
    existing = config_toml_path.read_text(encoding="utf-8") if config_toml_path.is_file() else ""
    if existing.strip():
        try:
            projects = tomllib.loads(existing).get("projects")
            if isinstance(projects, dict) and isinstance(projects.get(real_ws), dict) and projects[real_ws].get("trust_level") == "trusted":
                return False
        except tomllib.TOMLDecodeError:
            pass
    header = f"[projects.{_toml_quote(real_ws)}]"
    cleaned = _strip_toml_sections(existing, {header})
    snippet = f'{header}\ntrust_level = "trusted"\n'
    new_text = (cleaned.rstrip() + "\n\n" + snippet) if cleaned.strip() else snippet
    tomllib.loads(new_text)
    tmp = config_toml_path.with_suffix(".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, config_toml_path)
    return True


class CodexAppServerClient:
    """Native `codex app-server --listen stdio://` JSON-RPC 2.0 client."""

    def __init__(
        self,
        stdin_stream: IO[str],
        stdout_stream: IO[str],
        log_stream: IO[str] | None = None,
        on_event: Any = None,
    ) -> None:
        self._stdin = stdin_stream
        self._stdout = stdout_stream
        self._log = log_stream
        self._on_event = on_event
        self._next_id = 1
        self.events: list[dict[str, Any]] = []
        self.last_turn_id: str = ""
        self.last_assistant_text: str = ""

    def _log_msg(self, direction: str, msg: Any) -> None:
        if self._log is None:
            return
        self._log.write(json.dumps({"ts": _cfg.utc_now().isoformat(), "dir": direction, "msg": msg}, ensure_ascii=False) + "\n")
        self._log.flush()

    def _write_msg(self, msg: dict[str, Any]) -> None:
        self._log_msg("out", msg)
        self._stdin.write(json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._stdin.flush()

    def _handle_server_request(self, msg: dict[str, Any]) -> None:
        """Auto-answer server-initiated approval requests so headless turns never deadlock."""
        req_id = msg.get("id")
        method = str(msg.get("method") or "")
        if "approval" in method.lower() or "requestapproval" in method.lower():
            self._write_msg({"jsonrpc": "2.0", "id": req_id, "result": {"decision": "accept"}})
            return
        self._write_msg({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unsupported server callback in headless mode: {method}"},
        })

    def _record_event(self, msg: dict[str, Any]) -> None:
        self.events.append(msg)
        method = str(msg.get("method") or "")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if method in ("item/agentMessage/delta", "agentMessage/delta"):
            delta = str(params.get("delta") or params.get("text") or "")
            if delta:
                self.last_assistant_text += delta
        elif method == "item/completed":
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            if str(item.get("type") or "") in ("agentMessage", "message"):
                txt = str(item.get("text") or "")
                if txt:
                    self.last_assistant_text = txt
        if callable(self._on_event):
            try:
                self._on_event(msg)
            except Exception:
                pass

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
            if not isinstance(msg, dict):
                continue
            if msg.get("id") == req_id and ("result" in msg or "error" in msg):
                return msg
            if "id" in msg and "method" in msg and "result" not in msg and "error" not in msg:
                self._handle_server_request(msg)
                continue
            self._record_event(msg)

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        req_id = self._next_id
        self._next_id += 1
        self._write_msg({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return self._read_until_id(req_id)

    def initialize(self) -> dict[str, Any]:
        resp = self.request("initialize", {"clientInfo": {"name": "parhelion", "title": "Parhelion", "version": _OPSB_VERSION}})
        if "error" in resp:
            raise RuntimeError(f"Codex initialize failed: {resp['error']}")
        self._write_msg({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        return resp.get("result") or {}

    def open_thread(
        self,
        cwd: Path,
        resume_id: str | None = None,
        *,
        persist_extended_history: bool = False,
        model: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
    ) -> str:
        if resume_id:
            r_params: dict[str, Any] = {"threadId": resume_id, "cwd": str(cwd)}
            if model:
                r_params["model"] = model
            if approval_policy:
                r_params["approvalPolicy"] = approval_policy
            if sandbox:
                r_params["sandbox"] = sandbox
            resp = self.request("thread/resume", r_params)
            res = resp.get("result") if isinstance(resp.get("result"), dict) else None
            if "error" not in resp and res is not None:
                if model and res.get("model") != model:
                    raise RuntimeError(f"Codex did not confirm worker model {model!r}")
                thread = res.get("thread") if isinstance(res.get("thread"), dict) else {}
                return str(thread.get("id") or res.get("threadId") or resume_id)
        params: dict[str, Any] = {"cwd": str(cwd)}
        if model:
            params["model"] = model
        if persist_extended_history:
            params["persistExtendedHistory"] = True
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        if sandbox:
            params["sandbox"] = sandbox
        resp = self.request("thread/start", params)
        if "error" in resp:
            raise RuntimeError(f"Codex thread/start failed: {resp['error']}")
        res = resp.get("result") or {}
        if model and res.get("model") != model:
            raise RuntimeError(f"Codex did not confirm worker model {model!r}")
        thread = res.get("thread") if isinstance(res.get("thread"), dict) else {}
        return str(thread.get("id") or res.get("threadId") or "")

    def set_thread_name(self, thread_id: str, name: str) -> dict[str, Any]:
        resp = self.request("thread/name/set", {"threadId": thread_id, "name": name})
        if "error" in resp:
            raise RuntimeError(f"Codex thread/name/set failed: {resp['error']}")
        return resp.get("result") or {}

    def interrupt_turn(self, thread_id: str, turn_id: str | None = None) -> None:
        params: dict[str, Any] = {"threadId": thread_id}
        if turn_id or self.last_turn_id:
            params["turnId"] = turn_id or self.last_turn_id
        self._write_msg({"jsonrpc": "2.0", "id": self._next_id, "method": "turn/interrupt", "params": params})
        self._next_id += 1

    def start_turn(
        self,
        thread_id: str,
        prompt_text: str,
        *,
        approval_policy: str | None = None,
        sandbox: str | None = None,
    ) -> dict[str, Any]:
        self.last_assistant_text = ""
        params: dict[str, Any] = {"threadId": thread_id, "input": [{"type": "text", "text": prompt_text}]}
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        if sandbox:
            # Thread requests take a SandboxMode; turn overrides take a tagged SandboxPolicy.
            policy_types = {
                "workspace-write": "workspaceWrite",
                "read-only": "readOnly",
                "danger-full-access": "dangerFullAccess",
            }
            if sandbox not in policy_types:
                raise ValueError(f"Unsupported Codex sandbox mode: {sandbox!r}")
            params["sandboxPolicy"] = {"type": policy_types[sandbox]}
        event_start = len(self.events)
        resp = self.request("turn/start", params)
        if "error" in resp:
            raise RuntimeError(f"Codex turn/start failed: {resp['error']}")
        res = dict(resp.get("result") or {})
        turn_obj = res.get("turn") if isinstance(res.get("turn"), dict) else {}
        self.last_turn_id = str(turn_obj.get("id") or res.get("turnId") or "")
        self._wait_turn(thread_id, self.last_turn_id, event_start)
        if self.last_assistant_text and "text" not in res:
            res["text"] = self.last_assistant_text.strip()
        return res

    def compact(self, thread_id: str) -> None:
        event_start = len(self.events)
        resp = self.request("thread/compact/start", {"threadId": thread_id})
        if "error" in resp:
            raise RuntimeError(f"Codex compaction request failed: {resp['error']}")
        self._wait_turn(thread_id, "", event_start)

    def _turn_events(self, event_start: int):
        yield from self.events[event_start:]
        while True:
            raw = self._stdout.readline()
            if raw == "":
                raise RuntimeError("Codex app-server closed before turn completion")
            s = raw.strip()
            if not s:
                continue
            try:
                msg = json.loads(s)
            except json.JSONDecodeError:
                continue
            self._log_msg("in", msg)
            if not isinstance(msg, dict):
                continue
            if "id" in msg and "method" in msg and "result" not in msg and "error" not in msg:
                self._handle_server_request(msg)
                continue
            self._record_event(msg)
            yield msg

    def _wait_turn(self, thread_id: str, turn_id: str, event_start: int) -> None:
        for msg in self._turn_events(event_start):
            if msg.get("method") in ("turn/completed", "turn/failed", "turn/cancelled"):
                event = msg.get("params") or {}
                if event.get("threadId") and event["threadId"] != thread_id:
                    continue
                completed = event.get("turn") or {}
                if completed.get("id") and turn_id and completed["id"] != turn_id:
                    continue
                status = completed.get("status") or msg["method"].split("/")[-1]
                if status != "completed":
                    error = completed.get("error") or event.get("error") or {}
                    detail = error.get("codexErrorInfo") if isinstance(error, dict) else None
                    if isinstance(detail, dict):
                        reason, data = next(iter(detail.items()), ("", {}))
                        code = data.get("httpStatusCode") if isinstance(data, dict) else None
                        if code == 401:
                            raise RuntimeError("Codex API HTTP 401 Unauthorized: model provider rejected the credentials")
                        detail = f"{reason} (HTTP {code})" if code else reason
                    raise RuntimeError(f"Codex turn {status}" + (f": {detail}" if detail else ""))
                return


def preseed_codex_thread(bin_path: str, real_wt: Path, name: str, c_home: Path) -> str:
    """Pre-create a persisted Codex thread via `codex app-server` before opening the GUI deep link."""
    proc = subprocess.Popen(
        [bin_path, "app-server", "--listen", "stdio://"],
        cwd=str(real_wt), env={**os.environ, "CODEX_HOME": str(c_home)},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    assert proc.stdin is not None and proc.stdout is not None
    client = CodexAppServerClient(proc.stdin, proc.stdout)
    try:
        client.initialize()
        tid = client.open_thread(real_wt, persist_extended_history=True)
        if tid and name:
            try:
                client.set_thread_name(tid, name)
            except Exception:
                pass
        return tid
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def run_codex_headless_turn(
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
    with log_path.open("a", encoding="utf-8") as lf:
        proc = subprocess.Popen(
            cmd, cwd=str(worktree), env={**os.environ, **env},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
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
            if proc.stderr is not None and (err_txt := proc.stderr.read()):
                lf.write(json.dumps({"event": "stderr", "text": err_txt}) + "\n")
        return int(rc)
