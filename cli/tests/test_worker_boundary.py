"""Worker boundary tests (P15, 4.2 K): detection of platform access and its one-shot alert."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core import boundary_scan, worker_boundary, worker_sandbox  # noqa: E402


def _ph() -> Path:
    return worker_sandbox.parhelion_home()


# 3. detection ----------------------------------------------------------------------------------

def _ph() -> Path:
    return worker_sandbox.parhelion_home()


def _acp(command: str, title: str = "bash") -> dict:
    return {"sessionUpdate": "tool_call", "title": title, "rawInput": {"command": command}}


@pytest.mark.parametrize("event, category", [
    (_acp('python3 -c "import opsb_core; print(opsb_core.__file__)"'), "platform_import"),
    (_acp("python3 - <<'EOF'\nimport sys; sys.path.insert(0, 'cli')\nimport opsb_core\nEOF"), "platform_import"),
    (_acp(f"ls {_ph()}/workspaces/ws-abc/state/test_runs"), "platform_state"),
    (_acp(f"sqlite3 {_ph()}/workspaces/ws-abc/state/agent_bus.db 'select 1'"), "platform_state"),
    (_acp(f"cat {_ph()}/cache/.cache_remote_judge_abc.json"), "judge_credentials"),
    (_acp(f"cat {_ph()}/cj_accounts.json"), "judge_credentials"),
    (_acp("cat ~/.judge/session.json"), "judge_credentials"),
    (_acp("python3 -c \"from opsb_core.services import board_refresh_service as b; "
          "print(b._all_team_sessions())\""), "team_sessions"),
])
def test_detector_hits_each_documented_shape(event, category):
    assert category in worker_boundary.classify_acp_update(event)


def test_detector_reads_the_dsh_native_event_shape(monkeypatch):
    event = {"type": "tool/call", "time": 1_700_000_000_000,
             "data": {"name": "bash", "arguments": json.dumps(
                 {"command": "python3 -c \"import opsb_core.services.board_refresh_service as b; "
                             "b._all_team_sessions()\""})}}
    assert worker_boundary.classify_dsh_event(event) == ["team_sessions", "platform_import"]
    read = {"type": "tool/call", "data": {"name": "read", "arguments": json.dumps({"file_path": "~/.judge/session.json"})}}
    assert worker_boundary.classify_dsh_event(read) == ["judge_credentials"]


def test_detector_does_not_flag_a_worktree_local_same_name_file(tmp_path):
    wt = tmp_path / "problem" / ".worktrees" / "sprint-solo"
    (wt / "state" / "test_runs").mkdir(parents=True)
    (wt / "state" / "test_runs" / "run-1.json").write_text("{}", encoding="utf-8")
    local = str(wt / "state" / "test_runs" / "run-1.json")
    assert worker_boundary.classify_acp_update(_acp(f"python3 -c \"import json; print(json.load(open('{local}')))\""),
                                              worktree=wt) == []
    assert worker_boundary.classify_tool("read", {"file_path": local}, worktree=wt) == []
    assert worker_boundary.classify_acp_update(_acp("cat state/test_runs/run-1.json"), worktree=wt) == []
    # the same *name* inside the worktree is fine even when a platform path is mentioned elsewhere
    assert "judge_credentials" not in worker_boundary.classify_tool(
        "write", {"file_path": str(wt / "notes" / "cj_accounts_notes.md")}, worktree=wt)
    # ... while the platform's own copy is not
    assert worker_boundary.classify_acp_update(_acp(f"cat {_ph()}/cj_accounts.json")) == ["judge_credentials"]


def test_detector_ignores_platform_tool_arguments(tmp_path):
    """A team message or a report that mentions a path is not a read of it."""
    message = f"see {_ph()}/workspaces/ws-abc/state/test_runs for the run"
    assert worker_boundary.classify_tool("parhelion_squad", {"message": message}) == []
    assert worker_boundary.classify_tool("parhelion_agent_report", {"summary": message}) == []


def test_scan_reads_a_mesh_log_and_a_dsh_session(tmp_path):
    head = _ph() / "workspaces" / "ws-abc" / "state" / "test_runs" / "run-1.json"
    log = tmp_path / "mesh-dee-abc123.jsonl"
    rows = [
        {"ts": "2026-11-26T04:20:00.100000+00:00", "msg": {"method": "session/new", "id": 1,
                                                           "params": {"cwd": str(tmp_path / "wt")}}},
        {"ts": "2026-11-26T04:20:00.200000+00:00", "msg": {"id": 1, "result": {"sessionId": "sess-1"}}},
        {"ts": "2026-11-26T04:20:01.000000+00:00", "msg": {"method": "session/prompt",
                                                           "params": {"sessionId": "sess-1", "prompt": "go"}}},
        {"ts": "2026-11-26T04:20:02.000000+00:00", "msg": {"method": "session/update", "params": {"sessionId": "sess-1",
            "update": {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "bash",
                       "rawInput": {"command": f"cat {head}"}}}}},
        {"ts": "2026-11-26T04:20:03.000000+00:00", "msg": {"id": 2, "result": {"stopReason": "end_turn"}}},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    hits = boundary_scan.scan(mesh_paths=[log], include_dsh=False)
    assert [(h["worker"], h["categories"]) for h in hits] == [("dee-abc123", ["platform_state"])]
    assert hits[0]["ts"] == "2026-11-26T04:20:02"


def test_scan_dsh_attributes_a_session_to_its_worker(tmp_path, monkeypatch):
    from opsb_core.metrics import usage

    dsh_home = tmp_path / "dsh"
    session_dir = dsh_home / "sessions" / "--project--" / "sess-1"
    session_dir.mkdir(parents=True)
    (session_dir / "session.v4.jsonl.zstd").write_bytes(b"fixture")
    events = [
        {"type": "session", "cwd": "/competition/Op/.worktrees/sprint-solo", "id": "sess-1"},
        {"type": "tool/call", "time": 1_764_128_402_000,
         "data": {"name": "bash", "arguments": json.dumps({"command": "cat ~/.judge/session.json"})}},
    ]
    raw = "\n".join(json.dumps(e) for e in events).encode()
    monkeypatch.setattr(usage, "decompress_zstd", lambda _b: raw)
    hits = boundary_scan.scan_dsh(since="2025-01-01", until="2026-12-31", dsh_home=dsh_home,
                                    agents_by_session={"sess-1": "dee-abc123"})
    assert [(h["worker"], h["categories"], h["source"]) for h in hits] == [
        ("dee-abc123", ["judge_credentials"], "dsh")]


# 4. alerting -----------------------------------------------------------------------------------

def _store_and_workspace(tmp_path):
    from opsb_core.agents.bus_store import AgentBusStore

    ws = tmp_path / "contest"
    ws.mkdir()
    store = AgentBusStore(ws)
    store.create_agent({"agent_id": "dee-abc123", "driver": "deepseek", "problem": "Op", "worktree": str(ws)})
    store.create_agent({"agent_id": "dee-clean1", "driver": "deepseek", "problem": "Op", "worktree": str(ws)})
    return store, ws


def test_same_worker_same_class_alerts_only_once(tmp_path):
    store, ws = _store_and_workspace(tmp_path)
    first = worker_boundary.report(store, ws, "dee-abc123", "judge_credentials")
    assert first["ok"] and first["categories"] == ["judge_credentials"]
    again = worker_boundary.report(store, ws, "dee-abc123", "judge_credentials")
    worker_boundary.report(store, ws, "dee-abc123", "team_sessions")
    assert again["alerts"] == [] and again["ledger"] == []

    messages = store.list_messages(limit=50)
    platform_rows = [m for m in messages if m["kind"] == "platform"]
    per_class = {}
    for row in platform_rows:
        per_class.setdefault((row["agent_id"], row["direction"]), []).append(row)
    assert len(per_class[("dee-abc123", "worker_to_orch")]) == 2  # one per class, to the orchestrator
    assert len(per_class[("admin", "system")]) == 2  # one per class, to the administrator
    assert all(m["priority"] == "strategic" for m in platform_rows)
    assert all("dee-abc123" in m["body"] for m in platform_rows)
    # the alert names the worker, the class and the time only — never the command or what it read
    for row in platform_rows:
        assert "ci_accounts" not in row["body"] and "cookie" not in row["body"].lower()

    ledger = ws / "results" / "ledger" / "Op" / "dee-abc123.jsonl"
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    constraints = [r for r in rows if r["type"] == "constraint"]
    assert len(constraints) == 2 and {r["claim"] for r in constraints} == {
        "worker boundary: judge_credentials", "worker boundary: team_sessions"}
    assert all(r["source"] == "platform" and r["confidence"] == "measured" for r in constraints)

    rec = store.get_agent("dee-abc123")
    assert worker_boundary.flags_of(rec) == ["team_sessions", "judge_credentials"]
    assert worker_boundary.flags_of(store.get_agent("dee-clean1")) == []


def test_report_marks_the_collaboration_page_row(tmp_path, monkeypatch):
    from opsb_core.agents.manager import AgentMeshManager

    ws = tmp_path / "contest"
    ws.mkdir()
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "home"))
    mgr = AgentMeshManager()
    mgr._cached_doctor = lambda: {"active_driver": "codex", "diagnostics": []}
    mgr._observed_driver_sessions = lambda w, d: ({}, [])
    store = mgr._store(ws)
    store.create_agent({"agent_id": "dee-abc123", "driver": "deepseek", "problem": "P1", "worktree": str(ws)})
    store.create_agent({"agent_id": "dee-clean1", "driver": "deepseek", "problem": "P1", "worktree": str(ws)})
    assert all(row["boundary_flags"] == [] for row in mgr.build_hot_mesh(ws)["workers"])

    worker_boundary.report(store, ws, "dee-abc123", "platform_state")
    rows = {row["agent_id"]: row for row in mgr.build_hot_mesh(ws)["workers"]}
    assert rows["dee-abc123"]["boundary_flags"] == ["platform_state"]
    assert rows["dee-clean1"]["boundary_flags"] == []


def test_live_hit_never_stops_the_worker(tmp_path):
    """A hit alerts and marks; the runner keeps the seat running for the administrator to decide."""
    from opsb_core.agents.adapters import BaseWorkerAdapter
    from opsb_core.agents.manager import _WorkerRunner

    ws = tmp_path / "contest"
    ws.mkdir()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PARHELION_HOME", str(tmp_path / "home"))
        from opsb_core.agents.bus_store import AgentBusStore

        store = AgentBusStore(ws)
        store.create_agent({"agent_id": "dee-abc123", "driver": "deepseek", "problem": "Op", "worktree": str(ws)})
        adapter = BaseWorkerAdapter(ws, ws, "deepseek", "dee-abc123", ws / "log.jsonl")
        runner = _WorkerRunner("dee-abc123", store, adapter, lambda: None)
        try:
            adapter._note_boundary(["team_sessions"])
            adapter._note_boundary(["team_sessions"])
            assert store.get_agent("dee-abc123")["status"] != "stopped"
            assert adapter._closed is False
            assert worker_boundary.flags_of(store.get_agent("dee-abc123")) == ["team_sessions"]
            assert len([m for m in store.list_messages(limit=20) if m["kind"] == "platform"]) == 2
        finally:
            runner.stop()


def test_own_spill_file_is_not_a_boundary_hit_but_another_seats_is(tmp_path):
    """4.4.4: the output bound hands a worker the path of its own spilled reply; reading it is legal."""
    ph = tmp_path / "ph"
    own = ph / "workspaces" / "contest-1" / "state" / "spill" / "dee-48be00" / "r-kb_query.json"
    other = ph / "workspaces" / "contest-1" / "state" / "spill" / "dee-000000" / "r.json"
    cmd = f"python3 -c \"import json; json.load(open('{own}'))\""
    assert worker_boundary.classify_text(cmd, parhelion=ph, agent_id="dee-48be00") == []
    assert worker_boundary.classify_text(cmd, parhelion=ph) == ["platform_state"]  # no identity: unchanged
    assert worker_boundary.classify_text(f"cat {other}", parhelion=ph, agent_id="dee-48be00") == ["platform_state"]
    assert worker_boundary.classify_text(f"ls {ph}/workspaces/contest-1/state/guidance", parhelion=ph,
                                         agent_id="dee-48be00") == ["platform_state"]
