"""The app's test registry, on-demand run detail and driver-suspension release (4.1 route #4)."""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.agents.manager import SUSPENDED_ERROR, AgentMeshManager  # noqa: E402
from opsb_core.daemon.server import _ThreadingHTTPServer, _make_handler  # noqa: E402
from opsb_core.services import test_runs  # noqa: E402


def _record(ws: Path, rid: str, **fields) -> dict:
    rec = {"request_id": rid, "problem": "Op", "sha": "a" * 40, "level": "standard", "status": "finished",
           "created_at": "2026-11-26T08:00:00+00:00", "finished_at": "2026-11-26T08:01:00+00:00",
           "run_id": "r" * 32, "exit_code": 0, "digest": {"summary": {"total": 3, "passed": 3}},
           "progress": {"phase": "finish", "cases_done": 3, "selected": 3, "wait_sec": 0.5, "hold_sec": 4.0}}
    rec.update(fields)
    test_runs._write(ws, rec)
    return rec


class RegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)

    def test_rows_are_compact_newest_first_and_carry_their_own_timings(self) -> None:
        _record(self.ws, "1" * 32)
        events = self.ws / "run"
        events.mkdir()
        (events / "events.jsonl").write_text("\n".join(json.dumps(e) for e in (
            {"event": "launch"}, {"event": "device_batch", "wait_sec": 2.0, "hold_sec": 3.5}, {"event": "case"})))
        log = self.ws / "run.log"
        log.write_text("OPSB_RUN_STARTED " + json.dumps({"run_id": "s" * 32, "dir": str(events), "selected": 15}) + "\n")
        _record(self.ws, "2" * 32, status="running", created_at="2026-11-26T08:05:00+00:00", finished_at=None,
                digest=None, log=str(log), pid=None, progress=None, compare="b" * 40)
        rows = test_runs.registry_view(self.ws)
        self.assertEqual([r["request_id"] for r in rows], ["2" * 32, "1" * 32])
        running, done = rows
        self.assertEqual((running["cases_done"], running["cases"], running["wait_sec"], running["hold_sec"]),
                         (1, 15, 2.0, 3.5))
        self.assertEqual(running["compare"], "b" * 7)
        self.assertEqual(done["e2e_sec"], 60.0)
        self.assertTrue(done["has_digest"] and not running["has_digest"])
        self.assertNotIn("digest", done)  # digests are fetched on demand
        self.assertEqual(len(test_runs.registry_view(self.ws, limit=1)), 1)


class SuspensionRowsTest(unittest.TestCase):
    def test_rows_name_the_workers_the_fault_stopped(self) -> None:
        mgr = AgentMeshManager()
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            mgr._write_suspensions(ws, {"deepseek": {"since": "2026-11-26T04:00:00Z", "reason": "balance exhausted"}})
            workers = [{"agent_id": "w1", "driver": "deepseek", "error": SUSPENDED_ERROR},
                       {"agent_id": "w2", "driver": "deepseek", "error": ""},
                       {"agent_id": "w3", "driver": "codex", "error": SUSPENDED_ERROR}]
            rows = mgr.suspension_rows(ws, workers)
            self.assertEqual(rows, [{"driver": "deepseek", "since": "2026-11-26T04:00:00Z",
                                     "reason": "balance exhausted", "workers": ["w1"]}])
            self.assertTrue(mgr.clear_driver_suspension(ws, "DeepSeek"))
            self.assertEqual(mgr.suspension_rows(ws, workers), [])


class RouteTest(unittest.TestCase):
    def test_run_detail_and_resume_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            _record(ws, "3" * 32)
            cleared = []
            runtime = types.SimpleNamespace(
                token="admin", resolve_ws=lambda _ws: ws,
                agent_mesh=types.SimpleNamespace(clear_driver_suspension=lambda w, d: cleared.append(d) or True),
            )
            server = _ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(runtime, threading.Event()))
            port = int(server.server_address[1])
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                def call(method, path, token="admin"):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    conn.request(method, path, body=b"{}" if method == "POST" else None, headers={"X-Opsb-Token": token})
                    resp = conn.getresponse()
                    status, body = resp.status, json.loads(resp.read() or b"{}")
                    conn.close()
                    return status, body

                status, detail = call("GET", "/v1/test-runs/" + "3" * 32)
                self.assertEqual((status, detail["run"]["digest"]["summary"]["passed"]), (200, 3))
                self.assertEqual(call("GET", "/v1/test-runs/" + "9" * 32)[0], 404)
                self.assertEqual(call("POST", "/v1/drivers/deepseek/resume"),
                                 (200, {"ok": True, "driver": "deepseek", "cleared": True,
                                        "resumed_seats": [], "orchestrator": ""}))
                self.assertEqual(cleared, ["deepseek"])
                self.assertEqual(call("POST", "/v1/drivers/deepseek/resume", token="worker")[0], 401)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
