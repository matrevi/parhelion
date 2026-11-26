"""P27: the start-up intel block, workspace broadcasts with evidence classes, and ``view=sweep``.

Covered here in the plan's order:

1. the intel block: leader and weighted space, the last three shots, the stop condition, the rule
   line, its byte budget, and the pack/MCP-meter wiring;
2. ``parhelion_squad(to="all")``: every running seat except the sender, the worker rate limit, the
   orchestrator exemption, and the recipient ledger row;
3. evidence classes: the required ``{class, ref}``, the ``待验证`` mark, the bus row's class, and an
   old envelope without evidence reading as ``local``;
4. ``parhelion_report(view=sweep)``: fields per metric, the orchestrator-only rule and the 4KB cap;
5. the two sprint metrics over mesh logs.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[1]
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from opsb_core import evidence  # noqa: E402
from opsb_core.agents import peer  # noqa: E402
from opsb_core.agents.adapters import BaseWorkerAdapter  # noqa: E402
from opsb_core.agents.manager import AgentMeshManager  # noqa: E402
from opsb_core.drivers import startup_pack as sp  # noqa: E402
from opsb_core.ledger import store as ledger_store  # noqa: E402
from opsb_core.mcp import handlers_report  # noqa: E402
from opsb_core.mcp.report_views import SWEEP_BYTES  # noqa: E402
from opsb_core.mcp.tools import call_tool, list_tool_schemas  # noqa: E402
from opsb_core.services import state_card_ext  # noqa: E402

SHA_A = "a" * 40
SHA_B = "b" * 40


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _text(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "phome"))
    monkeypatch.setenv("PARHELION_DISABLE_DAEMON_PROXY", "1")
    monkeypatch.setattr("opsb_core.agents.manager.PEER_COALESCE_SEC", 30.0)
    ws = (tmp_path / "contest").resolve()
    ws.mkdir()
    (ws / "opsb.workspace.json").write_text(json.dumps({"problems": [
        {"title": "Op", "difficulty": 3, "path": "Op"},
        {"title": "Op2", "difficulty": 2, "path": "Op2"},
    ]}), encoding="utf-8")
    for name in ("Op", "Op2"):
        (ws / name).mkdir()
    return ws


def _ledger_row(ws: Path, worker: str, row: dict) -> None:
    root = ledger_store.problem_dir(ws, "Op")
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{worker}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _receipt(ws: Path, rows: list[dict]) -> None:
    results = ws / "results"
    results.mkdir(exist_ok=True)
    (results / "submit-queue-outcome.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def _live_board(ws: Path, problems: dict) -> None:
    path = ws / "results" / "scoring-evidence" / "live-board-tbest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"problems": problems}), encoding="utf-8")


def _intel_workspace(ws: Path) -> None:
    _live_board(ws, {"Op": {"frontier_top1": 90.0, "our_score": 30.0, "board_submission_id": "sub-2"}})
    _receipt(ws, [
        {"title": "Op", "sha": SHA_A, "status": "Pass", "score": 24.0, "submission_id": "sub-1",
         "submitted_at": "2026-11-26T01:00:00Z", "note": "tile the reduce, one dma stage"},
        {"title": "Op", "sha": SHA_B, "status": "Pass", "score": 31.5, "submission_id": "sub-2",
         "submitted_at": "2026-11-26T02:00:00Z", "note": "widen the gather"},
        {"title": "Op", "sha": "c" * 40, "status": "Wrong Answer", "submission_id": "sub-3",
         "submitted_at": "2026-11-26T03:00:00Z", "note": "smaller tiles"},
    ])
    _ledger_row(ws, "dee-1", {"id": "L-0001", "type": "hypothesis", "claim": "split the reduce by row",
                              "status": "open", "confidence": "inferred", "ts": "2026-11-26T00:30:00.000+00:00",
                              "worker": "dee-1", "scope": {"bottleneck": "memory"}})
    _ledger_row(ws, "dee-1", {"id": "L-0002", "type": "experiment", "claim": "tile the reduce, one dma stage",
                              "outcome": "worse", "ts": "2026-11-26T01:01:00.000+00:00", "worker": "dee-1",
                              "links": ["L-0001"], "sha": SHA_A})
    rule = ws / "results" / "Op" / ".judge-rule.json"
    rule.parent.mkdir(parents=True, exist_ok=True)
    rule.write_text(json.dumps({"schema_version": 1, "ranking_submission_mode": "best", "testcases": {"total": 15},
                                "fetched_at": "2026-11-26T00:00:00Z"}), encoding="utf-8")


# ---------------------------------------------------------------- 1. the intel block


def test_intel_block_carries_standing_shots_stop_condition_and_rules(home):
    ws = home
    _intel_workspace(ws)
    doc = state_card_ext.intel(ws, "Op")
    assert doc["weight"] == 3
    standing = doc["standing"]
    assert (standing["leader"], standing["board"], standing["weight"]) == (90.0, 30.0, 3)
    assert standing["gap_weighted"] == pytest.approx(180.0)  # (90 - 30) * weight
    assert [r["sha"] for r in doc["recent"]] == ["ccccccc", SHA_B[:7], SHA_A[:7]]  # newest first
    assert [r["high"] for r in doc["recent"]] == [False, True, True]  # a scored Pass is a board entry
    assert doc["recent"][1]["score"] == 31.5 and doc["recent"][0]["score"] == 0.0  # WA scores zero
    assert doc["recent"][1]["mechanism"] == "widen the gather"  # else the receipt note
    assert doc["recent"][2]["mechanism"] == "tile the reduce, one dma stage"  # from the ledger entry
    assert doc["ledger"]["open"] == ["L-0001"]
    assert doc["ledger"]["budget"] == 3 and doc["ledger"]["budget_left"] == 2  # one experiment cited it
    assert doc["ledger"]["no_gain"] == 1  # the newest judged receipt (WA) did not move the board
    assert doc["rule"] == "rule: best"  # no paired receipt here, so no sigma yet
    assert state_card_ext.rule_line(ws, "Op") == "rule: best"

    text = state_card_ext.intel_text(ws, "Op")
    assert text.startswith("INTEL Op (weight x3): leader 90.00, board 30.00, weighted gap 180.00")
    assert "recent: " in text and "high" in text
    assert "stop when: open L-0001 · attempts left 2/3 · 1/6 paid shots without a new high" in text
    assert "rule: best" in text
    assert len(text.encode("utf-8")) <= state_card_ext.INTEL_MAX_BYTES


def test_rule_line_reports_mode_and_sigma(home, monkeypatch):
    ws = home
    from opsb_core.services import judge_rules

    monkeypatch.setattr(judge_rules, "status_line", lambda workspace, problems: {
        "board_mode": "latest", "sigma_hat": 3.16, "df": 26, "scope": "contest"})
    assert state_card_ext.rule_line(ws, "Op") == "rule: latest, sigma 3.16 (contest, df 26)"
    monkeypatch.setattr(judge_rules, "status_line", lambda workspace, problems: {})
    assert state_card_ext.rule_line(ws, "Op") == ""


def test_intel_without_data_degrades_and_never_raises(home, monkeypatch):
    ws = home
    text = state_card_ext.intel_text(ws, "Op")
    assert "leader n/a" in text and "board n/a" in text
    assert "no open hypothesis" in text and "attempts left 3/3" in text
    monkeypatch.setattr(state_card_ext, "INTEL_MAX_BYTES", 60)
    clipped = state_card_ext.intel_text(ws, "Op")
    assert len(clipped.encode("utf-8")) <= 60
    assert clipped.splitlines()[0].startswith("INTEL Op")


def test_intel_is_a_bounded_section_of_the_startup_pack(home, monkeypatch):
    ws = home
    _intel_workspace(ws)
    contract = "# Op · worker\nGoal: raise the score.\n" + "\n".join(["Loop: parhelion_test ..."] * 20)
    pack = sp.build(ws, "Op", contract=contract, worktree=ws / "Op", branch="sprint-solo", driver="deepseek")
    ledger_part = pack["sections"]["Problem ledger"]["text"]
    assert "INTEL Op (weight x3)" in ledger_part
    assert pack["intel_bytes"] == len(state_card_ext.intel_text(ws, "Op").encode("utf-8")) > 0
    assert pack["bytes"] <= pack["budget_bytes"]
    # The intel block lives inside section 5, which the existing shrink order cuts first.
    fitted, cuts = sp.fit({1: "c" * 200, 2: "w" * 200, 5: ledger_part + "x" * 3000}, 900)
    assert cuts and cuts[0]["section"] == 5
    assert "INTEL Op" in fitted.get(5, "") or not fitted.get(5)
    assert sum(len(v.encode("utf-8")) for v in fitted.values()) <= 900
    # The MCP meter records the intel size (P03 `tool=startup_pack`), aggregates only.
    from opsb_core.metrics import mcp_meter

    assert sp.record_size(ws, pack, agent_id="dee-1", role="worker") is True
    rows = mcp_meter.read_rows(ws)
    assert rows and any((row.get("extra") or {}).get("intel_bytes") == pack["intel_bytes"] for row in rows)


# ---------------------------------------------------------------- 2./3. broadcast and evidence


class FakeAdapter(BaseWorkerAdapter):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.turns: list[str] = []

    def send_turn(self, prompt, on_event=None):
        self.turns.append(prompt)
        return "ok"

    def stop(self):
        pass


@pytest.fixture()
def mesh(home, tmp_path, monkeypatch):
    ws = home
    adapters: dict[str, FakeAdapter] = {}

    def factory(workspace, worktree, driver, agent_id, log_path):
        adapters[agent_id] = FakeAdapter(workspace, worktree, driver, agent_id, log_path)
        return adapters[agent_id]

    mgr = AgentMeshManager(adapter_factory=factory)
    monkeypatch.setattr(mgr, "_cached_doctor", lambda: {"diagnostics": [
        dict(kind="deepseek", auth_ok=True, binary_path="dsh", installed=True)]})
    yield ws, mgr, adapters
    mgr.stop_all()


def _spawn(mgr, ws, *, problem="Op", axis="", role="worker"):
    res = mgr.spawn_agent(ws, driver="deepseek", task="work", problem=problem, axis=axis, role_label=role,
                          worktree_mode="readonly", _sprint=True)
    agent_id = res["agent"]["agent_id"]
    assert _wait(lambda: mgr._store(ws).get_agent(agent_id)["status"] == "idle")
    return agent_id


def _pending(ws, agent_id):
    path = peer.pending_path(ws, agent_id)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] \
        if path.is_file() else []


def _peer_rows(ws, mgr, agent_id):
    return [row for row in mgr._store(ws).list_messages(agent_id=agent_id, limit=200)
            if row["kind"] == "peer"]


def test_broadcast_reaches_every_running_seat_but_the_sender(mesh):
    ws, mgr, _ = mesh
    sender = _spawn(mgr, ws, axis="algorithm")
    mate_a = _spawn(mgr, ws, axis="dataflow")
    mate_b = _spawn(mgr, ws, problem="Op2", axis="algorithm")
    out = mgr.squad(ws, sender, to="all", claim="the small bucket repeats once",
                    evidence={"class": "local", "ref": "run-2026"})
    assert out["ok"] is True and out["broadcast"] is True
    assert sorted(out["recipients"]) == sorted([mate_a, mate_b])
    assert _pending(ws, sender) == []  # never to itself
    assert len(_pending(ws, mate_a)) == 1 and len(_pending(ws, mate_b)) == 1
    # the cross-problem recipient's ledger row lands in its own problem
    rows = ledger_store.read_rows(ledger_store.problem_dir(ws, "Op2") / f"{mate_b}.jsonl")
    assert rows and rows[-1]["source"] == "peer" and "evidence local" in rows[-1]["why"]


def test_worker_broadcast_is_limited_to_one_per_ten_minutes(mesh):
    ws, mgr, _ = mesh
    sender = _spawn(mgr, ws, axis="algorithm")
    mate = _spawn(mgr, ws, axis="dataflow")
    first = mgr.squad(ws, sender, to="all", claim="first", evidence={"class": "local", "ref": "run-1"})
    assert first["ok"] and first["recipients"] == [mate]
    second = mgr.squad(ws, sender, to="all", claim="second", evidence={"class": "local", "ref": "run-2"})
    assert second["ok"] is False and second["limited"] is True and second["delivered"] is False
    assert second["recipients"] == [] and "10 minutes" in second["reason"]
    assert second["retry_after_sec"] > 0
    assert len(_pending(ws, mate)) == 1  # the refused one was not queued
    # a directed message is not a broadcast and is not limited
    assert mgr.squad(ws, sender, recipient=mate, claim="direct",
                     evidence={"class": "paired", "ref": "pair-9"})["ok"] is True
    # after the window the seat may broadcast again
    peer.note_broadcast(ws, sender, now=time.time() - peer.BROADCAST_MIN_SEC - 1)
    assert mgr.squad(ws, sender, to="all", claim="third", evidence={"class": "local", "ref": "r3"})["ok"] is True


def test_the_orchestrator_broadcast_is_not_limited(mesh):
    ws, mgr, _ = mesh
    mate_a = _spawn(mgr, ws, axis="algorithm")
    mate_b = _spawn(mgr, ws, problem="Op2", axis="dataflow")
    for index in range(3):
        out = mgr.squad(ws, "orchestrator", to="all", claim=f"directive {index}",
                        evidence={"class": "online_receipt", "ref": f"sub-{index}"})
        assert out["ok"] is True, out
        assert sorted(out["recipients"]) == sorted([mate_a, mate_b])
    assert len(_pending(ws, mate_a)) == 3
    assert "orchestrator" in peer.pending_path(ws, mate_a).read_text(encoding="utf-8")


def test_evidence_is_required_and_the_class_marks_the_body(mesh):
    ws, mgr, _ = mesh
    sender = _spawn(mgr, ws, axis="algorithm")
    mate = _spawn(mgr, ws, axis="dataflow")
    with pytest.raises(ValueError, match="evidence is required"):
        mgr.squad(ws, sender, recipient=mate, claim="no evidence")
    with pytest.raises(ValueError, match="evidence.ref is required"):
        mgr.squad(ws, sender, recipient=mate, claim="empty ref", evidence={"class": "local", "ref": ""})
    with pytest.raises(ValueError, match="evidence class must be one of"):
        mgr.squad(ws, sender, recipient=mate, claim="bad class", evidence={"class": "vibes", "ref": "x"})

    mgr.squad(ws, sender, recipient=mate, claim="local only", evidence={"class": "local", "ref": "run-1"})
    mgr.squad(ws, sender, recipient=mate, claim="judged", evidence={"class": "online_receipt", "ref": "sub-9"})
    mgr._flush_peer(ws, mate)
    rows = _peer_rows(ws, mgr, mate)
    body = rows[-1]["body"]
    assert body.splitlines()[1].startswith("待验证 (local)")  # the mark precedes that body
    assert "evidence=local:run-1" in body and "evidence=online_receipt:sub-9" in body
    # the batch row carries the weakest class for the collaboration page
    assert rows[-1]["evidence_class"] == "local"
    peers = [row for row in rows if row["kind"] == "peer"]
    assert peers and all(row["evidence_class"] == "local" for row in peers)


def test_an_old_envelope_without_evidence_reads_as_local(mesh):
    ws, mgr, _ = mesh
    sender = _spawn(mgr, ws, axis="algorithm")
    mate = _spawn(mgr, ws, axis="dataflow")
    old = {"msg_id": "old1", "from": sender, "to": mate, "claim": "pre-P27 message",
           "refs": ["L-0001"], "ask": "", "detail": "", "full": "", "chars": 15,
           "ts": "2026-11-26T00:00:00+00:00"}
    peer.queue(ws, mate, [old])
    mgr._flush_peer(ws, mate)
    body = _peer_rows(ws, mgr, mate)[-1]["body"]
    assert body.startswith("待验证 (local)")
    assert "evidence=local" in body and "pre-P27 message" in body
    assert _peer_rows(ws, mgr, mate)[-1]["evidence_class"] == "local"


def test_the_squad_schema_carries_the_evidence_object_and_both_roles():
    orchestrator = {tool["name"]: tool for tool in list_tool_schemas("orchestrator")}
    worker = {tool["name"]: tool for tool in list_tool_schemas("worker")}
    assert "parhelion_squad" in orchestrator and "parhelion_squad" in worker
    schema = orchestrator["parhelion_squad"]["inputSchema"]
    evidence_schema = schema["properties"]["evidence"]
    assert evidence_schema["type"] == "object"
    assert evidence_schema["required"] == ["class", "ref"]
    assert evidence_schema["properties"]["class"]["enum"] == list(evidence.CLASSES)
    assert evidence_schema["properties"]["ref"]["type"] == "string"
    for name, spec in schema["properties"].items():
        assert spec.get("description") != "", f"{name} must not carry an empty description"


def test_the_mcp_handler_and_route_sign_the_orchestrator(monkeypatch, mesh):
    ws, mgr, _ = mesh
    from opsb_core.daemon.agent_routes import handle_agent_post
    from opsb_core.mcp import handlers_agents

    monkeypatch.setattr(handlers_agents, "_ACTIVE_DAEMON_MANAGER", mgr)
    mate = _spawn(mgr, ws, axis="dataflow")
    out = call_tool("parhelion_squad", {"to": "all", "claim": "from the orchestrator",
                                        "evidence": {"class": "paired", "ref": "pair-1"}},
                    ws, role="orchestrator")
    assert out["ok"] is True and out["recipients"] == [mate]

    seen: dict = {}

    class Mesh:
        def squad(self, workspace, agent_id, **kwargs):
            seen["agent_id"] = agent_id
            seen.update(kwargs)
            return {"ok": True}

    class Runtime:
        agent_mesh = Mesh()

    status, _ = handle_agent_post("/v1/agents/dee-1/squad", {"to": "all", "claim": "route",
                                                             "evidence": {"class": "local", "ref": "r"}},
                                  ws, Runtime())
    assert status == 200 and seen["to"] == "all"
    assert seen["evidence"] == {"class": "local", "ref": "r"}


# ---------------------------------------------------------------- 4. the sweep view


def _sweep_workspace(ws: Path, *, count: int = 2) -> None:
    problems = [{"title": f"Op{i}", "difficulty": 3, "path": f"Op{i}"} for i in range(count)]
    (ws / "opsb.workspace.json").write_text(json.dumps({"problems": problems}), encoding="utf-8")
    results = ws / "results"
    rows = []
    for index in range(count):
        title, sha = f"Op{index}", f"{index:040d}"
        (ws / title).mkdir(exist_ok=True)
        run = results / title / sha / "bench-runs" / "r1"
        run.mkdir(parents=True, exist_ok=True)
        (run / "report.json").write_text(json.dumps({"sha": sha, "formal": True,
                                                     "finished_at": f"2026-11-26T0{index}:00:00Z"}),
                                         encoding="utf-8")
        (results / title / sha / "result.json").write_text(json.dumps({
            "sha": sha, "title": title, "exit_code": 0, "kernel_o_sum_bytes": 4096 + index,
            "kernel_o_max_text_bytes": 2048, "phase_a_cpu_sec": 12.5 + index,
            "kernel_o_count": 2, "finished_at": f"2026-11-26T0{index}:00:00Z"}), encoding="utf-8")
        rows.append({"title": title, "sha": sha, "status": "Pass", "score": 10.0 + index,
                     "submission_id": f"sub-{index}", "submitted_at": f"2026-11-26T0{index}:10:00Z",
                     "public_times_us": [1.5, 2.5, 3.5]})
    _receipt(ws, rows)
    _live_board(ws, {f"Op{i}": {"frontier_top1": 50.0, "our_score": 10.0 + i, "board_submission_id": f"sub-{i}"}
                     for i in range(count)})


def test_sweep_rows_carry_board_sha_run_sha_metric_and_public_times(home):
    ws = home
    _sweep_workspace(ws)
    size = call_tool("parhelion_report", {"view": "sweep", "metric": "size"}, ws, role="orchestrator")
    assert size["view"] == "sweep" and size["metric"] == "size"
    assert size["columns"] == ["title", "board_sha", "board_score", "run_sha", "basis", "size_bytes",
                               "public_us"]
    row = dict(zip(size["columns"], size["problems"][0]))
    assert row["title"] == "Op0"
    assert row["board_sha"] == "0" * 7 and row["board_score"] == 10.0
    assert row["run_sha"] == "0" * 7 and row["basis"] == "formal"
    assert row["size_bytes"] == 4096
    assert row["public_us"] == [1.5, 2.5, 3.5]
    assert "no device run" in size["source"]

    compile_ = call_tool("parhelion_report", {"view": "sweep", "metric": "compile"}, ws, role="orchestrator")
    assert compile_["columns"][5] == "compile_sec"
    assert dict(zip(compile_["columns"], compile_["problems"][0]))["compile_sec"] == 12.5


def test_sweep_extracts_cold_and_pmu_from_the_card(home, monkeypatch):
    ws = home
    _sweep_workspace(ws)
    from opsb_core.fast_feedback import pmu_torch_card

    def fake_card(workspace, title, sha=None, top_n=3):
        return {"title": title, "sha": sha or "0" * 40,
                "elf_guard": {"metrics": {"kernel_o_sum_bytes": 1024, "phase_a_cpu_sec": 3.5}},
                "cold3": {"metrics": {"cold_tax_us": 0.42}},
                "pmu": {"aiv_vec_ratio": 0.31, "aiv_mte2_ratio": 0.87},
                "dominant_bottleneck": "mte2-bound"}

    monkeypatch.setattr(pmu_torch_card, "build_pmu_torch_card", fake_card)
    cold = call_tool("parhelion_report", {"view": "sweep", "metric": "cold"}, ws, role="orchestrator")
    assert dict(zip(cold["columns"], cold["problems"][0]))["cold_tax_us"] == 0.42
    pmu = call_tool("parhelion_report", {"view": "sweep", "metric": "pmu"}, ws, role="orchestrator")
    assert dict(zip(pmu["columns"], pmu["problems"][0]))["pmu"] == "mte2-bound 87.0%"


def test_sweep_is_orchestrator_only(home):
    ws = home
    _sweep_workspace(ws)
    from opsb_core.mcp.report_views import _sweep

    with pytest.raises(PermissionError, match="orchestrator only"):
        _sweep(ws, {"metric": "size"}, "worker")
    with pytest.raises(PermissionError):
        call_tool("parhelion_report", {"view": "sweep"}, ws, role="worker", agent_id="dee-x")
    with pytest.raises(ValueError, match="metric must be one of"):
        call_tool("parhelion_report", {"view": "sweep", "metric": "fps"}, ws, role="orchestrator")
    # every documented view name is still discoverable from the tool description
    orchestrator = {tool["name"]: tool for tool in list_tool_schemas("orchestrator")}
    for view in handlers_report.VIEWS:
        assert view in orchestrator["parhelion_report"]["description"]


def test_sweep_reply_stays_inside_four_kilobytes(home):
    ws = home
    _sweep_workspace(ws, count=120)
    out = call_tool("parhelion_report", {"view": "sweep", "metric": "size"}, ws, role="orchestrator")
    assert len(_text(out).encode("utf-8")) <= SWEEP_BYTES
    assert out.get("truncated") is True and len(out["problems"]) < 120


# ---------------------------------------------------------------- 5. the two sprint metrics


def test_comms_metrics_count_broadcasts_and_evidence_classes(tmp_path):
    from opsb_core.replay import sprint_metrics
    from opsb_core.replay.loader import AgentLog, ToolCall, Turn

    def call(name, args):
        return ToolCall(seq=1, ts="2026-11-26T00:00:00Z", call_id=name, name=name, args=args)

    turn = Turn(index=0, seq=1, ts="2026-11-26T00:00:00Z", session_id="s", prompt="p", kind="brief")
    turn.tools = [
        call("parhelion_squad", {"to": "all", "evidence": {"class": "local", "ref": "run-1"}}),
        call("parhelion_squad", {"recipient": "dee-2", "evidence": {"class": "online_receipt", "ref": "sub-1"}}),
        call("parhelion_squad", {"to": "all", "evidence": "legacy/path#a"}),  # a pre-P27 envelope
        call("parhelion_squad", {}),  # a listing call, not a message
    ]
    log = AgentLog(agent_id="dee-1", path="", turns=[turn], usage=[], sessions=[], cuts=[])
    out = sprint_metrics.comms_metrics([log], 2.0)
    assert out["squad_messages"] == 3 and out["broadcasts"] == 2
    assert out["evidence"] == {"local": 1, "paired": 0, "online_receipt": 1, "legacy": 1}
    full = sprint_metrics.compute([], logs=[log], workspace=None)
    assert {k: v for k, v in full["comms"].items() if k != "per_hour"} == \
        {k: v for k, v in out.items() if k != "per_hour"}


def test_evidence_module_round_trip():
    assert evidence.normalize(None) == {"class": "local", "ref": ""}
    assert evidence.normalize("results/x#a") == {"class": "local", "ref": "results/x#a"}
    assert evidence.normalize({"class": "Paired", "ref": " pair-1 "}) == {"class": "paired", "ref": "pair-1"}
    assert evidence.marker({"class": "online_receipt"}) == ""
    assert evidence.marker({"class": "paired"}).startswith("待验证 (paired)")
    assert evidence.weakest([{"class": "online_receipt"}, {"class": "local"}]) == "local"
    assert evidence.line({"class": "paired", "ref": "pair-1"}) == "paired:pair-1"
    with pytest.raises(ValueError):
        evidence.require({"class": "local", "ref": "x" * 400})
