"""Persistent single-SSH NDJSON NPU telemetry stream manager for `opsb daemon`."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from typing import Any, Callable

from ..services.npu_service import deploy_probe, enrich_npu_payload, read_mac_npu_waiters

PROBE_VERSION_MARKER = "OPSB_STREAM_PROBE_V1"
RECONNECT_BACKOFFS = (1.0, 2.0, 5.0)


class NpuStreamManager:
    """Maintains one persistent SSH stdout NDJSON stream and auto-reconnects with backoff."""

    def __init__(
        self,
        host: str = "npu",
        interval: float = 1.0,
        on_frame: Callable[[], None] | None = None,
        problems_provider: Callable[[], dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        self._host = (host or "npu").strip() or "npu"
        self._interval = max(0.5, min(10.0, float(interval or 1.0)))
        self._on_frame = on_frame
        self._problems_provider = problems_provider

        self._lock = threading.Lock()
        self._latest_frame: dict[str, Any] | None = None
        self._stream_connected = False
        self._last_error: str | None = None
        self._last_frame_ts: float = 0.0
        self._probe_backend: str = "dcmi"
        self._dcmi_sample_ms: float = 0.0

        self._mac_waiters_cache: list[dict[str, Any]] = []
        self._mac_waiters_ts: float = 0.0

        self._proc: subprocess.Popen[str] | None = None
        self._stop_event = threading.Event()
        self._restart_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def host(self) -> str:
        with self._lock:
            return self._host

    @property
    def interval(self) -> float:
        with self._lock:
            return self._interval

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ssh_host": self._host,
                "interval": self._interval,
                "stream_connected": self._stream_connected,
                "probe_backend": self._probe_backend,
                "dcmi_sample_ms": self._dcmi_sample_ms,
                "last_frame_ts": self._last_frame_ts,
                "last_error": self._last_error,
            }

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="opsb-npu-ssh-stream")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._restart_event.set()
        self._terminate_active_proc()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def configure(self, host: str | None = None, interval: float | None = None) -> bool:
        changed = False
        with self._lock:
            if host:
                clean_h = host.strip()
                if clean_h and clean_h != self._host:
                    self._host = clean_h
                    self._latest_frame = None
                    self._stream_connected = False
                    changed = True
            if interval is not None and interval > 0:
                clamped = max(0.5, min(10.0, float(interval)))
                if abs(clamped - self._interval) >= 0.05:
                    self._interval = clamped
                    changed = True
        if changed:
            self._restart_event.set()
            self._terminate_active_proc()
        return changed

    def snapshot(self, compact_history: bool = False, history_tail: int = 2) -> dict[str, Any]:
        with self._lock:
            host = self._host
            frame = self._latest_frame
            last_err = self._last_error

        if frame is None:
            return {
                "online": False,
                "ssh_host": host,
                "error": last_err or "Connecting SSH NPU stream…",
                "die_locks": [],
                "tasks": [],
            }

        out = dict(frame)
        if compact_history:
            hist = out.get("history")
            if isinstance(hist, list) and len(hist) > history_tail:
                out["history"] = hist[-history_tail:]
        return out

    def _terminate_active_proc(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    return
                except Exception:
                    proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        proc.kill()
                    proc.wait(timeout=0.5)
        except Exception:
            pass

    def _get_mac_waiters_throttled(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if (now - self._mac_waiters_ts) >= 2.0:
            self._mac_waiters_cache = read_mac_npu_waiters()
            self._mac_waiters_ts = now
        return self._mac_waiters_cache

    def _run_loop(self) -> None:
        backoff_idx = 0
        while not self._stop_event.is_set():
            self._restart_event.clear()
            with self._lock:
                host = self._host
                interval = self._interval

            remote_cmd = (
                f"if grep -q {PROBE_VERSION_MARKER} ~/.opsb/npu_stream_probe.py 2>/dev/null; then "
                f"exec python3 -u ~/.opsb/npu_stream_probe.py --stream --interval {interval:.2f}; "
                f"else exit 42; fi"
            )
            cmd = [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=5",
                "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=3",
                host,
                remote_cmd,
            ]
            stderr_lines: list[str] = []
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    start_new_session=True,
                )
            except Exception as exc:
                with self._lock:
                    self._stream_connected = False
                    self._last_error = str(exc)[:220]
                self._wait_reconnect(RECONNECT_BACKOFFS[min(backoff_idx, len(RECONNECT_BACKOFFS) - 1)])
                backoff_idx += 1
                continue

            with self._lock:
                self._proc = proc

            def _drain_stderr() -> None:
                if proc.stderr is None:
                    return
                try:
                    for line in proc.stderr:
                        s = line.strip()
                        if s:
                            stderr_lines.append(s)
                            if len(stderr_lines) > 10:
                                del stderr_lines[:-10]
                except Exception:
                    pass

            err_th = threading.Thread(target=_drain_stderr, daemon=True, name="opsb-ssh-stderr")
            err_th.start()

            got_frame = False
            if proc.stdout is not None:
                try:
                    for raw_line in proc.stdout:
                        if self._stop_event.is_set() or self._restart_event.is_set():
                            break
                        s = raw_line.strip()
                        if not s or not s.startswith("{"):
                            continue
                        try:
                            frame = json.loads(s)
                        except Exception:
                            continue

                        probs_map = self._problems_provider() if self._problems_provider else None
                        waiters = self._get_mac_waiters_throttled()
                        enriched = enrich_npu_payload(frame, host, problems_by_title=probs_map, mac_waiters=waiters)
                        sample_ms = float(enriched.get("sample_ms") or enriched.get("probe_latency_ms") or 0.0)
                        enriched["rtt_ms"] = round(sample_ms, 2)
                        backend = str(enriched.get("probe_backend") or "dcmi")

                        with self._lock:
                            self._latest_frame = enriched
                            self._stream_connected = True
                            self._last_error = None
                            self._last_frame_ts = time.time()
                            self._probe_backend = backend
                            self._dcmi_sample_ms = sample_ms

                        got_frame = True
                        backoff_idx = 0
                        if self._on_frame is not None:
                            try:
                                self._on_frame()
                            except Exception:
                                pass
                except Exception as exc:
                    stderr_lines.append(str(exc)[:180])

            rc = proc.poll()
            self._terminate_active_proc()
            err_th.join(timeout=0.5)

            if self._stop_event.is_set():
                break
            if self._restart_event.is_set():
                backoff_idx = 0
                continue

            if rc == 42:
                dep = deploy_probe(host)
                if dep.get("ok"):
                    backoff_idx = 0
                    continue
                err_msg = str(dep.get("error") or "Failed to deploy remote stream probe")
            else:
                err_msg = " | ".join(stderr_lines[-2:]) if stderr_lines else f"SSH stream closed (rc={rc})"

            with self._lock:
                self._stream_connected = False
                self._last_error = err_msg[:220]
                if not got_frame and self._latest_frame is not None:
                    self._latest_frame = {**self._latest_frame, "online": False, "error": self._last_error}
            if self._on_frame is not None:
                try:
                    self._on_frame()
                except Exception:
                    pass

            wait_s = RECONNECT_BACKOFFS[min(backoff_idx, len(RECONNECT_BACKOFFS) - 1)]
            backoff_idx += 1
            self._wait_reconnect(wait_s)

    def _wait_reconnect(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop_event.is_set() and not self._restart_event.is_set():
            rem = deadline - time.monotonic()
            if rem <= 0:
                break
            self._restart_event.wait(timeout=min(0.25, rem))
