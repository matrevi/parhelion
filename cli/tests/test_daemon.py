"""Unit tests for `opsb_core.daemon` (state_cache, HTTP/SSE server, token auth, stream manager)."""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.daemon.npu_stream import NpuStreamManager  # noqa: E402
from opsb_core.daemon.server import OpsbDaemonRuntime, _ThreadingHTTPServer, _make_handler  # noqa: E402
from opsb_core.daemon.state_cache import WorkspaceStateCache  # noqa: E402


def _make_fake_workspace(root: Path) -> Path:
    ws = root / "ws"
    (ws / "results" / "scoring-evidence").mkdir(parents=True, exist_ok=True)
    (ws / "bench" / "DemoOp" / ".git" / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (ws / "bench" / "DemoOp" / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (ws / "bench" / "DemoOp" / ".git" / "refs" / "heads" / "main").write_text(
        "1234567890abcdef1234567890abcdef12345678\n", encoding="utf-8"
    )
    (ws / "bench" / "problems.json").write_text(
        json.dumps([{"title": "DemoOp", "slug": "demo_op", "problem_id": "p1", "path": "bench/DemoOp"}]),
        encoding="utf-8",
    )
    (ws / "results" / "score-snapshot.json").write_text(
        json.dumps({"DemoOp": {"score": 72.5, "status": "Pass", "rank": 3, "public_times_us": [2.0, 2.0, 2.0, 2.0, 2.0]}}),
        encoding="utf-8",
    )
    (ws / "results" / "submit-queue.jsonl").write_text("", encoding="utf-8")
    (ws / "results" / "submit-queue-outcome.jsonl").write_text("", encoding="utf-8")
    return ws


class TestDaemonStateAndHttp(unittest.TestCase):
    def test_state_cache_fingerprint_and_304(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _make_fake_workspace(Path(tmp))
            cache = WorkspaceStateCache(ws)
            with (
                mock.patch("opsb_core.daemon.state_cache.invoke_bridge", return_value=None),
                mock.patch("opsb_core.daemon.state_cache.probe_backfill_compact", return_value=None),
                mock.patch("opsb_core.daemon.state_cache.refresh_if_stale_async", return_value={"stale": False}),
                mock.patch("opsb_core.daemon.state_cache.get_auth_status", return_value={"judge": {"logged_in": False, "session_file": "", "base_url": ""}, "judge_accounts": [], "gitcode": {"logged_in": False, "cli_path": ""}}),
                mock.patch("opsb_core.daemon.state_cache.get_skills_status", return_value={"mode": "agents-md", "available_skills": [], "allowed_targets": [], "targets": {}}),
                mock.patch("opsb_core.services.ranking_service.team_ranking", return_value=None),
            ):
                cache.warm(ws)
                code1, etag1, raw1 = cache.get_snapshot(ws)
                self.assertEqual(code1, 200)
                self.assertTrue(etag1)
                snap = json.loads(raw1.decode("utf-8"))
                self.assertEqual(snap["contest"]["problem_count"], 1)

                # Matching ETag returns 304 with empty body
                code_hit, etag_hit, raw_hit = cache.get_snapshot(ws, if_none_match=f'"{etag1}"')
                self.assertEqual(code_hit, 304)
                self.assertEqual(etag_hit, etag1)
                self.assertEqual(raw_hit, b"")

                # Mutating score-snapshot.json invalidates fingerprint & produces new cold_etag
                time.sleep(0.01)
                (ws / "results" / "score-snapshot.json").write_text(
                    json.dumps({"DemoOp": {"score": 88.0, "status": "Pass", "rank": 1, "public_times_us": [1.5, 1.5, 1.5, 1.5, 1.5]}}),
                    encoding="utf-8",
                )
                ent = cache._entry(ws)
                with ent._lock:
                    ent._last_fp_check = 0.0
                ent.ensure_cold_fresh(synchronous=True)
                code2, etag2, raw2 = cache.get_snapshot(ws, if_none_match=etag1)
                self.assertEqual(code2, 200)
                self.assertNotEqual(etag2, etag1)
                self.assertEqual(json.loads(raw2.decode("utf-8"))["problems"][0]["current_score"], 88.0)

    def test_http_endpoints_auth_hot_304_and_sse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = _make_fake_workspace(Path(tmp))
            with (
                mock.patch("opsb_core.daemon.state_cache.invoke_bridge", return_value=None),
                mock.patch("opsb_core.daemon.state_cache.probe_backfill_compact", return_value=None),
                mock.patch("opsb_core.daemon.state_cache.refresh_if_stale_async", return_value={"stale": False}),
                mock.patch("opsb_core.daemon.state_cache.get_auth_status", return_value={"judge": {"logged_in": False, "session_file": "", "base_url": ""}, "judge_accounts": [], "gitcode": {"logged_in": False, "cli_path": ""}}),
                mock.patch("opsb_core.daemon.state_cache.get_skills_status", return_value={"mode": "agents-md", "available_skills": [], "allowed_targets": [], "targets": {}}),
                mock.patch("opsb_core.services.ranking_service.team_ranking", return_value=None),
                mock.patch.object(NpuStreamManager, "start", return_value=None),
                mock.patch.object(NpuStreamManager, "stop", return_value=None),
                mock.patch.object(
                    NpuStreamManager,
                    "snapshot",
                    return_value={
                        "online": True,
                        "ssh_host": "npu",
                        "probe_backend": "dcmi",
                        "sample_ms": 2.1,
                        "die_locks": [],
                        "tasks": [],
                        "history": [{"ts": 1000.0, "aicore_pct": 10.0, "power_w": 95.0, "temp_c": 35, "hbm_pct": 5.0, "cpu_pct": 12.0, "ram_pct": 20.0}],
                    },
                ),
                mock.patch.object(
                    NpuStreamManager,
                    "stats",
                    return_value={"ssh_host": "npu", "interval": 0.5, "stream_connected": True, "probe_backend": "dcmi", "dcmi_sample_ms": 2.1, "last_frame_ts": 1000.0, "last_error": None},
                ),
            ):
                runtime = OpsbDaemonRuntime(workspace=ws, ssh_host="npu", port=0, token="secret-test-token")
                runtime.state_cache.warm(ws)
                stop_ev = threading.Event()
                server = _ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(runtime, stop_ev))
                port = int(server.server_address[1])
                runtime.port = port
                th = threading.Thread(target=server.serve_forever, daemon=True)
                th.start()
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    # 1. Token enforcement
                    conn.request("GET", "/v1/health")
                    self.assertEqual(conn.getresponse().status, 401)
                    conn.close()

                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    headers = {"X-Opsb-Token": "secret-test-token"}
                    conn.request("GET", "/v1/health", headers=headers)
                    r = conn.getresponse()
                    self.assertEqual(r.status, 200)
                    hl = json.loads(r.read().decode())
                    self.assertTrue(hl["ok"])
                    self.assertTrue(hl["stream_connected"])

                    # 2. Hot payload
                    conn.request("GET", "/v1/hot", headers=headers)
                    r = conn.getresponse()
                    hot_raw = r.read()
                    self.assertEqual(r.status, 200)
                    self.assertLess(len(hot_raw), 8192)
                    hot = json.loads(hot_raw.decode())
                    self.assertEqual(hot["daemon_stats"]["probe_backend"], "dcmi")

                    # 3. Snapshot 200 + 304
                    conn.request("GET", "/v1/snapshot", headers=headers)
                    r = conn.getresponse()
                    r.read()
                    etag = r.getheader("ETag")
                    self.assertEqual(r.status, 200)
                    conn.request("GET", "/v1/snapshot", headers={**headers, "If-None-Match": etag})
                    r304 = conn.getresponse()
                    r304.read()
                    self.assertEqual(r304.status, 304)

                    # 4. Invalidate
                    conn.request("POST", "/v1/invalidate", body=b"{}", headers={**headers, "Content-Length": "2"})
                    r_inv = conn.getresponse()
                    self.assertEqual(r_inv.status, 200)
                    inv_body = json.loads(r_inv.read().decode())
                    self.assertTrue(inv_body["ok"])
                    conn.close()

                    # 5. SSE 3 frames
                    sse = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    sse.request("GET", "/v1/stream/hot?interval=0.5", headers=headers)
                    sse_resp = sse.getresponse()
                    self.assertEqual(sse_resp.status, 200)
                    seen = 0
                    while seen < 3:
                        line = sse_resp.fp.readline().decode("utf-8")
                        if line.startswith("data: "):
                            obj = json.loads(line[6:].strip())
                            self.assertIn("cold_etag", obj)
                            self.assertIn("npu", obj)
                            seen += 1
                    sse.close()
                finally:
                    stop_ev.set()
                    runtime.notify_subscribers()
                    server.shutdown()
                    server.server_close()

    def test_npu_stream_manager_parses_stream_and_configures(self) -> None:
        import io

        ndjson = "\n".join(
            [
                json.dumps({"probe_backend": "dcmi", "sample_ms": 2.4, "die_locks": [], "tasks": [], "history": [{"ts": 1.0}]}),
                json.dumps({"probe_backend": "dcmi", "sample_ms": 1.9, "die_locks": [], "tasks": [], "history": [{"ts": 2.0}]}),
            ]
        ) + "\n"

        class _FakeProc:
            def __init__(self) -> None:
                self.pid = 999999
                self.stdout = io.StringIO(ndjson)
                self.stderr = io.StringIO("")
                self._rc = 0

            def poll(self):
                return self._rc

            def wait(self, timeout=None):
                return self._rc

            def terminate(self):
                pass

            def kill(self):
                pass

        frames_notified = threading.Event()
        seen_count = [0]

        def _on_frame() -> None:
            seen_count[0] += 1
            if seen_count[0] >= 2:
                frames_notified.set()

        mgr = NpuStreamManager(host="npu", interval=1.0, on_frame=_on_frame)
        with (
            mock.patch("subprocess.Popen", return_value=_FakeProc()),
            mock.patch("opsb_core.daemon.npu_stream.read_mac_npu_waiters", return_value=[]),
            mock.patch("os.killpg", side_effect=ProcessLookupError()),
        ):
            mgr.start()
            self.assertTrue(frames_notified.wait(timeout=3.0))
            snap = mgr.snapshot(compact_history=False)
            self.assertTrue(snap["online"])
            self.assertEqual(snap["probe_backend"], "dcmi")
            self.assertEqual(snap["rtt_ms"], 1.9)
            self.assertTrue(mgr.configure(host="npu-alt", interval=0.5))
            self.assertEqual(mgr.host, "npu-alt")
            self.assertEqual(mgr.interval, 0.5)
            mgr.stop()


if __name__ == "__main__":
    unittest.main()
