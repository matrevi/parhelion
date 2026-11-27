"""4.4.8: a provider outage recovers once its suspension is lifted; a failed resume hands the sprint over.

In the 2026-11-27 run the DeepSeek balance ran out at 03:49: the background orchestrator died on
Insufficient Balance without suspending the driver, lifting the suspension woke nothing, and the
relaunch with --resume fell back to a fresh session that knew nothing of the sprint.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.agents.bus_store import AgentBusStore  # noqa: E402
from opsb_core.agents.manager import SUSPENDED_ERROR, AgentMeshManager  # noqa: E402
from opsb_core.drivers import antigravity_acp, registry, session_store  # noqa: E402
from opsb_core.drivers.base import ORCHESTRATOR_PROBLEM, SessionRecord  # noqa: E402
from opsb_core.drivers.brief import orchestrator_handoff  # noqa: E402


def _ws(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "ph"))
    ws = tmp_path / "contest"
    ws.mkdir()
    return ws


def _orchestrator(ws: Path, sid: str, status: str, fault: str = "", updated: str = "2026-11-27T03:49:22") -> None:
    session_store.save_session(ws, SessionRecord(
        session_id=sid, driver="deepseek", workspace=str(ws), problem=ORCHESTRATOR_PROBLEM, worktree=str(ws),
        native_session_id=f"native-{sid}", status=status, pid=None, created_at=updated, updated_at=updated,
        brief_preview="", launch_mode="background", fault=fault))


def test_lifting_a_suspension_wakes_the_seats_the_outage_stopped(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    store = AgentBusStore(ws)
    for aid, status, error in (("dee-susp01", "error", SUSPENDED_ERROR),
                               ("dee-fault1", "error", "ACP session/prompt failed: Insufficient Balance"),
                               ("dee-admin1", "stopped", SUSPENDED_ERROR),
                               ("dee-crash1", "error", "Traceback: KeyError"),
                               ("gem-susp01", "error", SUSPENDED_ERROR)):
        store.create_agent({"agent_id": aid, "driver": "gemini" if aid.startswith("gem") else "deepseek",
                            "problem": "Op", "worktree": str(ws)})
        store.update_agent(aid, status=status, error=error)
    mgr = AgentMeshManager()
    mgr.suspend_driver(ws, "deepseek", "Insufficient Balance")
    sent: list[str] = []
    monkeypatch.setattr(mgr, "send_message", lambda _ws, aid, _msg, **_kw: sent.append(aid))
    monkeypatch.setattr(registry, "launch_problem_session", lambda *a, **k: {"session": {}})
    assert mgr.clear_driver_suspension(ws, "deepseek") is True
    assert sorted(sent) == ["dee-fault1", "dee-susp01"]  # never a stopped seat, a crash or another driver
    assert sorted(mgr.last_outage_resume["resumed_seats"]) == sorted(sent)


def test_a_failed_orchestrator_is_relaunched_once_with_resume(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    _orchestrator(ws, "deepseek-old", "failed", fault="provider")
    launches: list[dict] = []
    monkeypatch.setattr(registry, "launch_problem_session",
                        lambda w, p, **k: launches.append({"problem": p, **k}) or {"session": {"session_id": "deepseek-new"}})
    mgr = AgentMeshManager()
    for _ in range(2):
        mgr.suspend_driver(ws, "deepseek", "Insufficient Balance")
        mgr.clear_driver_suspension(ws, "deepseek")
    assert len(launches) == 1
    assert launches[0]["resume_id"] == "native-deepseek-old" and launches[0]["mode"] == "background"
    assert launches[0]["driver_kind"] == "deepseek" and "only the administrator ends it" in launches[0]["goal"]
    assert session_store.get_session(ws, "deepseek-old").fault == "provider-resumed"


def test_no_relaunch_while_an_orchestrator_runs_or_after_a_plain_failure(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    _orchestrator(ws, "deepseek-crash", "failed", fault="")
    launches: list = []
    monkeypatch.setattr(registry, "launch_problem_session", lambda *a, **k: launches.append(k) or {"session": {}})
    mgr = AgentMeshManager()
    mgr.suspend_driver(ws, "deepseek", "x")
    mgr.clear_driver_suspension(ws, "deepseek")
    _orchestrator(ws, "deepseek-old", "failed", fault="provider", updated="2026-11-27T03:50:00")
    _orchestrator(ws, "deepseek-live", "running", updated="2026-11-27T03:40:00")
    mgr.suspend_driver(ws, "deepseek", "x")
    mgr.clear_driver_suspension(ws, "deepseek")
    assert launches == []


def test_a_provider_fault_of_the_background_orchestrator_suspends_its_driver(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    _orchestrator(ws, "deepseek-old", "running")
    registry._record_failure(ws, "deepseek-old", "deepseek",
                             "ACP session/prompt failed: {'message': 'Insufficient Balance (request_id: 1)'}")
    rec = session_store.get_session(ws, "deepseek-old")
    assert rec.status == "failed" and rec.fault == "provider" and "Insufficient Balance" in rec.error
    assert AgentMeshManager().driver_suspended(ws, "deepseek")


class _FakeClient:
    prompts: list[str] = []

    def __init__(self, *_a, **_k):
        pass

    def initialize(self):
        pass

    def open_session(self, **_k):
        return "new-sid", False

    def send_prompt(self, _sid, text):
        _FakeClient.prompts.append(text)


class _FakeProc:
    def __init__(self, *_a, **_k):
        self.stdin, self.stdout, self.stderr = io.StringIO(), io.StringIO(), io.StringIO()

    def wait(self, timeout=None):
        return 0


def test_a_fresh_session_after_a_failed_load_gets_the_handoff(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    _orchestrator(ws, "deepseek-new", "running")
    monkeypatch.setattr(antigravity_acp, "AcpSessionClient", _FakeClient)
    monkeypatch.setattr(antigravity_acp.subprocess, "Popen", _FakeProc)
    monkeypatch.setattr(antigravity_acp, "build_acp_mcp_servers", lambda _ws: [])
    _FakeClient.prompts = []
    antigravity_acp.run_acp_headless_turn(ws, ws, "BRIEF", "deepseek-new", "native", "old-native", ["dsh"], {},
                                          tmp_path / "log.jsonl", handoff=lambda: "HANDOFF")
    assert _FakeClient.prompts == ["HANDOFF\n\nBRIEF"]
    assert session_store.get_session(ws, "deepseek-new").resume == "fresh"


def test_the_handoff_counts_seats_and_open_questions_without_bodies(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    store = AgentBusStore(ws)
    for aid, status in (("dee-a00001", "working"), ("dee-a00002", "idle"), ("dee-a00003", "stopped")):
        store.create_agent({"agent_id": aid, "driver": "deepseek", "problem": "OpA", "worktree": str(ws)})
        store.update_agent(aid, status=status)
    store.append_message("dee-a00001", "worker_to_orch", "question", "SECRET LEDGER TEXT", priority="strategic")
    text = orchestrator_handoff(ws)
    assert "could not be reloaded" in text and "OpA: 2 seats" in text
    assert "Unacknowledged worker questions: 1 (dee-a00001)" in text and "SECRET" not in text
    assert len(text.encode("utf-8")) <= 1200
