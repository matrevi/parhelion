"""4.4.6: a daemon shutdown leaves live seats resumable (error) even when their runner writes `stopped`.

In the 4.4.5 restart of the 2026-11-27 run all twelve live seats ended `stopped`: stopping a runner
mid-turn made its loop write `stopped`, and stop_all checked the status only after the stop.
"""
from __future__ import annotations

import sys
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.agents.bus_store import AgentBusStore  # noqa: E402
from opsb_core.agents.manager import DAEMON_STOPPED, AgentMeshManager  # noqa: E402


class _Runner:
    """Stands in for _WorkerRunner: stopping it mid-turn writes `stopped`, as the turn loop does."""

    def __init__(self, store, agent_id):
        self.store, self.agent_id = store, agent_id

    def stop(self):
        self.store.update_agent(self.agent_id, status="stopped")


def test_shutdown_keeps_live_seats_resumable_and_admin_stops_stopped(tmp_path, monkeypatch):
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "ph"))
    ws = tmp_path / "contest"
    ws.mkdir()
    store = AgentBusStore(ws)
    for aid, status in (("dee-live01", "working"), ("dee-idle01", "idle"), ("dee-admin1", "stopped")):
        store.create_agent({"agent_id": aid, "driver": "deepseek", "problem": "Op", "worktree": str(ws)})
        store.update_agent(aid, status=status)
    mgr = AgentMeshManager()
    mgr._runners = {aid: _Runner(store, aid) for aid in ("dee-live01", "dee-idle01", "dee-admin1")}
    mgr.stop_all()
    after = {aid: store.get_agent(aid) for aid in ("dee-live01", "dee-idle01", "dee-admin1")}
    assert after["dee-live01"]["status"] == "error" and after["dee-live01"]["error"] == DAEMON_STOPPED
    assert after["dee-idle01"]["status"] == "error"
    assert after["dee-admin1"]["status"] == "stopped"  # an administrator's stop is not undone
