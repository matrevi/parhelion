"""4.4 P34: user-ticked Harnesses and the doc-scout seat (request, question, answer card, findings).

Temporary PARHELION_HOME and workspaces only; no daemon, device, model or network.
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core.drivers import availability  # noqa: E402
from opsb_core.guidance import cards, dossier, gap, scout, settings  # noqa: E402
from opsb_core.ledger import store as lstore  # noqa: E402

S = settings({})
NOW = datetime.datetime(2026, 11, 26, 22, 0, tzinfo=datetime.timezone.utc)
AGENT = "dee-aaaaaa"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "ph"))
    monkeypatch.delenv("PARHELION_ALLOW_CLAUDE_WORKERS", raising=False)
    return tmp_path


@pytest.fixture()
def ws(home):
    root = (home / "contest").resolve()
    root.mkdir()
    (root / "opsb.workspace.json").write_text(json.dumps({"problems": [{"title": "Op", "path": "Op"}]}))
    return root


def test_available_harnesses_default_set_and_claude_opt_in(home):
    assert availability.available({}) == ["antigravity", "codex", "pi", "gemini", "deepseek"]  # 4.3 behaviour
    assert availability.available({"agents": {"available_drivers": ["deepseek", "bogus"]}}) == ["deepseek"]
    assert availability.available({"agents": {"available_drivers": ["deepseek"]}, "allow_claude_workers": True}) \
        == ["deepseek", "claude"]
    cfg = {"agents": {"available_drivers": ["deepseek"]}}
    assert availability.refusal("codex", cfg).startswith("Harness 'codex' is not available")
    assert availability.choose("codex", cfg) == "deepseek" and availability.choose("deepseek", cfg) == "deepseek"
    assert availability.set_available(["deepseek"])["available_drivers"] == ["deepseek"]
    assert availability.available() == ["deepseek"]
    with pytest.raises(ValueError):
        availability.set_available(["nope"])


def test_cli_driver_available_sets_the_list(home, capsys):
    import argparse
    from opsb_core.commands import driver_mcp_cmd
    assert driver_mcp_cmd.run_driver(argparse.Namespace(driver_cmd="available", set="deepseek", json=True)) == 0
    assert json.loads(capsys.readouterr().out)["available_drivers"] == ["deepseek"]
    from opsb_core.drivers.registry import doctor_drivers
    doc = doctor_drivers()
    assert doc["available_drivers"] == ["deepseek"]
    assert {d["kind"]: d["available"] for d in doc["diagnostics"]}["codex"] is False


def test_spawn_pool_and_hot_state_respect_the_ticked_list(ws):
    from opsb_core.agents.manager import AgentMeshManager
    availability.set_available(["deepseek"])
    mgr = AgentMeshManager()
    diags = [{"kind": k, "installed": True, "binary_path": f"/bin/{k}", "auth_ok": True} for k in ("codex", "deepseek")]
    mgr._cached_doctor = lambda: {"active_driver": "antigravity", "diagnostics": diags}
    mgr._observed_driver_sessions = lambda w, d: ({}, [])
    with pytest.raises(ValueError, match="not available"):
        mgr.spawn_agent(ws, driver="codex", task="x", problem="")
    pool = mgr.get_harness_pool(ws)
    assert [d["kind"] for d in pool["eligible_external_drivers"]] == ["deepseek"]
    assert pool["dispatch_policy"]["available_drivers"] == ["deepseek"]
    assert [d["kind"] for d in mgr.build_hot_mesh(ws)["ready_drivers"]] == ["deepseek"]


def test_sprint_driver_choice_and_scout_role(ws):
    from opsb_core.agents import seat_plan, sprint
    availability.set_available(["deepseek"])

    class Mgr:
        def _cached_doctor(self):
            return {"diagnostics": []}
    with pytest.raises(ValueError, match="not available"):
        sprint.start_sprint(Mgr(), ws, driver="codex", preview=True)
    with pytest.raises(ValueError, match="needs the problems"):
        sprint.start_sprint(Mgr(), ws, role="scout", preview=True)
    assert seat_plan.seat_role("scout-2") == "scout" and "scout" in seat_plan.NON_TEST_ROLES
    occupied = {("Op", "scout"): {"status": "stopped"}}
    scout._log_path(ws).parent.mkdir(parents=True, exist_ok=True)
    scout._log_path(ws).write_text(json.dumps({"event": "requested", "problem": "Op", "question": "Q-TEXT"}) + "\n")
    seat = sprint.scout_seat(ws, "Op", occupied)
    assert seat["seat"] == "scout-2" and seat["role"] == "scout" and seat["task"].endswith("Q-TEXT")


def _triage():
    rows = [{"case": f"#{i:02d}", "ours_us": 10.0 * r, "tbest_us": 10.0} for i, r in enumerate([400, 300, 2], 1)]
    return gap.triage(rows, 5, S)


STUCK = {"class": "scale_mismatch", "signal": "2 of the top 3 gap cases are >= 50x tbest"}


class _Mgr:
    def __init__(self, ws):
        from opsb_core.agents.bus_store import AgentBusStore
        self.store = AgentBusStore(ws)

    def _store(self, ws):
        return self.store


def test_request_once_per_question_and_interval_with_the_exact_call(ws):
    availability.set_available(["deepseek"])
    mgr = _Mgr(ws)
    key = scout.key_of("Op", "scale_mismatch", _triage(), "abc1234")
    assert scout.due(ws, "Op", STUCK, None, key, NOW, S) == "scale_mismatch"
    text = scout.question("Op", STUCK, _triage(), reason="scale_mismatch", lead_ref="w/L-0003")
    assert "algorithm tier" in text and "execution unit" in text and "w/L-0003" in text
    scout.request(mgr, ws, "Op", AGENT, "scale_mismatch", text, key, S, now=NOW)
    msg = next(m for m in mgr.store.list_messages(limit=10) if m["kind"] == "scout_request")
    assert msg["priority"] == "strategic" and 'role="scout"' in msg["body"] and "deepseek" in msg["body"]
    assert scout.pending_question(ws, "Op") == text
    assert scout.due(ws, "Op", STUCK, None, key, NOW, S) == ""  # same question
    other = scout.key_of("Op", "scale_mismatch", _triage(), "def5678")
    assert scout.due(ws, "Op", STUCK, None, other, NOW, S) == ""  # inside the hourly interval
    plateau = {"class": "plateau", "signal": "flat"}
    card = {"card_id": "G-1", "class": "plateau", "status": "delivered",
            "delivered_at": (NOW - datetime.timedelta(minutes=40)).isoformat()}
    later = NOW + datetime.timedelta(minutes=61)
    assert scout.due(ws, "Op", plateau, card, other, later, S).startswith("still plateau after G-1")
    assert scout.due(ws, "Op", plateau, {**card, "status": "adopted"}, other, later, S) == ""


def test_scout_answer_becomes_a_card_and_leads_findings(ws):
    availability.set_available(["deepseek"])
    mgr = _Mgr(ws)
    mgr.store.create_agent({"agent_id": AGENT, "driver": "deepseek", "problem": "Op", "worktree": str(ws)})
    rec = mgr.store.get_agent(AGENT)
    exps = [{"type": "experiment", "sha": "abc1234", "ts": NOW.isoformat()}]
    assert scout.step(mgr, ws, S, NOW, rec, STUCK, _triage(), exps, [], set()) is None  # requested, no answer yet
    led = lstore.Ledger(ws, "Op", "dee-scout1")
    led.append("insight", {"claim": "docs: use the matrix unit above 256", "why": "guide 4.2",
                           "actionable_next": "move the N>=256 path"}, source="worker")
    rows = lstore.read_problem(ws, "Op")
    made = scout.step(mgr, ws, S, NOW, rec, STUCK, _triage(), exps, rows, {"dee-scout1"})
    assert made and made["class"] == "scout" and "dee-scout1/L-0001" in made["text"]
    assert "matrix unit above 256" not in made["text"]  # cited by ref, not quoted
    assert len(made["text"].encode("utf-8")) <= 800
    assert scout.step(mgr, ws, S, NOW, rec, STUCK, _triage(), exps, rows, {"dee-scout1"}) is None  # answered once
    text = dossier.render_findings("Op", rows, {"dee-scout1"})
    assert text.index("Doc scout findings") < text.index("Unexploited insights")
    assert text.count("matrix unit above 256") == 1


def test_scout_subrole_and_contract(ws, monkeypatch):
    from opsb_core.drivers import brief
    from opsb_core.mcp import tools
    assert "parhelion_test" in tools.SUBROLE_DENIED["scout"] and "parhelion_submit_gate" in tools.SUBROLE_DENIED["scout"]
    assert "scout" in tools.WORKER_SUBROLES
    repo = ws / "Op"
    repo.mkdir()
    text = brief.compose_seat_contract(ws, "scout", problem="Op", worktree=repo, goal="answer")
    assert "mirror is still empty" in text and "parhelion_note type=insight" in text
    from opsb_core.commands import docs_cmd
    page = ws / "guide.md"
    page.write_text("# guide\n")
    assert docs_cmd.add([str(page)])["files"] == 1
    text = brief.compose_seat_contract(ws, "scout", problem="Op", worktree=repo, goal="answer")
    assert f"`{docs_cmd.mirror_dir()}` first" in text and "parhelion_note type=insight" in text
    assert len(text.encode("utf-8")) <= brief.MAX_BRIEF_BYTES


def test_a_live_scout_card_is_not_superseded_by_the_stuck_class(ws, monkeypatch):
    from opsb_core.guidance import monitor
    mgr = _Mgr(ws)
    mgr.store.create_agent({"agent_id": AGENT, "driver": "deepseek", "problem": "Op", "worktree": str(ws)})
    mgr.store.update_agent(AGENT, status="working")
    led = lstore.Ledger(ws, "Op", AGENT)
    for i in range(3):
        led.append("experiment", {"claim": f"r{i}", "outcome": "WA", "scope": {"cases": ["05"]},
                                  "actual_outcome": {"level": "standard"}, "test_run_id": f"r{i}"})
    cards.issue(ws, AGENT, "Op", "scout", "[guide G-? · scout · Op] read findings", {}, repeat_sec=0)
    monkeypatch.setattr(monitor, "settings", lambda: dict(S))
    monkeypatch.setattr(monitor, "kb_entries", lambda: [])
    monitor.tick(mgr, ws, force=True)
    seat = cards.read_seats(ws)[AGENT]
    assert seat["class"] == "wa_loop" and seat["card"]["class"] == "scout"
