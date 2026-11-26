"""HTTP control-plane tests for `/v1/agents/*` and role-scoped `/v1/mcp/*` on the opsb daemon."""
from __future__ import annotations

import http.client
import json
import os
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

from opsb_core.agents.adapters import BaseWorkerAdapter  # noqa: E402
from opsb_core.agents.manager import AgentMeshManager  # noqa: E402
from opsb_core.daemon.npu_stream import NpuStreamManager  # noqa: E402
from opsb_core.daemon.server import OpsbDaemonRuntime, _ThreadingHTTPServer, _make_handler  # noqa: E402
from opsb_core.drivers.registry import select_active_driver  # noqa: E402
from opsb_core.mcp.handlers_agents import bind_daemon_agent_manager  # noqa: E402


class _FakeAdapter(BaseWorkerAdapter):
    """Deterministic single-turn adapter so the HTTP layer can be exercised without real Harnesses."""

    def __init__(self, workspace: Path, worktree: Path, driver_kind: str, agent_id: str, log_path: Path) -> None:
        super().__init__(workspace, worktree, driver_kind, agent_id, log_path)
        self.turns: list[str] = []
        self.stopped = False

    def send_turn(self, prompt: str, on_event=None) -> str:
        self.turns.append(prompt)
        if callable(on_event):
            on_event("tool", "opsb_test")
        time.sleep(0.02)
        return f"[{self.driver_kind}] completed turn #{len(self.turns)}"

    def interrupt(self) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True


class AgentMeshHttpControlPlaneTests(unittest.TestCase):
    def _post(self, conn: http.client.HTTPConnection, path: str, payload: dict, headers: dict) -> tuple[int, dict]:
        raw = json.dumps(payload).encode()
        conn.request("POST", path, body=raw, headers={**headers, "Content-Length": str(len(raw))})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode() or "{}")
        return resp.status, body

    def test_agent_routes_and_role_scoped_mcp_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ops-bench"
            ws.mkdir()
            with mock.patch.dict(
                os.environ,
                {"PARHELION_HOME": str(root / "phome"), "PARHELION_DISABLE_DAEMON_PROXY": "1"},
                clear=False,
            ), mock.patch.object(NpuStreamManager, "start", return_value=None), mock.patch.object(
                NpuStreamManager, "stop", return_value=None
            ):
                select_active_driver("codex")
                runtime = OpsbDaemonRuntime(workspace=ws, ssh_host="npu", port=0, token="mesh-token")
                mesh = AgentMeshManager(adapter_factory=_FakeAdapter)
                runtime.agent_mesh = mesh
                bind_daemon_agent_manager(mesh)

                stop_ev = threading.Event()
                server = _ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(runtime, stop_ev))
                port = int(server.server_address[1])
                runtime.port = port
                threading.Thread(target=server.serve_forever, daemon=True).start()
                headers = {"X-Opsb-Token": "mesh-token"}
                try:
                    # 1. Bearer token still enforced on the new agent control plane.
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    conn.request("GET", "/v1/agents/pool")
                    self.assertEqual(conn.getresponse().status, 401)
                    conn.close()

                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)

                    # 2. Harness pool excludes the active orchestrator from eligible drivers.
                    conn.request("GET", "/v1/agents/pool", headers=headers)
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, 200)
                    pool = json.loads(resp.read().decode())
                    self.assertEqual(pool["orchestrator"]["kind"], "codex")
                    self.assertNotIn("codex", {d["kind"] for d in pool["eligible_external_drivers"]})

                    # 3. Self-dispatch is rejected with 400 (ValueError -> 400 mapping).
                    status, body = self._post(
                        conn,
                        "/v1/agents/spawn",
                        {"driver": "codex", "task": "recurse into myself", "worktree_mode": "readonly"},
                        headers,
                    )
                    self.assertEqual(status, 400)
                    self.assertIn("Self-dispatch blocked", body["error"])

                    # 4. Spawn an external worker and observe it through the message bus.
                    status, spawned = self._post(
                        conn,
                        "/v1/agents/spawn",
                        {
                            "driver": "deepseek",
                            "task": "audit PadV3Grad L1I packing",
                            "role_label": "audit",
                            "problem": "PadV3Grad",
                            "collab_mode": "orthogonal",
                            "axis": "L1I-pack",
                            "worktree_mode": "readonly",
                        },
                        headers,
                    )
                    self.assertEqual(status, 200)
                    self.assertTrue(spawned["ok"])
                    aid = spawned["agent"]["agent_id"]

                    conn.request("GET", f"/v1/agents/{aid}/messages", headers=headers)
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, 200)
                    msgs = json.loads(resp.read().decode())["messages"]
                    self.assertTrue(any("audit PadV3Grad" in m["body"] for m in msgs))

                    # 5. Orchestrator -> worker send and worker-scoped wait both work over HTTP.
                    status, sent = self._post(
                        conn, f"/v1/agents/{aid}/send", {"message": "switch to Arena axis", "delivery": "queue"}, headers
                    )
                    self.assertEqual(status, 200)
                    self.assertTrue(sent["ok"])

                    status, waited = self._post(
                        conn,
                        "/v1/agents/wait",
                        {"since_seq": 0, "agent_ids": [aid], "timeout_sec": 3.0, "strategic_only": False},
                        headers,
                    )
                    self.assertEqual(status, 200)
                    self.assertFalse(waited["timed_out"])

                    # 6. Unknown agent maps KeyError -> 404 instead of a 500.
                    status, missing = self._post(conn, "/v1/agents/nope-404/send", {"message": "hi"}, headers)
                    self.assertEqual(status, 404)
                    self.assertIn("Unknown agent_id", missing["error"])

                    # 7. Role-scoped MCP includes peer communication but excludes orchestration.
                    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                    status, worker_list = self._post(conn, f"/v1/mcp/worker/{aid}", rpc, headers)
                    self.assertEqual(status, 200)
                    worker_tools = {t["name"] for t in worker_list["result"]["tools"]}
                    self.assertIn("parhelion_squad", worker_tools)
                    self.assertNotIn("parhelion_harness_pool", worker_tools)
                    self.assertIn("parhelion_agent_inbox", worker_tools)

                    # 8. Default endpoint (/v1/mcp) is the orchestrator surface with 20 tools.
                    conn.request(
                        "POST",
                        "/v1/mcp",
                        body=json.dumps(rpc).encode(),
                        headers={**headers, "Content-Type": "application/json", "Content-Length": str(len(json.dumps(rpc)))},
                    )
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, 200)
                    orch_tools = {t["name"] for t in json.loads(resp.read().decode())["result"]["tools"]}
                    self.assertEqual(len(orch_tools), 20)
                    self.assertIn("parhelion_harness_pool", orch_tools)

                    # 9. Stop is idempotent enough to report a terminal record over HTTP.
                    status, stopped = self._post(conn, f"/v1/agents/{aid}/stop", {}, headers)
                    self.assertEqual(status, 200)
                    self.assertEqual(stopped["agent"]["status"], "stopped")
                    conn.close()
                finally:
                    stop_ev.set()
                    runtime.notify_subscribers()
                    mesh.stop_all()
                    bind_daemon_agent_manager(None)
                    server.shutdown()
                    server.server_close()


if __name__ == "__main__":
    unittest.main()
