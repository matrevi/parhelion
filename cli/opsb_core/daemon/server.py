"""Localhost-only authenticated HTTP/SSE daemon server for Parhelion (`opsb daemon`)."""
from __future__ import annotations

import argparse
import errno
import fcntl
import hmac
import http.server
import json
import os
import secrets
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..config import (
    CLI_DIR,
    DAEMON_LOCK_PATH,
    DAEMON_LOG_PATH,
    DAEMON_STATE_PATH,
    PARHELION_HOME,
    ConfigRepository,
    ensure_parhelion_migrated,
    resolve_python_exe,
    utc_now,
)
from ..agents.manager import AgentMeshManager
from ..mcp.handlers_agents import bind_daemon_agent_manager
from . import dispatch as _dispatch
from .agent_routes import handle_agent_get, handle_agent_post
from .hot_reload import daemon_reloader
from .npu_stream import NpuStreamManager
from .. import device_hosts
from .state_cache import WorkspaceStateCache
from ..services.submit_runner import spawn_jump_submit
from ..ipc_bridge import invoke_bridge
from ..worker_access import worker_token
from ..services import test_runs

DAEMON_CONFIG_DIR = PARHELION_HOME
DEFAULT_PORT = 18910


def _is_expected_disconnect(exc: BaseException | None) -> bool:
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)):
        return True
    if isinstance(exc, OSError):
        return exc.errno in (errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED, errno.ETIMEDOUT)
    return False


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        _, exc, _ = sys.exc_info()
        if _is_expected_disconnect(exc):
            return
        super().handle_error(request, client_address)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_daemon_state() -> dict[str, Any] | None:
    if not DAEMON_STATE_PATH.is_file():
        return None
    try:
        data = json.loads(DAEMON_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("port") and data.get("token"):
            return data
    except Exception:
        pass
    return None


def probe_daemon_health(state: dict[str, Any] | None = None, timeout: float = 1.2) -> dict[str, Any] | None:
    st = state or read_daemon_state()
    if not st:
        return None
    pid = int(st.get("pid") or 0)
    if pid and not _pid_alive(pid):
        return None
    port = int(st["port"])
    token = str(st["token"])
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/health",
        headers={"X-Opsb-Token": token},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                body = json.loads(resp.read().decode("utf-8"))
                if isinstance(body, dict) and body.get("ok"):
                    return body
    except Exception:
        return None
    return None


def request_daemon_reload(state: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
    """Ask the running daemon to hot-reload its scheduling modules (`opsb daemon reload`)."""
    st = state or read_daemon_state()
    if not st or not _pid_alive(int(st.get("pid") or 0)):
        return {"ok": False, "error": "daemon not running"}
    req = urllib.request.Request(
        f"http://127.0.0.1:{int(st['port'])}/v1/daemon/reload",
        data=b"{}", headers={"X-Opsb-Token": str(st["token"]), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"reload request failed: {exc}"}
    return body if isinstance(body, dict) else {"ok": False, "error": "unexpected reply"}


def _test_registry(ws: Path) -> list[dict[str, Any]]:
    """The hot frame's test registry rows; an unreadable registry never breaks the frame."""
    try:
        return test_runs.registry_view(ws)
    except Exception:
        return []


def _write_daemon_state(state: dict[str, Any]) -> None:
    DAEMON_CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = DAEMON_STATE_PATH.with_name(f".{DAEMON_STATE_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, DAEMON_STATE_PATH)
    finally:
        tmp.unlink(missing_ok=True)


def _remove_daemon_state(expected_pid: int | None = None) -> None:
    try:
        if expected_pid is not None:
            cur = read_daemon_state()
            if cur and int(cur.get("pid") or 0) != expected_pid:
                return
        DAEMON_STATE_PATH.unlink(missing_ok=True)
    except Exception:
        pass


class OpsbDaemonRuntime:
    """Shared runtime state injected into HTTP/SSE request handlers."""

    def __init__(self, workspace: Path, ssh_host: str, port: int, token: str) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.ssh_host = ssh_host
        self.port = port
        self.token = token
        self.started_monotonic = time.monotonic()
        self.started_at = utc_now().isoformat()

        self._sub_cond = threading.Condition()
        self._notify_seq = 0

        self.state_cache = WorkspaceStateCache(
            default_workspace=self.workspace,
            on_cold_updated=self.notify_subscribers,
        )
        self.stream_mgr = self._new_stream(self.ssh_host)
        # One telemetry stream per further device host in the pool (``npu_ssh_hosts``), reconciled
        # with the config on every tick, so adding or removing a host needs no restart.
        self.extra_streams: dict[str, NpuStreamManager] = {}
        self._hosts_lock = threading.Lock()
        self.agent_mesh = AgentMeshManager(on_change=self.notify_subscribers)
        bind_daemon_agent_manager(self.agent_mesh)
        # Stateless scheduling modules reload between dispatch ticks (see hot_reload.py).
        self.reloader = daemon_reloader()
        self._tick_lock = threading.Lock()

    def _new_stream(self, host: str) -> NpuStreamManager:
        return NpuStreamManager(
            host=host,
            interval=1.0,
            on_frame=self.notify_subscribers,
            problems_provider=lambda: self.state_cache.get_problems_by_title(self.workspace),
        )

    def sync_device_streams(self, cfg: dict[str, Any]) -> None:
        """Start a stream for each pool host besides the primary stream's; stop removed ones."""
        wanted = [h for h in device_hosts.device_hosts(cfg) if h != self.stream_mgr.host]
        with self._hosts_lock:
            gone = [h for h in self.extra_streams if h not in wanted]
            for host in wanted:
                if host not in self.extra_streams:
                    mgr = self._new_stream(host)
                    self.extra_streams[host] = mgr
                    mgr.start()
            stopped = [self.extra_streams.pop(h) for h in gone]
        for mgr in stopped:
            mgr.stop()

    def stop_device_streams(self) -> None:
        with self._hosts_lock:
            streams = list(self.extra_streams.values())
            self.extra_streams.clear()
        for mgr in streams:
            mgr.stop()

    def device_host_frames(self, *, full_history: bool) -> list[dict[str, Any]]:
        """``npu_hosts`` of a hot frame: every device host, the primary first.

        The primary entry carries no ``npu``; clients read it from the frame's top-level ``npu``.
        """
        def head(host: str, primary: bool, st: dict[str, Any]) -> dict[str, Any]:
            return {"host": host, "primary": primary, "stream_connected": bool(st.get("stream_connected")),
                    "probe_backend": st.get("probe_backend"), "dcmi_sample_ms": round(float(st.get("dcmi_sample_ms") or 0.0), 2),
                    "last_error": st.get("last_error")}

        out = [head(self.stream_mgr.host, True, self.stream_mgr.stats())]
        with self._hosts_lock:
            extras = list(self.extra_streams.items())
        for host, mgr in extras:
            out.append({**head(host, False, mgr.stats()),
                        "npu": mgr.snapshot(compact_history=not full_history, history_tail=5)})
        return out

    def dispatch_next(self, workspace: Path, snapshot: dict, backoff: dict) -> None:
        # Looked up on the module at call time, so a hot reload takes effect on the next tick.
        _dispatch.dispatch_next(self, workspace, snapshot, backoff, bridge=invoke_bridge,
                                launch=spawn_jump_submit, cli=CLI_DIR / "opsb")

    def reload_modules(self, *, changed_only: bool = False) -> dict[str, Any]:
        """Reload the scheduling modules between ticks; failures keep the previous code."""
        with self._tick_lock:
            result = self.reloader.poll() if changed_only else self.reloader.reload()
        for row in (result or {}).get("modules", []):
            state = f"reloaded ({row['rebound']} rebound)" if row["ok"] else f"FAILED, kept previous: {row['error']}"
            print(f"hot reload {row['module']}: {state}", file=sys.stderr, flush=True)
        if result and any(row["ok"] for row in result["modules"]):
            try:
                self.state_cache.invalidate(self.resolve_ws(ConfigRepository.load().get("active_workspace")))
            except Exception as exc:
                print(f"hot reload: state refresh failed: {exc}", file=sys.stderr, flush=True)
            self.notify_subscribers()
        return result or {"ok": True, "modules": []}

    def consume_queue(self, stop: threading.Event) -> None:
        backoff: dict = {}
        while not stop.wait(1):
            if os.environ.get("OPSB_DAEMON_HOT_RELOAD", "1") != "0":
                try:
                    self.reload_modules(changed_only=True)
                except Exception as exc:
                    print(f"hot reload check failed: {exc}", file=sys.stderr, flush=True)
            with self._tick_lock:
                try:
                    cfg = ConfigRepository.load()
                    try:
                        self.sync_device_streams(cfg)
                    except Exception as exc:
                        print(f"device stream sync failed: {exc}", file=sys.stderr, flush=True)
                    ws = Path(cfg.get("active_workspace") or self.workspace).expanduser().resolve()
                    if not (ws / "results").is_dir():
                        continue
                    self.agent_mesh.deliver_judgements(ws, float(cfg.get("scheduler", {}).get("breakthrough_threshold", 5.0)))
                    try:
                        self.agent_mesh.deliver_test_results(ws)
                    except Exception as exc:
                        print(f"test result delivery failed: {exc}", file=sys.stderr, flush=True)
                    try:
                        self.agent_mesh.guide_stuck_seats(ws)
                    except Exception as exc:
                        print(f"stuck guidance failed: {exc}", file=sys.stderr, flush=True)
                    if not cfg.get("scheduler", {}).get("auto_consumer_enabled"):
                        continue
                    _, _, data = self.state_cache.get_snapshot(ws)
                    backoff = {key: until for key, until in backoff.items() if until > time.monotonic()}
                    if not stop.is_set():
                        self.dispatch_next(ws, json.loads(data), backoff)
                except Exception as exc:
                    print(f"queue dispatch failed: {exc}", file=sys.stderr, flush=True)
                    stop.wait(4)

    def notify_subscribers(self) -> None:
        with self._sub_cond:
            self._notify_seq += 1
            self._sub_cond.notify_all()

    def wait_for_update(self, last_seq: int, timeout: float) -> int:
        with self._sub_cond:
            if self._notify_seq == last_seq:
                self._sub_cond.wait(timeout=max(0.1, timeout))
            return self._notify_seq

    def current_seq(self) -> int:
        with self._sub_cond:
            return self._notify_seq

    def uptime_sec(self) -> float:
        return round(time.monotonic() - self.started_monotonic, 1)

    def resolve_ws(self, ws_param: str | None) -> Path:
        if ws_param:
            return Path(ws_param).expanduser().resolve()
        return self.workspace


def _make_handler(runtime: OpsbDaemonRuntime, stop_event: threading.Event):
    class DaemonHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def handle(self) -> None:
            try:
                super().handle()
            except Exception as exc:
                if _is_expected_disconnect(exc):
                    self.close_connection = True
                    return
                raise

        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except Exception as exc:
                if _is_expected_disconnect(exc):
                    self.close_connection = True
                    return
                raise

        def finish(self) -> None:
            try:
                super().finish()
            except Exception as exc:
                if _is_expected_disconnect(exc):
                    return
                raise

        def _check_auth(self, scoped_token=None) -> bool:
            provided = self.headers.get("X-Opsb-Token") or ""
            if not provided or not (hmac.compare_digest(provided, runtime.token)
                                    or scoped_token and hmac.compare_digest(provided, scoped_token)):
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return False
            return True

        def _send_json(self, code: int, obj: Any, extra_headers: dict[str, str] | None = None) -> None:
            raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self._send_bytes(code, "application/json; charset=utf-8", raw, extra_headers=extra_headers)

        def _send_bytes(
            self,
            code: int,
            content_type: str,
            raw: bytes,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store" if code != 304 else "no-cache")
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            if raw:
                self.wfile.write(raw)

        def _parse_query(self) -> tuple[str, dict[str, str]]:
            parsed = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
            flat = {k: v[0] for k, v in qs.items() if v}
            return parsed.path, flat

        def do_GET(self) -> None:
            if not self._check_auth():
                return
            path, q = self._parse_query()
            ws = runtime.resolve_ws(q.get("workspace"))

            if path == "/v1/health":
                st = runtime.stream_mgr.stats()
                cold_etag, cold_ms = runtime.state_cache.get_cold_meta(ws)
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "pid": os.getpid(),
                        "port": runtime.port,
                        "uptime_sec": runtime.uptime_sec(),
                        "stream_connected": st["stream_connected"],
                        "probe_backend": st["probe_backend"],
                        "ssh_host": st["ssh_host"],
                        "workspace": str(ws),
                        "cold_etag": cold_etag,
                        "dcmi_sample_ms": round(float(st["dcmi_sample_ms"]), 2),
                        "cold_recompute_ms": cold_ms,
                        "hosts": [{k: h[k] for k in ("host", "primary", "stream_connected")}
                                  for h in runtime.device_host_frames(full_history=False)],
                    },
                )
                return

            if path == "/v1/hot":
                host_q = q.get("host")
                interval_q = float(q["interval"]) if q.get("interval") else None
                if host_q or interval_q:
                    runtime.stream_mgr.configure(host=host_q, interval=interval_q)
                full_hist = q.get("full") in ("1", "true", "yes")
                frame = runtime.state_cache.build_hot_frame(
                    workspace=ws,
                    npu_payload=runtime.stream_mgr.snapshot(compact_history=not full_hist, history_tail=5),
                    stream_stats=runtime.stream_mgr.stats(),
                    uptime_sec=runtime.uptime_sec(),
                )
                frame["npu_hosts"] = runtime.device_host_frames(full_history=full_hist)
                frame["agent_mesh"] = runtime.agent_mesh.build_hot_mesh(ws)
                frame["test_runs"] = _test_registry(ws)
                self._send_json(200, frame)
                return

            if path == "/v1/snapshot":
                inm = self.headers.get("If-None-Match")
                live_npu = runtime.stream_mgr.snapshot(compact_history=False)
                code, etag, raw_bytes = runtime.state_cache.get_snapshot(
                    ws, if_none_match=inm, live_npu=live_npu
                )
                if code == 304:
                    self.send_response(304)
                    self.send_header("ETag", f'"{etag}"')
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self._send_bytes(
                        200,
                        "application/json; charset=utf-8",
                        raw_bytes,
                        extra_headers={"ETag": f'"{etag}"', "Cache-Control": "no-cache"},
                    )
                return

            if path == "/v1/stream/hot":
                host_q = q.get("host")
                interval_s = float(q["interval"]) if q.get("interval") else 1.0
                interval_s = max(0.5, min(10.0, interval_s))
                runtime.stream_mgr.configure(host=host_q, interval=interval_s)

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()

                first_frame = True
                last_seq = runtime.current_seq()
                try:
                    while not stop_event.is_set():
                        frame = runtime.state_cache.build_hot_frame(
                            workspace=ws,
                            npu_payload=runtime.stream_mgr.snapshot(compact_history=not first_frame),
                            stream_stats=runtime.stream_mgr.stats(),
                            uptime_sec=runtime.uptime_sec(),
                        )
                        frame["npu_hosts"] = runtime.device_host_frames(full_history=first_frame)
                        frame["agent_mesh"] = runtime.agent_mesh.build_hot_mesh(ws)
                        frame["test_runs"] = _test_registry(ws)
                        first_frame = False
                        line = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
                        payload = f"event: hot\ndata: {line}\n\n".encode("utf-8")
                        self.wfile.write(payload)
                        self.wfile.flush()

                        last_seq = runtime.wait_for_update(last_seq, timeout=interval_s)
                except Exception as exc:
                    if _is_expected_disconnect(exc):
                        self.close_connection = True
                        return
                    raise
                return

            if path.startswith("/v1/test-runs/"):
                # On-demand detail of one registry row: the compact result and its <=2KB digest.
                request_id = path.rsplit("/", 1)[-1]
                try:
                    self._send_json(200, {"ok": True, "run": test_runs.public_view(test_runs.refresh(ws, request_id))})
                except (KeyError, ValueError, OSError) as exc:
                    self._send_json(404, {"ok": False, "error": str(exc)})
                return

            if agent_resp := handle_agent_get(path, q, ws, runtime):
                self._send_json(agent_resp[0], agent_resp[1])
                return

            self._send_json(404, {"ok": False, "error": f"not found: {path}"})

        def do_POST(self) -> None:
            path, q = self._parse_query()
            worker_id = path.removeprefix("/v1/mcp/worker/") if path.startswith("/v1/mcp/worker/") else None
            worker_ws = runtime.resolve_ws(q.get("workspace")) if worker_id else None
            if not self._check_auth(worker_token(runtime.token, worker_ws, worker_id) if worker_ws else None):
                return
            if worker_id:
                worker = runtime.agent_mesh._store(worker_ws).get_agent(worker_id)
                if not worker or worker["status"] in ("stopped", "error"):
                    self._send_json(403, {"ok": False, "error": "Worker seat is inactive"})
                    return
            length = int(self.headers.get("Content-Length") or "0")
            raw_body = self.rfile.read(min(length, 1024 * 1024)) if length > 0 else b""
            body_obj: dict[str, Any] = {}
            if raw_body:
                try:
                    parsed_body = json.loads(raw_body.decode("utf-8"))
                    if isinstance(parsed_body, dict):
                        body_obj = parsed_body
                except Exception:
                    body_obj = {}

            ws = worker_ws or runtime.resolve_ws(body_obj.get("workspace") or q.get("workspace"))

            if path in ("/v1/mcp", "/mcp", "/v1/mcp/orchestrator") or path.startswith("/v1/mcp/worker/"):
                from ..mcp.server import handle_mcp_http_request

                if path.startswith("/v1/mcp/worker/"):
                    mcp_role = "worker"
                    mcp_agent_id = urllib.parse.unquote(path[len("/v1/mcp/worker/"):]).strip("/") or None
                else:
                    mcp_role = (self.headers.get("X-Parhelion-Role") or q.get("role") or "orchestrator").strip().lower()
                    mcp_agent_id = (self.headers.get("X-Parhelion-Agent-Id") or q.get("agent_id") or "").strip() or None

                code, resp_obj = handle_mcp_http_request(raw_body, workspace=ws, role=mcp_role, agent_id=mcp_agent_id)
                if resp_obj is None:
                    self._send_bytes(code, "application/json; charset=utf-8", b"")
                else:
                    self._send_json(code, resp_obj)
                return

            if agent_resp := handle_agent_post(path, body_obj, ws, runtime):
                self._send_json(agent_resp[0], agent_resp[1])
                return

            if path.startswith("/v1/drivers/") and path.endswith("/resume"):
                # The administrator's explicit release of a provider-fault suspension.
                driver = urllib.parse.unquote(path[len("/v1/drivers/"):-len("/resume")]).strip("/")
                cleared = runtime.agent_mesh.clear_driver_suspension(ws, driver)
                self._send_json(200, {"ok": True, "driver": driver.lower(), "cleared": cleared})
                return

            if path == "/v1/daemon/reload":
                # Admin token only (worker tokens are scoped to /v1/mcp/worker/<id>).
                result = runtime.reload_modules()
                self._send_json(200, {**result, "status": runtime.reloader.status()})
                return

            if path == "/v1/invalidate":
                etag = runtime.state_cache.invalidate(ws)
                runtime.notify_subscribers()
                self._send_json(200, {"ok": True, "cold_etag": etag, "workspace": str(ws)})
                return

            if path == "/v1/configure":
                host = body_obj.get("host") or q.get("host")
                interval = body_obj.get("interval") or q.get("interval")
                runtime.stream_mgr.configure(
                    host=str(host) if host else None,
                    interval=float(interval) if interval is not None else None,
                )
                self._send_json(200, {"ok": True, **runtime.stream_mgr.stats()})
                return

            self._send_json(404, {"ok": False, "error": f"not found: {path}"})

    return DaemonHandler


def _bind_server(preferred_port: int, handler_cls: type[http.server.BaseHTTPRequestHandler]) -> tuple[_ThreadingHTTPServer, int]:
    candidates = list(range(preferred_port, preferred_port + 20)) + [0]
    last_exc: Exception | None = None
    for port in candidates:
        try:
            srv = _ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
            return srv, int(srv.server_address[1])
        except OSError as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"Failed to bind daemon socket on 127.0.0.1: {last_exc}")


def run_daemon_server(workspace: Path, ssh_host: str, preferred_port: int = DEFAULT_PORT) -> int:
    ensure_parhelion_migrated()
    DAEMON_CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    DAEMON_LOG_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    stop_event = threading.Event()
    runtime = OpsbDaemonRuntime(workspace=workspace, ssh_host=ssh_host, port=preferred_port, token=token)
    handler_cls = _make_handler(runtime, stop_event)
    server, actual_port = _bind_server(preferred_port, handler_cls)
    runtime.port = actual_port
    runtime.agent_mesh.recover_workers(workspace)

    # Warm cold cache before accepting traffic so the first snapshot & hot frame are ready immediately.
    runtime.state_cache.warm(workspace)
    runtime.stream_mgr.start()
    try:
        runtime.sync_device_streams(ConfigRepository.load())
    except Exception as exc:
        print(f"device stream start failed: {exc}", file=sys.stderr, flush=True)
    consumer = threading.Thread(target=runtime.consume_queue, args=(stop_event,), name="submission-consumer", daemon=True)
    consumer.start()

    state = {
        "pid": os.getpid(),
        "port": actual_port,
        "token": token,
        "started_at": runtime.started_at,
        "workspace": str(runtime.workspace),
        "ssh_host": ssh_host,
        "version": 1,
    }
    _write_daemon_state(state)

    def _on_sig(_sig: int, _frame: Any) -> None:
        stop_event.set()
        runtime.notify_subscribers()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _on_sig)
    signal.signal(signal.SIGINT, _on_sig)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop_event.set()
        runtime.notify_subscribers()
        runtime.agent_mesh.stop_all()
        bind_daemon_agent_manager(None)
        runtime.stream_mgr.stop()
        runtime.stop_device_streams()
        server.server_close()
        _remove_daemon_state(expected_pid=os.getpid())
    return 0


def start_daemon(
    *,
    workspace: Path,
    ssh_host: str,
    port: int = DEFAULT_PORT,
    background: bool = False,
) -> dict[str, Any]:
    ensure_parhelion_migrated()
    DAEMON_CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    DAEMON_LOG_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with DAEMON_LOCK_PATH.open("a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            existing = read_daemon_state()
            health = probe_daemon_health(existing, timeout=1.0)
            if existing and health:
                return {
                    "ok": True,
                    "already_running": True,
                    "pid": int(existing["pid"]),
                    "port": int(existing["port"]),
                    "health": health,
                }

            if not background:
                raise RuntimeError("foreground mode handled directly")

            py_exe = resolve_python_exe(workspace)
            cmd = [
                py_exe,
                str(CLI_DIR / "opsb"),
                "daemon",
                "start",
                "--workspace",
                str(workspace),
                "--host",
                ssh_host,
                "--port",
                str(port),
            ]
            child_env = {**os.environ, "OPSB_DAEMON_LOCKED_PARENT": "1"}
            with DAEMON_LOG_PATH.open("a", encoding="utf-8") as log_fh:
                proc = subprocess.Popen(
                    cmd,
                    env=child_env,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                )

            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                st = read_daemon_state()
                if st and int(st.get("pid") or 0) == proc.pid:
                    hl = probe_daemon_health(st, timeout=0.8)
                    if hl:
                        return {
                            "ok": True,
                            "already_running": False,
                            "pid": proc.pid,
                            "port": int(st["port"]),
                            "health": hl,
                        }
                if proc.poll() is not None:
                    break
                time.sleep(0.08)

            return {"ok": False, "error": f"Daemon failed to become healthy within 6s (see {DAEMON_LOG_PATH})"}
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def stop_daemon() -> dict[str, Any]:
    st = read_daemon_state()
    if not st:
        return {"ok": True, "stopped": False, "reason": "not running"}
    pid = int(st.get("pid") or 0)
    if not _pid_alive(pid):
        _remove_daemon_state()
        return {"ok": True, "stopped": False, "reason": "stale state cleaned"}

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.05)

    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    _remove_daemon_state()
    return {"ok": True, "stopped": True, "pid": pid}


def get_daemon_status() -> dict[str, Any]:
    st = read_daemon_state()
    if not st:
        return {"ok": True, "running": False, "state_path": str(DAEMON_STATE_PATH)}
    health = probe_daemon_health(st, timeout=1.2)
    if not health:
        pid = int(st.get("pid") or 0)
        alive = _pid_alive(pid)
        return {
            "ok": True,
            "running": False,
            "pid": pid,
            "pid_alive": alive,
            "state_path": str(DAEMON_STATE_PATH),
        }
    return {
        "ok": True,
        "running": True,
        "pid": int(st["pid"]),
        "port": int(st["port"]),
        "started_at": st.get("started_at"),
        "state_path": str(DAEMON_STATE_PATH),
        "health": health,
    }


def run_daemon(args: argparse.Namespace) -> int:
    cfg = ConfigRepository.load()
    ws = ConfigRepository.resolve_workspace(getattr(args, "workspace", None), cfg)
    host = getattr(args, "host", None) or cfg.get("npu_ssh_host", "npu")
    port = int(getattr(args, "port", None) or DEFAULT_PORT)
    as_json = bool(getattr(args, "json", False))

    if args.daemon_cmd == "start":
        if getattr(args, "background", False):
            res = start_daemon(workspace=ws, ssh_host=host, port=port, background=True)
            print(json.dumps(res, ensure_ascii=False) if as_json else (f"Daemon running (PID {res.get('pid')}, port {res.get('port')})" if res.get("ok") else res.get("error")))
            return 0 if res.get("ok") else 1
        # Check under lock unless parent `start_daemon(--background)` already holds `daemon.lock`.
        if os.environ.get("OPSB_DAEMON_LOCKED_PARENT") != "1":
            with DAEMON_LOCK_PATH.open("a") as lf:
                DAEMON_CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
                fcntl.flock(lf, fcntl.LOCK_EX)
                try:
                    existing = read_daemon_state()
                    health = probe_daemon_health(existing, timeout=1.0)
                finally:
                    fcntl.flock(lf, fcntl.LOCK_UN)
                if existing and health:
                    out = {"ok": True, "already_running": True, "pid": existing["pid"], "port": existing["port"], "health": health}
                    print(json.dumps(out, ensure_ascii=False) if as_json else f"Daemon already running (PID {existing['pid']}, port {existing['port']})")
                    return 0
        return run_daemon_server(workspace=ws, ssh_host=host, preferred_port=port)

    if args.daemon_cmd == "reload":
        res = request_daemon_reload()
        if as_json:
            print(json.dumps(res, ensure_ascii=False))
        elif res.get("error"):
            print(f"Daemon reload: {res['error']}")
        else:
            for row in res.get("modules", []):
                state = "reloaded" if row.get("ok") else f"FAILED, previous code kept: {row.get('error')}"
                print(f"{row.get('module')}: {state}")
        return 0 if res.get("ok") else 1

    if args.daemon_cmd == "stop":
        res = stop_daemon()
        print(json.dumps(res, ensure_ascii=False) if as_json else f"Daemon stop: {res}")
        return 0 if res.get("ok") else 1

    if args.daemon_cmd == "status":
        res = get_daemon_status()
        if as_json:
            print(json.dumps(res, ensure_ascii=False))
        elif res.get("running"):
            hl = res.get("health") or {}
            print(
                f"Parhelion daemon RUNNING: PID {res['pid']} · 127.0.0.1:{res['port']} · "
                f"stream={hl.get('stream_connected')} ({hl.get('probe_backend')}) · "
                f"dcmi={hl.get('dcmi_sample_ms')}ms · etag={hl.get('cold_etag')}"
            )
        else:
            print("Parhelion daemon STOPPED")
        return 0
    return 1
