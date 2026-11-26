"""P26 sprint seats: seat identity, race/pipeline expansion, per-problem modes, mid-sprint changes,
replacement idempotency and its start-up handover, recon/knowledge tool filtering and the remote
checkout root (4.3)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.agents.adapters import BaseWorkerAdapter  # noqa: E402
from opsb_core.agents.bus_store import AgentBusStore  # noqa: E402
from opsb_core.agents.manager import AgentMeshManager  # noqa: E402
from opsb_core.drivers.base import resolve_workspace_state_dir  # noqa: E402


class FakeAdapter(BaseWorkerAdapter):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.turns: list[str] = []

    def send_turn(self, prompt, on_event=None):
        self.turns.append(prompt)
        return f"turn {len(self.turns)} done"

    def compact(self, checkpoint):
        return "compacted"

    def stop(self):
        pass


def _wait(predicate, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "phome"))
    monkeypatch.setenv("PARHELION_DISABLE_DAEMON_PROXY", "1")
    monkeypatch.delenv("OPSB_DEVICE_ISOLATED", raising=False)
    ws = (tmp_path / "contest").resolve()
    ws.mkdir()
    mgr = AgentMeshManager(adapter_factory=FakeAdapter)
    monkeypatch.setattr(mgr, "_cached_doctor", lambda: {"diagnostics": [
        dict(kind="deepseek", auth_ok=True, binary_path="dsh", installed=True)]})
    yield ws, mgr
    mgr.stop_all()


def _git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "kernel.asc").write_text("// kernel\n")
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"],
                 ["add", "."], ["commit", "-q", "-m", "baseline"]):
        subprocess.run(["git", "-C", str(path), *args], check=True)


def _make_contest(ws: Path, count: int) -> list[str]:
    # Each problem repo is built in place: a shared template copy is one more filesystem race in the
    # full suite, and the sprint code only needs a committed repo per problem.
    titles = [f"Op{i:02d}" for i in range(count)]
    for title in titles:
        _git_repo(ws / title)
    (ws / "opsb.workspace.json").write_text(json.dumps({
        "layout": "clean-agents-first",
        "problems": [{"title": t, "path": t, "difficulty": 2} for t in titles],
    }))
    return titles


# 1. seat identity ---------------------------------------------------------------------------

def _record(store, agent_id, problem, *, seat="", worktree="", role="worker"):
    return store.create_agent({
        "agent_id": agent_id, "driver": "deepseek", "role_label": role, "problem": problem,
        "axis": seat, "seat": seat, "worktree": str(worktree), "branch": Path(str(worktree)).name or seat,
        "status": "idle",
    })


def test_seat_identity_reads_the_new_column_and_keeps_the_legacy_layout(tmp_path):
    from opsb_core.agents.seat_plan import seat_of, sprint_seats

    store = AgentBusStore(tmp_path)
    _record(store, "dee-new1", "Demo", seat="race-2", worktree=tmp_path / "Demo/.worktrees/sprint-race-2")
    # A 4.2 record has no seat column: the worktree name still identifies it.
    _record(store, "dee-old1", "Demo", worktree=tmp_path / "Demo/.worktrees/sprint-dataflow")
    _record(store, "dee-other", "Demo", worktree=tmp_path / "Demo/.worktrees/tune-20260101-000000")
    workers = store.list_agents(limit=-1)
    by_id = {w["agent_id"]: w for w in workers}
    assert seat_of(by_id["dee-new1"]) == "race-2"
    assert seat_of(by_id["dee-old1"]) == "dataflow"
    assert seat_of(by_id["dee-other"]) == ""
    assert set(sprint_seats(workers)) == {("Demo", "race-2"), ("Demo", "dataflow")}


# 2. modes -----------------------------------------------------------------------------------

def test_modes_expand_to_the_documented_seats():
    from opsb_core.agents.seat_plan import expand_seats

    def seats(mode, tracks=None):
        return [(s["seat"], s["role"], s["track"]) for s in expand_seats(mode, tracks)]

    assert seats("solo") == [("solo", "worker", "sprint-solo")]
    assert seats("dual") == [("algorithm", "worker", "sprint-algorithm"),
                             ("dataflow", "worker", "sprint-dataflow")]
    assert seats("full") == [("algorithm", "worker", "sprint-algorithm"),
                             ("dataflow", "worker", "sprint-dataflow"),
                             ("calibration", "bench-author", "sprint-calibration")]
    assert seats("race") == [("race-1", "race", "sprint-race-1"), ("race-2", "race", "sprint-race-2")]
    assert [s for s, _, _ in seats("race", 3)] == ["race-1", "race-2", "race-3"]
    assert seats("pipeline") == [("recon", "recon", "sprint-recon"), ("impl-1", "worker", "sprint-impl-1"),
                                 ("impl-2", "worker", "sprint-impl-2"), ("integrator", "integrator", "sprint-integrator"),
                                 ("harness", "bench-author", "sprint-harness")]
    assert [s for s, _, _ in seats("pipeline", 1)] == ["recon", "impl-1", "integrator", "harness"]
    race = expand_seats("race", 3)
    assert "one of 3 tracks" in race[0]["task"]
    with pytest.raises(ValueError, match="Unknown sprint mode"):
        expand_seats("triple")
    with pytest.raises(ValueError, match="seats must be between"):
        expand_seats("race", 99)


def test_sprint_modes_open_the_planned_seats_for_the_right_problems(env):
    from opsb_core.agents.sprint import start_sprint

    ws, mgr = env
    titles = _make_contest(ws, 2)
    race = start_sprint(mgr, ws, driver="deepseek", mode="race", preview=True)
    assert {s["state"] for s in race["assignments"]} == {"planned"}
    assert [(s["problem"], s["seat"]) for s in race["assignments"]] == [
        (titles[0], "race-1"), (titles[0], "race-2"), (titles[1], "race-1"), (titles[1], "race-2")]
    pipeline = start_sprint(mgr, ws, driver="deepseek", mode="pipeline", seats=3, preview=True)
    assert [s["seat"] for s in pipeline["assignments"]][:6] == [
        "recon", "impl-1", "impl-2", "impl-3", "integrator", "harness"]
    assert {s["role"] for s in pipeline["assignments"]} >= {"recon", "integrator", "bench-author"}


# 3. per-problem modes and the knowledge seat ------------------------------------------------

def test_problem_objects_choose_a_mode_per_problem_and_unknown_titles_raise(env):
    from opsb_core.agents.sprint import start_sprint
    from opsb_core.mcp.handlers_agents import bind_daemon_agent_manager
    from opsb_core.mcp.tools import call_tool

    ws, mgr = env
    titles = _make_contest(ws, 3)
    out = start_sprint(mgr, ws, driver="deepseek", preview=True, problems=[
        {"title": titles[1], "mode": "dual"}, {"title": titles[0]}, {"title": titles[2], "mode": "full"}])
    table = [(s["problem"], s["seat"], s["mode"]) for s in out["assignments"]]
    assert table == [(titles[0], "solo", "solo"),
                     (titles[1], "algorithm", "dual"), (titles[1], "dataflow", "dual"),
                     (titles[2], "algorithm", "full"), (titles[2], "dataflow", "full"),
                     (titles[2], "calibration", "full")]
    with pytest.raises(ValueError, match="Unknown problems"):
        start_sprint(mgr, ws, driver="deepseek", preview=True, problems=[{"title": "Nope", "mode": "dual"}])
    with pytest.raises(ValueError, match="each problem object needs a title"):
        start_sprint(mgr, ws, driver="deepseek", preview=True, problems=[{"mode": "dual"}])
    with pytest.raises(ValueError, match="seats for"):
        start_sprint(mgr, ws, driver="deepseek", preview=True, problems=[{"title": titles[0], "seats": "two"}])

    bind_daemon_agent_manager(mgr)
    try:
        via_tool = call_tool("parhelion_sprint_start", {"driver": "deepseek", "preview": True,
                                                       "problems": [{"title": titles[0], "mode": "race", "seats": 3}]}, ws)
        assert [s["seat"] for s in via_tool["assignments"]] == ["race-1", "race-2", "race-3"]
        with pytest.raises(ValueError, match="list of problem titles"):
            call_tool("parhelion_sprint_start", {"problems": "Op0"}, ws)
        with pytest.raises(ValueError, match="mode must be one of"):
            call_tool("parhelion_sprint_start", {"mode": "bogus"}, ws)
    finally:
        bind_daemon_agent_manager(None)


def test_knowledge_seat_needs_no_problem_and_delivers_knowledge(env):
    from opsb_core.agents.sprint import start_sprint

    ws, mgr = env
    _make_contest(ws, 1)
    preview = start_sprint(mgr, ws, driver="deepseek", preview=True, role="knowledge")
    assert [(s["problem"], s["seat"], s["role"]) for s in preview["assignments"]] == [
        ("", "knowledge", "knowledge")]
    started = start_sprint(mgr, ws, driver="deepseek", role="knowledge")
    assert [s["state"] for s in started["assignments"]] == ["started"]
    agent = mgr._store(ws).list_agents(limit=-1)[0]
    assert agent["role_label"] == "knowledge"
    # The seat reuses its live slot instead of opening a second one.
    assert [s["state"] for s in start_sprint(mgr, ws, driver="deepseek", role="knowledge")["assignments"]] == ["existing"]


# 4. mid-sprint changes ----------------------------------------------------------------------

def test_adjust_adds_seats_for_listed_problems_only(env):
    from opsb_core.agents.sprint import start_sprint

    ws, mgr = env
    titles = _make_contest(ws, 2)
    assert [s["state"] for s in start_sprint(mgr, ws, driver="deepseek")["assignments"]] == ["started", "started"]
    grew = start_sprint(mgr, ws, driver="deepseek", adjust=True, problems=[{"title": titles[0], "mode": "dual"}])
    assert sorted(s["seat"] for s in grew["assignments"]) == ["algorithm", "dataflow"]
    assert {s["state"] for s in grew["assignments"]} == {"started"}
    live = {(a["problem"], a["seat"]) for a in mgr._store(ws).list_agents(limit=-1) if a["status"] != "stopped"}
    assert live == {(titles[0], "solo"), (titles[1], "solo"), (titles[0], "algorithm"), (titles[0], "dataflow")}
    with pytest.raises(ValueError, match="adjust=true needs"):
        start_sprint(mgr, ws, driver="deepseek", adjust=True)


def test_stop_names_a_seat_and_is_idempotent(env):
    from opsb_core.agents.sprint import start_sprint

    ws, mgr = env
    titles = _make_contest(ws, 2)
    start_sprint(mgr, ws, driver="deepseek")
    with pytest.raises(ValueError, match="several problems"):
        start_sprint(mgr, ws, driver="deepseek", stop=["solo"])
    with pytest.raises(ValueError, match="Unknown seat"):
        start_sprint(mgr, ws, driver="deepseek", stop=["race-9"])
    planned = start_sprint(mgr, ws, driver="deepseek", preview=True, stop=[f"{titles[0]}/solo"])
    assert planned["stopped"][0]["state"] == "planned"
    assert all(a["status"] != "stopped" for a in mgr._store(ws).list_agents(limit=-1))
    stopped = start_sprint(mgr, ws, driver="deepseek", stop=[f"{titles[0]}/solo"])
    assert stopped["stopped"][0]["state"] == "stopped"
    assert stopped["ok"]
    again = start_sprint(mgr, ws, driver="deepseek", stop=[f"{titles[0]}/solo"])
    assert again["stopped"][0]["state"] == "already_stopped"
    seat_states = {a["seat"]: a["status"] for a in mgr._store(ws).list_agents(limit=-1)
                   if a["problem"] == titles[0]}
    assert seat_states == {"solo": "stopped"}


def _seed_failure_and_handoff(ws: Path, title: str, agent_id: str, text: str = "raised the tiling to 48KB"):
    from opsb_core.ledger import store as ledger_store

    ledger_store.Ledger(ws, title, "dee-old0").append(
        "failure", {"claim": "schMode explosion", "approach": "one giant schMode",
                    "why_failed": "compile time blew past the limit",
                    "retry_conditions": "only with fewer template instantiations"}, source="platform")
    path = resolve_workspace_state_dir(ws) / "handoffs" / f"{agent_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agent_id": agent_id, "narrative_final": f"## Next experiment\n{text}"}) + "\n")


def test_replace_hands_over_failures_and_the_last_narrative_once(env):
    from opsb_core.agents.sprint import start_sprint

    ws, mgr = env
    titles = _make_contest(ws, 1)
    title = titles[0]
    started = start_sprint(mgr, ws, driver="deepseek")
    old_id = started["assignments"][0]["agent_id"]
    _seed_failure_and_handoff(ws, title, old_id)
    first = start_sprint(mgr, ws, driver="deepseek", replace=[title])
    slot = first["replaced"][0]
    assert slot["state"] == "started" and slot["seat"] == "replace-1" and slot["direction"] == "dataflow"
    assert slot["handoff_to"] == [old_id]
    new_id = slot["agent_id"]
    assert _wait(lambda: mgr._runners[new_id].adapter.turns)
    prompt = mgr._runners[new_id].adapter.turns[0]
    assert "## 6. Handover" in prompt
    assert "one giant schMode" in prompt and "compile time blew past the limit" in prompt
    assert "raised the tiling to 48KB" in prompt
    assert f"handoffs/{old_id}.jsonl" in prompt
    # The replaced seat is asked to hand off and stop at a boundary; it is not killed outright.
    directive = " ".join(m["body"] for m in mgr._store(ws).list_messages(agent_id=old_id, direction="orch_to_worker"))
    assert "handoff now" in directive and "orthogonal 'dataflow'" in directive
    assert mgr._store(ws).get_agent(old_id)["status"] != "stopped"
    # Replaying the same stagnation trigger does not open a second replacement seat.
    replay = start_sprint(mgr, ws, driver="deepseek", replace=[title])
    assert replay["replaced"][0]["state"] == "existing" and replay["replaced"][0]["agent_id"] == new_id
    live = [a for a in mgr._store(ws).list_agents(limit=-1) if a["status"] != "stopped"]
    assert sorted(a["seat"] for a in live) == ["replace-1", "solo"]


def test_replace_is_keyed_by_the_stagnation_event(env):
    from opsb_core.agents.sprint import start_sprint

    ws, mgr = env
    titles = _make_contest(ws, 1)
    title = titles[0]
    store = mgr._store(ws)
    start_sprint(mgr, ws, driver="deepseek")
    store.append_message("platform", "worker_to_orch", "stagnation", "six paid attempts",
                         problem=title, priority="strategic", event_key="strategy:s1:stagnation")
    first = start_sprint(mgr, ws, driver="deepseek", replace=[title])
    assert first["replaced"][0]["trigger"] == "strategy:s1:stagnation"
    # A second stagnation event is a new trigger and may replace the problem again.
    store.update_agent(first["replaced"][0]["agent_id"], status="stopped")
    store.append_message("platform", "worker_to_orch", "stagnation", "six more attempts",
                         problem=title, priority="strategic", event_key="strategy:s2:stagnation")
    second = start_sprint(mgr, ws, driver="deepseek", replace=[title])
    assert second["replaced"][0]["trigger"] == "strategy:s2:stagnation"
    assert second["replaced"][0]["seat"] == "replace-2"
    assert second["replaced"][0]["state"] == "started"


# 5. stagnation executor ---------------------------------------------------------------------

def test_stagnation_message_carries_the_executable_replace_call():
    from opsb_core.agents.judge_feedback import judge_feedback

    rows = [dict(submission_id="1", submitted_at="2026-11-01T00:00:01Z", status="Pass", score=40, title="Op00")]
    rows += [dict(submission_id=str(i), submitted_at=f"2026-11-01T00:00:{i:02d}Z", status="Pass", score=41,
                  title="Op00") for i in range(2, 8)]
    events = {k: message for _row, kinds in judge_feedback(rows, 5, 1) for k, message in kinds}
    assert 'parhelion_sprint_start(replace=["Op00"])' in events["stagnation"]


def test_auto_replace_is_off_by_default_and_uses_the_switch(env):
    from opsb_core.agents.sprint import start_sprint
    from opsb_core.config import ConfigRepository

    ws, mgr = env
    titles = _make_contest(ws, 1)
    title = titles[0]
    start_sprint(mgr, ws, driver="deepseek")
    assert mgr.auto_replace_on_stagnation(ws, title)["skipped"] == "agents.auto_replace_on_stagnation is off"
    assert len(mgr._store(ws).list_agents(limit=-1)) == 1
    cfg = ConfigRepository.load()
    cfg["agents"] = {"auto_replace_on_stagnation": True}
    ConfigRepository.save(cfg)
    out = mgr.auto_replace_on_stagnation(ws, title)
    assert out["ok"] and out["replaced"][0]["state"] == "started"
    again = mgr.auto_replace_on_stagnation(ws, title)
    assert again["replaced"][0]["state"] == "existing"


# 6. role permissions ------------------------------------------------------------------------

def test_recon_and_knowledge_tables_are_shorter_and_enforced(env, monkeypatch):
    from opsb_core.mcp import tools
    from opsb_core.mcp.tools import call_tool, list_tool_schemas

    monkeypatch.setattr(tools, "_SUBROLE_CACHE", {})
    worker = {t["name"] for t in list_tool_schemas(role="worker")}
    recon = {t["name"] for t in list_tool_schemas(role="recon")}
    knowledge = {t["name"] for t in list_tool_schemas(role="knowledge")}
    assert len(worker) == 24 and len(recon) == 18 and len(knowledge) == 14
    assert recon < worker and knowledge < recon
    assert {"parhelion_test", "parhelion_test_wait", "parhelion_test_cancel", "parhelion_test_plan"} & recon == set()
    assert "parhelion_submit_gate" in recon and "parhelion_submit_gate" not in knowledge
    assert {"parhelion_kb_record", "parhelion_kb_query"} <= knowledge

    ws, mgr = env
    with pytest.raises(PermissionError, match="disabled for MCP role 'recon'"):
        call_tool("parhelion_test", {"problem": "Demo"}, ws, role="recon")
    with pytest.raises(PermissionError, match="preview"):
        call_tool("parhelion_submit_gate", {"problem": "Demo", "enqueue": True}, ws, role="recon")
    with pytest.raises(PermissionError, match="disabled for MCP role 'knowledge'"):
        call_tool("parhelion_submit_gate", {"problem": "Demo"}, ws, role="knowledge")

    # A worker seat's bus record decides the subrole of its MCP calls.
    store = mgr._store(ws)
    _record(store, "dee-recon", "Demo", seat="recon", worktree=ws / "Demo/.worktrees/sprint-recon", role="recon")
    assert tools.worker_subrole(ws, "dee-recon") == "recon"
    from opsb_core.mcp.server import handle_mcp_http_request
    code, resp = handle_mcp_http_request(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(), ws,
        role="worker", agent_id="dee-recon")
    assert code == 200
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "parhelion_test" not in names and "parhelion_submit_gate" in names


# 7. remote checkout root --------------------------------------------------------------------

def _device_config(tmp_path: Path, device: dict) -> None:
    home = Path(__import__("os").environ["PARHELION_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"device": device}))


def test_remote_problem_dir_comes_from_the_device_settings(env, tmp_path):
    from opsb_core.commands.queue_cmd import _remote_problem_dir

    ws, _mgr = env
    (ws / "opsb.workspace.json").write_text(json.dumps({"contest_id": "demo-contest", "problems": []}))
    assert _remote_problem_dir(ws, "Op00") == "/srv/work/demo-contest/Op00"  # neutral default
    _device_config(tmp_path, {"work_root": "/srv/work"})
    assert _remote_problem_dir(ws, "Op00") == "/srv/work/demo-contest/Op00"
    _device_config(tmp_path, {"work_root": "/srv/work", "work_prefix": "bench", "contest_work_root": "/srv/contests"})
    assert _remote_problem_dir(ws, "Op00") == "/srv/contests/demo-contest/Op00"
    (ws / "opsb.workspace.json").write_text(json.dumps({"contest_id": "demo-contest", "npu_remote_root": "/srv/pinned"}))
    assert _remote_problem_dir(ws, "Op00") == "/srv/pinned/Op00"
    (ws / "opsb.workspace.json").write_text(json.dumps({"problems": []}))
    assert _remote_problem_dir(ws, "Op00") == "/srv/work/bench/Op00"


def test_repository_carries_no_host_specific_compute_path():
    source = (CLI_DIR / "opsb_core/commands/queue_cmd.py").read_text(encoding="utf-8")
    assert "/home/developer/work" not in source
    from opsb_core import device_env

    assert device_env.DEFAULTS["contest_work_root"] == ""
    assert device_env.DEFAULTS["work_root"].startswith("/srv/")


# 8. brief contracts -------------------------------------------------------------------------

def test_each_seat_contract_fits_the_brief_cap_and_ends_with_the_data_channel(env):
    from opsb_core.drivers.brief import DATA_CHANNEL, MAX_BRIEF_BYTES, compose_seat_contract

    ws, _mgr = env
    titles = _make_contest(ws, 1)
    for role in ("race", "recon", "integrator", "knowledge"):
        text = compose_seat_contract(ws, role, problem=titles[0], worktree=ws / titles[0],
                                     branch="sprint-" + role, goal="Raise the score.")
        assert len(text.encode("utf-8")) <= MAX_BRIEF_BYTES, role
        assert text.splitlines()[-1] == DATA_CHANNEL
        for field in ("Worktree", "Commands:", "Deliverable:", "Recipient:", "Stop when:"):
            assert field in text, (role, field)
        assert "parhelion_report" in text
    assert "race" in compose_seat_contract(ws, "race", problem=titles[0], goal="g")
    with pytest.raises(ValueError, match="Unknown seat contract"):
        compose_seat_contract(ws, "worker", problem=titles[0])


def test_startup_pack_keeps_the_handover_section(env):
    from opsb_core.drivers import startup_pack

    ws, _mgr = env
    titles = _make_contest(ws, 1)
    pack = startup_pack.build(ws, titles[0], contract="contract body", handover="Replacement seat: failed ways")
    assert "## 6. Handover" in pack["text"]
    assert "Replacement seat: failed ways" in pack["text"]
    assert pack["sections"]["Handover"]["bytes"] > 0
    plain = startup_pack.build(ws, titles[0], contract="contract body")
    assert "## 6. Handover" not in plain["text"]
