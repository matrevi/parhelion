"""Unit tests for Multi-Harness Agent Mesh (AgentBus, Role-Scoped MCP, Deadlock Fixes, Daemon Routes)."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import tomllib
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
    def test_error_reaps_native_group_and_drops_queued_turns(self):
        from opsb_core.agents.bus_store import AgentBusStore
        from opsb_core.agents.manager import _WorkerRunner
        from opsb_core.agents.ownership import owned_groups
        ready, fail = threading.Event(), threading.Event()
        class FailingAdapter(BaseWorkerAdapter):
            def send_turn(self, prompt, on_event=None):
                self.child = self._spawn_proc([sys.executable, "-c", "import time; time.sleep(60)"], {})
                ready.set()
                fail.wait(3)
                raise RuntimeError("provider balance exhausted")
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "opsb_core.agents.bus_store.agent_bus_db_path", return_value=Path(tmp) / "test.db"
        ), mock.patch("opsb_core.agents.adapters.prepare_worker_command", side_effect=lambda cmd, *_: cmd):
            ws = Path(tmp)
            aid = "test-seat-" + str(os.getpid())
            store = AgentBusStore(ws)
            store.create_agent({"agent_id": aid, "driver": "deepseek"})
            adapter = FailingAdapter(ws, ws, "deepseek", aid, ws / "log")
            runner = _WorkerRunner(aid, store, adapter, lambda: None)
            try:
                runner.enqueue("work")
                self.assertTrue(ready.wait(5))
                self.assertEqual(store.get_agent(aid)["pid"], adapter.child.pid)
                self.assertEqual(owned_groups({aid}).get(aid), {adapter.child.pid})
                runner.enqueue("already queued")
                fail.set()
                runner._thread.join(5)
                rec = store.get_agent(aid)
                self.assertEqual((rec["status"], rec["pid"], rec["turn_count"]), ("error", None, 1))
                self.assertIsNotNone(adapter.child.poll())
            finally:
                fail.set()
                runner.stop()

    def test_codex_worker_provider_and_private_mcp_config(self):
        from opsb_core.drivers.codex_appserver import render_codex_config_toml
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "config.toml"
            config.write_text('model_provider="local"\n[model_providers.local]\n'
                              'base_url="http://localhost:1234/v1"\nrequires_openai_auth=true\n'
                              '[mcp_servers.unrelated]\ncommand="unused"\n')
            parsed = tomllib.loads(render_codex_config_toml(root, role="worker", host_config=config))
            self.assertEqual(parsed["model_providers"][parsed["model_provider"]]["base_url"], "http://localhost:1234/v1")
            self.assertEqual(set(parsed["mcp_servers"]), {"parhelion"})
            self.assertIn("parhelion_agent_report", parsed["mcp_servers"]["parhelion"]["tools"])
            worker_tools = parsed["mcp_servers"]["parhelion"]["tools"]
            self.assertEqual(worker_tools["parhelion_submit_gate"]["approval_mode"], "approve")
            self.assertNotIn("parhelion_agent_spawn", worker_tools)

    def test_compaction_completed_before_ack(self):
        events = [
            {"method": "turn/completed", "params": {"threadId": "mine", "turn": {"status": "completed"}}},
            {"id": 1, "result": {}},
        ]
        client = CodexAppServerClient(io.StringIO(), io.StringIO("".join(json.dumps(e) + "\n" for e in events)))
        client.compact("mine")

    def test_codex_failed_completion_surfaces_http_error(self):
        events = [
            {"id": 1, "result": {"turn": {"id": "t"}}},
            {"method": "turn/completed", "params": {"turn": {"id": "t", "status": "failed",
             "error": {"message": "private credential in upstream response",
                       "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 401}}}}}},
        ]
        client = CodexAppServerClient(io.StringIO(), io.StringIO("".join(json.dumps(e) + "\n" for e in events)))
        with self.assertRaisesRegex(RuntimeError, "HTTP 401 Unauthorized") as error:
            client.start_turn("thread", "test")
        self.assertNotIn("private credential", str(error.exception))

    def test_compaction_uses_token_threshold_or_high_percent_without_stage_result(self):
        from opsb_core.agents.adapters import CodexWorkerAdapter
        from opsb_core.agents.manager import _WorkerRunner
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        # (used tokens, window, stage report, expected compaction)
        for used, window, report, expected in [(290_000, 1_000_000, True, False), (300_000, 1_000_000, False, True),
                                               (69, 100, True, False), (70, 100, False, True)]:
            store = mock.Mock()
            store.db_path = Path(state.name) / "bus.db"
            store.get_agent.return_value = {}
            adapter = mock.Mock()
            adapter.worktree = Path(__file__).parent
            completed = threading.Event()
            runner = _WorkerRunner("worker", store, adapter, lambda: None)
            def turn(prompt, on_event):
                CodexWorkerAdapter._forward_event({"method": "thread/tokenUsage/updated", "params": {
                    "tokenUsage": {"last": {"totalTokens": used}, "total": {"totalTokens": 9999},
                                   "modelContextWindow": window}}}, on_event)
                if report:
                    runner._stage_result = "Verified SHA and next experiment"
                return "done"
            adapter.send_turn.side_effect = turn
            adapter.compact.return_value = "compacted"
            def notify():
                if store.append_message.called and (not expected or adapter.compact.called):
                    completed.set()
            runner._on_notify = notify
            try:
                with mock.patch("opsb_core.config.ConfigRepository.load", return_value={}):
                    runner.enqueue("work")
                    self.assertTrue(completed.wait(3))
                self.assertEqual(adapter.compact.called, expected)
                if expected:
                    self.assertIn("STATE", adapter.compact.call_args.args[0])
                    self.assertEqual(runner._context_tokens, 0)
            finally:
                runner.stop()
                runner._thread.join(2)

    def test_emit_context_reports_percent_tokens_and_window(self):
        from opsb_core.agents.adapters import emit_context
        events = []
        emit_context(250_000, 1_000_000, lambda kind, label: events.append((kind, label)))
        emit_context(None, 1_000_000, lambda kind, label: events.append((kind, label)))
        self.assertEqual(events, [("context", "25"), ("context_tokens", "250000"), ("context_window", "1000000")])

    def test_wait_finds_target_after_unrelated_message_backlog(self):
        from opsb_core.agents.bus_store import AgentBusStore
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            mgr = AgentMeshManager()
            store = AgentBusStore(ws / "bus.db")
            with mock.patch.object(mgr, "_store", return_value=store):
                for i in range(85):
                    store.append_message("unrelated", "worker_to_orch", "question", str(i))
                store.append_message("owner", "worker_to_orch", "result", "normal turn")
                msg = store.append_message("owner", "worker_to_orch", "question", "needs decision")
                reply = mgr.wait_events(ws, agent_ids=["owner"], timeout_sec=0.5)
                self.assertEqual([m["seq"] for m in reply["messages"]], [msg["seq"]])

    def test_paid_stagnation_counts_unique_terminal_receipts(self):
        from opsb_core.agents.judge_feedback import judge_feedback
        def receipt(i, score):
            return dict(submission_id=str(i), submitted_at=f"2026-11-01T00:00:{i:02}Z", status="Pass", score=score)
        rows = [receipt(1, 40)] + [receipt(i, 41) for i in range(2, 8)]
        rows.extend([rows[-1], dict(receipt(8, 0), status="Running")])
        events = [(r["submission_id"], k) for r, kinds in judge_feedback(rows, 5, 1)
                  for k, _ in kinds]
        self.assertEqual(events, [("1", "breakthrough"), ("7", "stagnation")])

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
        self.assertEqual(cx_sent[0]["params"]["sandboxPolicy"], {"type": "workspaceWrite"})
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
                    waited = call_tool("parhelion_agent_wait", {"since_seq": 0, "agent_ids": [aid], "strategic_only": False, "timeout_sec": 3.0}, ws, role="orchestrator")
                    self.assertFalse(waited["timed_out"])
                    self.assertGreaterEqual(len(waited["messages"]), 1)
                    self.assertIn("completed turn #1", waited["messages"][-1]["body"])
                    self.assertTrue(any("PadV3Grad" in d for d in waited["squad_digest"]))

                    # Local iterations do not count as paid judge receipts.
                    rep = call_tool(
                        "parhelion_agent_report",
                        {"kind": "progress", "no_gain_streak": 6, "summary": "6 tries stuck at +0.1"},
                        ws,
                        role="worker",
                        agent_id=aid,
                    )
                    self.assertTrue(rep["ok"])
                    self.assertEqual(rep["kind"], "progress")
                    self.assertEqual(rep["priority"], "normal")

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
                    w2 = call_tool("parhelion_agent_wait", {"since_seq": adm["message"]["seq"], "agent_ids": [aid], "strategic_only": False, "timeout_sec": 3.0}, ws, role="orchestrator")
                    self.assertFalse(w2["timed_out"])
                    self.assertIn("completed turn #2", w2["messages"][-1]["body"])

                    # 6. Hot mesh snapshot contains squads, strategic_alerts, orchestrator, ready external drivers
                    hot = mgr.build_hot_mesh(ws)
                    self.assertEqual(hot["orchestrator"]["driver"], "codex")
                    self.assertNotIn("codex", {d["kind"] for d in hot["ready_drivers"]})
                    self.assertEqual(len(hot["squads"]), 1)
                    self.assertEqual(hot["squads"][0]["collab_mode"], "orthogonal")
                    self.assertFalse(any(a["kind"] == "stagnation" for a in hot["strategic_alerts"]))
                    self.assertTrue(any(a["kind"] == "directive" for a in hot["strategic_alerts"]))

                    # 7. Stop worker cleanly
                    stopped = call_tool("parhelion_agent_stop", {"agent_id": aid}, ws, role="orchestrator")
                    self.assertEqual(stopped["agent"]["status"], "stopped")
                finally:
                    mgr.stop_all()
                    bind_daemon_agent_manager(None)


if __name__ == "__main__":
    unittest.main()
