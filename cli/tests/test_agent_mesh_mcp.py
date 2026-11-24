"""Unit tests for Multi-Harness Agent Mesh (AgentBus, Role-Scoped MCP, Deadlock Fixes, Daemon Routes)."""
from __future__ import annotations

import io
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
from opsb_core.drivers.antigravity_acp import AcpSessionClient  # noqa: E402
from opsb_core.drivers.codex_appserver import CodexAppServerClient  # noqa: E402
from opsb_core.drivers.registry import select_active_driver  # noqa: E402
from opsb_core.mcp.handlers_agents import bind_daemon_agent_manager  # noqa: E402
from opsb_core.mcp.server import handle_mcp_http_request  # noqa: E402
from opsb_core.mcp.tools import call_tool, list_tool_schemas  # noqa: E402


class _FakeAdapter(BaseWorkerAdapter):
    """In-memory multi-turn adapter for deterministic end-to-end mesh testing."""

    def __init__(self, workspace: Path, worktree: Path, driver_kind: str, agent_id: str, log_path: Path) -> None:
        super().__init__(workspace, worktree, driver_kind, agent_id, log_path)
        self.turns: list[str] = []
        self.interrupted = False
        self.stopped = False

    def send_turn(self, prompt: str, on_event=None) -> str:
        self.turns.append(prompt)
        if callable(on_event):
            on_event("tool", "opsb_test")
        time.sleep(0.03)
        return f"[{self.driver_kind}] completed turn #{len(self.turns)}: {prompt.splitlines()[-1][:60]}"

    def interrupt(self) -> None:
        self.interrupted = True

    def stop(self) -> None:
        self.stopped = True


