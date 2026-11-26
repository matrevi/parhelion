"""A full cleanup that empties the bus restarts its sequence; readers with an old cursor start over."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.agents.bus_store import AgentBusStore  # noqa: E402
from opsb_core.agents.manager import AgentMeshManager  # noqa: E402


def _agent(store: AgentBusStore, agent_id: str, **fields) -> None:
    store.create_agent({"agent_id": agent_id, "driver": "codex", "worktree": f"/tmp/{agent_id}", **fields})


class BusSequenceResetTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        self.store = AgentBusStore(self.ws)

    def _fill(self, agent_id: str, count: int) -> int:
        return max(self.store.append_message(agent_id, "worker_to_orch", "progress", str(i))["seq"]
                   for i in range(count))

    def test_emptied_bus_starts_again_at_one(self) -> None:
        _agent(self.store, "w1", status="stopped")
        self.store.append_message("admin", "system", "directive", "old directive")
        self.assertEqual(self._fill("w1", 3), 4)
        self.assertEqual(self.store.cleanup_stopped(), 1)
        self.assertEqual(self.store.latest_seq(), 0)
        self.assertEqual(self.store.append_message("admin", "system", "directive", "new")["seq"], 1)

    def test_counter_stays_while_messages_remain(self) -> None:
        _agent(self.store, "done", status="stopped")
        _agent(self.store, "live", status="running", pid=4242)
        self._fill("done", 2)
        self._fill("live", 1)
        last = self._fill("done", 1)
        self.store.cleanup_stopped()
        self.assertEqual([m["agent_id"] for m in self.store.list_messages()], ["live"])
        self.assertEqual(self.store.append_message("live", "worker_to_orch", "progress", "next")["seq"], last + 1)
        # A cursor past the surviving messages is still inside the counter: no replay.
        self.assertEqual(self.store.list_messages(since_seq=last + 1), [])

    def test_single_agent_cleanup_never_resets(self) -> None:
        _agent(self.store, "w1", status="stopped")
        last = self._fill("w1", 2)
        self.store.cleanup_stopped("w1")
        self.assertEqual(self.store.append_message("admin", "system", "directive", "x")["seq"], last + 1)

    def test_stale_cursor_reads_from_the_top_and_cannot_ack_new_history(self) -> None:
        _agent(self.store, "w1", status="stopped")
        stale = self._fill("w1", 5)
        self.store.cleanup_stopped()
        _agent(self.store, "w2", status="running", pid=1)
        self.store.append_message("w2", "orch_to_worker", "directive", "first after reset")
        self.store.append_message("w2", "orch_to_worker", "directive", "second")
        self.assertEqual(self.store.resolve_cursor(stale), 0)
        self.assertEqual(self.store.resolve_cursor(1), 1)
        self.assertEqual([m["seq"] for m in self.store.list_messages(since_seq=stale)], [1, 2])
        self.assertEqual(self.store.ack_messages("w2", stale, reader="worker"), 0)

        mgr = AgentMeshManager()
        with mock.patch.object(mgr, "_store", return_value=self.store):
            inbox = mgr.worker_inbox(self.ws, "w2", since_seq=stale)
            self.assertTrue(inbox["cursor_reset"])
            self.assertEqual([m["body"] for m in inbox["messages"]], ["first after reset", "second"])
            self.assertFalse(mgr.worker_inbox(self.ws, "w2", since_seq=2)["cursor_reset"])
            read = mgr.read_messages(self.ws, since_seq=stale, ack=False)
            self.assertTrue(read["cursor_reset"])
            self.assertEqual(len(read["messages"]), 2)

    def test_wait_events_moves_the_cursor_back_after_a_reset(self) -> None:
        _agent(self.store, "w1", status="stopped")
        stale = self._fill("w1", 4)
        self.store.cleanup_stopped()
        mgr = AgentMeshManager()
        with mock.patch.object(mgr, "_store", return_value=self.store):
            idle = mgr.wait_events(self.ws, since_seq=stale, timeout_sec=0.5)
            self.assertTrue(idle["timed_out"])
            self.assertEqual(idle["next_seq"], 0)
            msg = self.store.append_message("w9", "worker_to_orch", "question", "needs a decision")
            reply = mgr.wait_events(self.ws, since_seq=stale, timeout_sec=0.5)
            self.assertEqual([m["seq"] for m in reply["messages"]], [msg["seq"]])
            self.assertEqual(reply["next_seq"], msg["seq"])


if __name__ == "__main__":
    unittest.main()