class AgentMeshAndMcpTests(unittest.TestCase):
    def test_role_scoped_mcp_visibility_and_enforcement(self) -> None:
        orch_tools = {t["name"] for t in list_tool_schemas(role="orchestrator")}
        worker_tools = {t["name"] for t in list_tool_schemas(role="worker")}

        self.assertIn("parhelion_agent_spawn", orch_tools)
        self.assertIn("parhelion_agent_wait", orch_tools)
        self.assertIn("parhelion_submit_gate", orch_tools)
        self.assertNotIn("parhelion_agent_report", orch_tools)

        self.assertIn("parhelion_agent_report", worker_tools)
        self.assertIn("parhelion_agent_inbox", worker_tools)
        self.assertIn("parhelion_kb_query", worker_tools)
        self.assertIn("parhelion_submit_gate", worker_tools)
        self.assertNotIn("parhelion_agent_spawn", worker_tools)
        self.assertNotIn("parhelion_agent_stop", worker_tools)

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            code, resp = handle_mcp_http_request(
                json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "parhelion_agent_spawn", "arguments": {"driver": "claude", "task": "hi"}},
                }).encode("utf-8"),
                workspace=ws,
                role="worker",
                agent_id="cld-123456",
            )
            self.assertEqual(code, 200)
            self.assertIn("error", resp)
            self.assertEqual(resp["error"]["code"], -32601)

    def test_acp_and_codex_bidirectional_approval_no_deadlock(self) -> None:
        # ACP agent sends session/request_permission BEFORE replying to session/prompt
        acp_out = io.StringIO("\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 99, "method": "session/request_permission",
                        "params": {"options": [{"optionId": "allow_once", "kind": "allow"}]} }),
            json.dumps({"jsonrpc": "2.0", "method": "session/update",
                        "params": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "Fixed L1I."}}}}),
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"stopReason": "end_turn"}}),
        ]) + "\n")
        acp_in = io.StringIO()
        acp = AcpSessionClient(acp_in, acp_out, quiet_window_sec=0.0)
        res = acp.send_prompt("sess-1", "inspect elf")
        self.assertEqual(res["text"], "Fixed L1I.")
        sent = [json.loads(ln) for ln in acp_in.getvalue().splitlines()]
        perm_reply = next(m for m in sent if m.get("id") == 99)
        self.assertEqual(perm_reply["result"]["outcome"]["optionId"], "allow_once")

        # Codex server sends item/commandExecution/requestApproval BEFORE turn/completed
        cx_out = io.StringIO("\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"turn": {"id": "turn-1"}}}),
            json.dumps({"jsonrpc": "2.0", "id": 77, "method": "item/commandExecution/requestApproval", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "method": "item/agentMessage/delta", "params": {"delta": "Done!"}}),
            json.dumps({"jsonrpc": "2.0", "method": "turn/completed", "params": {}}),
        ]) + "\n")
        cx_in = io.StringIO()
        cx = CodexAppServerClient(cx_in, cx_out)
        cx_res = cx.start_turn("thr-1", "run bench", approval_policy="never", sandbox="workspace-write")
        self.assertEqual(cx_res["text"], "Done!")
        cx_sent = [json.loads(ln) for ln in cx_in.getvalue().splitlines()]
        appr_reply = next(m for m in cx_sent if m.get("id") == 77)
        self.assertEqual(appr_reply["result"]["decision"], "accept")


    def test_claude_dispatch_requires_admin_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = (root / "ops-bench").resolve()
            ws.mkdir()
            p_home = root / "phome"
            with mock.patch.dict(os.environ, {"PARHELION_HOME": str(p_home), "PARHELION_DISABLE_DAEMON_PROXY": "1"}, clear=False), \
                 mock.patch.dict(os.environ, {"PARHELION_ALLOW_CLAUDE_WORKERS": ""}, clear=False):
                select_active_driver("codex")
                mgr = AgentMeshManager(adapter_factory=_FakeAdapter)
                bind_daemon_agent_manager(mgr)
                try:
                    pool = mgr.get_harness_pool(ws)
                    self.assertNotIn("claude", {d["kind"] for d in pool["eligible_external_drivers"]})
                    self.assertFalse(pool["dispatch_policy"]["claude_workers_allowed"])
                    self.assertTrue(pool["dispatch_policy"]["prefer_native_subagents"])

                    with self.assertRaises(ValueError) as ctx:
                        mgr.spawn_agent(ws, driver="claude", task="audit PadV3Grad")
                    self.assertIn("Claude workers are disabled by policy", str(ctx.exception))

                    os.environ["PARHELION_ALLOW_CLAUDE_WORKERS"] = "1"
                    try:
                        spawned = mgr.spawn_agent(ws, driver="claude", task="audit PadV3Grad", worktree_mode="readonly")
                        self.assertTrue(spawned["ok"])
                        pool2 = mgr.get_harness_pool(ws)
                        self.assertIn("claude", {d["kind"] for d in pool2["eligible_external_drivers"]})
                        self.assertTrue(pool2["dispatch_policy"]["claude_workers_allowed"])
                    finally:
                        os.environ.pop("PARHELION_ALLOW_CLAUDE_WORKERS", None)
                finally:
                    mgr.stop_all()
                    bind_daemon_agent_manager(None)

    def test_end_to_end_spawn_blocks_self_dispatch_and_handles_messages_and_wait(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = (root / "ops-bench").resolve()
            ws.mkdir()
            p_home = root / "phome"
            with mock.patch.dict(os.environ, {"PARHELION_HOME": str(p_home), "PARHELION_DISABLE_DAEMON_PROXY": "1"}, clear=False):
                select_active_driver("codex")
                mgr = AgentMeshManager(adapter_factory=_FakeAdapter)
                bind_daemon_agent_manager(mgr)
                try:
                    # 1. Self-dispatch of active orchestrator ('codex') MUST be blocked
                    with self.assertRaises(ValueError) as ctx:
                        call_tool("parhelion_agent_spawn", {"driver": "codex", "task": "recurse"}, ws, role="orchestrator")
                    self.assertIn("Self-dispatch blocked", str(ctx.exception))

                    # 2. Spawn external 'deepseek' worker in orthogonal squad -> returns immediately & finishes turn
                    spawned = call_tool(
                        "parhelion_agent_spawn",
                        {
                            "driver": "deepseek", "role_label": "audit", "problem": "PadV3Grad",
                            "collab_mode": "orthogonal", "axis": "L1I-pack",
                            "task": "Check PadV3Grad ELF", "worktree_mode": "readonly",
                        },
                        ws,
                        role="orchestrator",
                    )
                    self.assertTrue(spawned["ok"])
                    aid = spawned["agent"]["agent_id"]

                    # 3. Wait wakes immediately when worker turn posts result
                    waited = call_tool("parhelion_agent_wait", {"since_seq": 0, "agent_ids": [aid], "timeout_sec": 3.0}, ws, role="orchestrator")
                    self.assertFalse(waited["timed_out"])
                    self.assertGreaterEqual(len(waited["messages"]), 1)
                    self.assertIn("completed turn #1", waited["messages"][-1]["body"])
                    self.assertTrue(any("PadV3Grad" in d for d in waited["squad_digest"]))

                    # 4. Worker reports 6-shot stagnation -> auto-escalates to strategic 'stagnation'
                    rep = call_tool(
                        "parhelion_agent_report",
                        {"kind": "progress", "no_gain_streak": 6, "summary": "6 tries stuck at +0.1"},
                        ws,
                        role="worker",
                        agent_id=aid,
                    )
                    self.assertTrue(rep["ok"])
                    self.assertEqual(rep["kind"], "stagnation")
                    self.assertEqual(rep["priority"], "strategic")

                    # 4b. Admin directive posts strategic directive
                    adm = mgr.post_admin_directive(ws, "Prioritize 16KB L1I hot packing", problem="PadV3Grad", broadcast_workers=False)
                    self.assertTrue(adm["ok"])
                    self.assertEqual(adm["message"]["priority"], "strategic")

                    # 5. Orchestrator sends follow-up with interrupt, then reads & acks messages
                    sent = call_tool(
                        "parhelion_agent_send",
                        {"agent_id": aid, "message": "Switch to Arena axis", "delivery": "interrupt"},
                        ws,
                        role="orchestrator",
                    )
                    self.assertTrue(sent["ok"])
                    w2 = call_tool("parhelion_agent_wait", {"since_seq": adm["message"]["seq"], "agent_ids": [aid], "timeout_sec": 3.0}, ws, role="orchestrator")
                    self.assertFalse(w2["timed_out"])
                    self.assertIn("completed turn #2", w2["messages"][-1]["body"])

                    # 6. Hot mesh snapshot contains squads, strategic_alerts, orchestrator, ready external drivers
                    hot = mgr.build_hot_mesh(ws)
                    self.assertEqual(hot["orchestrator"]["driver"], "codex")
                    self.assertNotIn("codex", {d["kind"] for d in hot["ready_drivers"]})
                    self.assertEqual(len(hot["squads"]), 1)
                    self.assertEqual(hot["squads"][0]["collab_mode"], "orthogonal")
                    self.assertTrue(any(a["kind"] == "stagnation" for a in hot["strategic_alerts"]))
                    self.assertTrue(any(a["kind"] == "directive" for a in hot["strategic_alerts"]))

                    # 7. Stop worker cleanly
                    stopped = call_tool("parhelion_agent_stop", {"agent_id": aid}, ws, role="orchestrator")
                    self.assertEqual(stopped["agent"]["status"], "stopped")
                finally:
                    mgr.stop_all()
                    bind_daemon_agent_manager(None)


if __name__ == "__main__":
    unittest.main()
